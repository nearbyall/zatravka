"""Один прогон конвейера и демон, который гоняет его по расписанию.

collect → prefilter → triage → enrich → extract → classify → dedup → (explore)
Каждая стадия идемпотентна и разбирает очередь из БД, поэтому недоделанное
из-за бюджета или сбоя спокойно доедается следующим прогоном.
"""
from __future__ import annotations

import logging
import random
import signal
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from .config import Config, load_config
from .http import redact
from .context import Ctx, Scope, make_ctx
from .llm import BudgetExceeded, LLMError
from .planner import Plan, make_plan
from .stages.classify import run_classify
from .stages.collect import run_collect, run_prefilter
from .stages.dedup import run_dedup
from .stages.digest import run_digest
from .stages.enrich import run_enrich
from .stages.explore import prune_directions, run_explore, should_explore
from .stages.extract import run_extract
from .stages.triage import run_triage
from .sources.threads import ensure_token as ensure_threads_token

log = logging.getLogger("ideaminer")

LLM_STAGES = (
    ("triage", run_triage),
    ("enrich", run_enrich),
    ("extract", run_extract),
    ("classify", run_classify),
    ("dedup", run_dedup),
)


LOCK_TTL_S = 3 * 3600


def acquire_lock(ctx: Ctx, owner: str) -> bool:
    """Один прогон за раз: демон, кнопка в вебе и CLI не должны толкаться в одной очереди."""
    with ctx.db.tx():
        cur = ctx.db.kv_get("run_lock")
        if cur and time.time() - cur.get("ts", 0) < LOCK_TTL_S:
            return False
        ctx.db.kv_set("run_lock", {"ts": time.time(), "owner": owner})
    return True


def release_lock(ctx: Ctx) -> None:
    ctx.db.execute("DELETE FROM kv WHERE key='run_lock'")


def run_once(ctx: Ctx, trigger: str = "manual", only_sources: list[str] | None = None,
             only_directions: list[str] | None = None, collect: bool = True, use_llm: bool = True,
             explore: bool | None = None) -> dict[str, Any]:
    if not acquire_lock(ctx, trigger):
        log.warning("другой прогон ещё идёт (снять зависшую блокировку: ideaminer unlock)")
        return {"status": "busy", "stats": {}, "cost_usd": 0.0}
    try:
        return _run_once(ctx, trigger, only_sources, only_directions, collect, use_llm, explore)
    finally:
        release_lock(ctx)


def _run_once(ctx: Ctx, trigger: str, only_sources: list[str] | None, only_directions: list[str] | None,
              collect: bool, use_llm: bool, explore: bool | None) -> dict[str, Any]:
    db = ctx.db
    run_id = db.start_run(trigger)
    ctx.set_run(run_id)
    ctx.stats = {}
    ctx.scope = Scope(tuple(only_sources or ()), tuple(only_directions or ()))
    plan: Plan | None = None
    status, error = "ok", None
    started = time.monotonic()
    log.info("═══ прогон #%s (%s) ═══", run_id, trigger)
    try:
        if collect:
            if ctx.cfg.secrets.threads_access_token:
                ensure_threads_token(ctx.cfg, ctx.db, log)
            plan = make_plan(ctx, only_sources, only_directions)
            log.info("направления: %s; лент: %d", ", ".join(plan.directions), len(plan.feeds))
            for name, st in plan.sources.items():
                log.info("  источник %-8s %s", name, st)
            run_collect(ctx, plan)
        run_prefilter(ctx)
        if ctx.scope:
            log.info("обрабатываются только посты: %s", ctx.scope.describe())
        if use_llm and not ctx.llm.available:
            ctx.stats["llm"] = "нет ключей ИИ — только сбор"
            log.warning("ИИ недоступен: задайте DEEPSEEK_API_KEY или REPLICATE_API_TOKEN")
        elif use_llm:
            for name, fn in LLM_STAGES:
                try:
                    fn(ctx)
                except BudgetExceeded as e:
                    status = "budget"
                    ctx.stats.setdefault(name, {})["budget_stop"] = 1
                    log.warning("[%s] %s", name, e)
                    break
            # Прогон с фильтрами — прицельный: новые направления он не ищет, если не попросить
            if status == "ok" and (explore or (explore is None and not ctx.scope and should_explore(ctx))):
                try:
                    run_explore(ctx)
                except (BudgetExceeded, LLMError) as e:
                    log.warning("[explore] %s", e)
            prune_directions(ctx)
    except Exception as e:  # noqa: BLE001 — прогон фиксируется в БД при любой ошибке
        status, error = "error", redact("".join(traceback.format_exception(e)))[-4000:]
        log.error("прогон #%s упал: %s", run_id, e)
    finally:
        ctx.scope = Scope()
        ctx.stats["duration_s"] = round(time.monotonic() - started, 1)
        db.finish_run(run_id, status, plan.to_json() if plan else None, ctx.stats, error)
    cost = db.spent_in_run(run_id)
    log.info("═══ прогон #%s: %s за %.0f с, расход $%.4f ═══", run_id, status, ctx.stats["duration_s"], cost)
    return {"run_id": run_id, "status": status, "stats": ctx.stats, "cost_usd": cost}


def digest_due(ctx: Ctx) -> bool:
    now = datetime.now(timezone.utc)
    return now.hour >= ctx.cfg.schedule.digest_hour_utc and ctx.db.kv_get("digest_last_date") != f"{now:%Y-%m-%d}"


def daemon(cfg: Config, config_path: str | None = None) -> None:
    """Бесконечный цикл: прогон → при необходимости дайджест → сон. Ловит SIGTERM/SIGINT."""
    stop = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        log.info("получен сигнал %s, завершаюсь после текущего шага", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    rng = random.Random()
    while not stop.is_set():
        # Конфиг перечитывается каждый цикл: правки config.yaml подхватываются без перезапуска
        try:
            cfg = load_config(config_path)
        except Exception as e:  # noqa: BLE001
            log.error("config.yaml не читается, беру прежний: %s", e)
        ctx = make_ctx(cfg)
        busy = False
        try:
            busy = run_once(ctx, trigger="daemon")["status"] == "busy"
            if digest_due(ctx):
                path = run_digest(ctx)
                log.info("дайджест: %s", path)
        except Exception as e:  # noqa: BLE001
            log.error("цикл демона: %s", e)
        finally:
            ctx.close()
            ctx.db.close()
        sch = cfg.schedule
        sleep_s = max(60, sch.run_every_minutes * 60 + rng.uniform(-1, 1) * sch.jitter_minutes * 60)
        if busy:  # идёт прогон из веба или CLI — заглянем позже, а не через полный интервал
            sleep_s = 600
        log.info("следующий прогон через %.0f мин", sleep_s / 60)
        stop.wait(sleep_s)
