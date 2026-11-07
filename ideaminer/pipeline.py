"""One pipeline run, and the daemon that repeats it on a schedule.

collect → prefilter → triage → enrich → extract → classify → dedup → explore.
Every stage drains its own queue in the database, so work left over because of
the budget or a failure is picked up by the next run.
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
from .context import Ctx, make_ctx
from .core.models import Scope
from .net.http import redact
from .llm import BudgetExceeded, LLMError
from .planner import Plan, make_plan
from .sources.threads import ensure_token as ensure_threads_token
from .stages.classify import run_classify
from .stages.collect import run_collect, run_prefilter
from .stages.dedup import run_dedup
from .stages.digest import run_digest
from .stages.enrich import run_enrich
from .stages.explore import prune_directions, run_explore, should_explore
from .stages.extract import run_extract
from .stages.triage import run_triage
from .storage.runs import acquire_lock, finish_run, release_lock, start_run
from .storage.usage import spent_in_run

log = logging.getLogger("ideaminer")

LLM_STAGES = (
    ("triage", run_triage),
    ("enrich", run_enrich),
    ("extract", run_extract),
    ("classify", run_classify),
    ("dedup", run_dedup),
)
ERROR_TAIL_CHARS = 4000
MIN_SLEEP_S = 60
BUSY_RETRY_S = 600


def run_once(ctx: Ctx, trigger: str = "manual", *, only_sources: list[str] | None = None,
             only_directions: list[str] | None = None, collect: bool = True, use_llm: bool = True,
             explore: bool | None = None) -> dict[str, Any]:
    """`only_*` limit both collection and the LLM stages; a limited run never explores
    unless asked to explicitly."""
    if not acquire_lock(ctx.db, trigger):
        log.warning("другой прогон ещё идёт (снять зависшую блокировку: ideaminer unlock)")
        return {"status": "busy", "stats": {}, "cost_usd": 0.0}
    try:
        return _run(ctx, trigger, only_sources, only_directions, collect, use_llm, explore)
    finally:
        release_lock(ctx.db)


def _run(ctx: Ctx, trigger: str, only_sources: list[str] | None, only_directions: list[str] | None,
         collect: bool, use_llm: bool, explore: bool | None) -> dict[str, Any]:
    run_id = start_run(ctx.db, trigger)
    ctx.set_run(run_id)
    ctx.stats = {}
    ctx.scope = Scope(tuple(only_sources or ()), tuple(only_directions or ()))
    plan: Plan | None = None
    status, error = "ok", None
    started = time.monotonic()
    log.info("═══ прогон #%s (%s) ═══", run_id, trigger)
    try:
        if collect:
            plan = _collect(ctx, only_sources, only_directions)
        run_prefilter(ctx)
        if ctx.scope:
            log.info("обрабатываются только посты: %s", ctx.scope.describe())
        if use_llm:
            status = _process(ctx, explore)
    except Exception as e:  # noqa: BLE001 - the run is recorded whatever happens
        status, error = "error", redact("".join(traceback.format_exception(e)))[-ERROR_TAIL_CHARS:]
        log.error("прогон #%s упал: %s", run_id, e)
    finally:
        ctx.scope = Scope()
        ctx.stats["duration_s"] = round(time.monotonic() - started, 1)
        finish_run(ctx.db, run_id, status, plan.to_json() if plan else None, ctx.stats, error)
    cost = spent_in_run(ctx.db, run_id)
    log.info("═══ прогон #%s: %s за %.0f с, расход $%.4f ═══", run_id, status, ctx.stats["duration_s"], cost)
    return {"run_id": run_id, "status": status, "stats": ctx.stats, "cost_usd": cost}


def _collect(ctx: Ctx, only_sources: list[str] | None, only_directions: list[str] | None) -> Plan:
    if ctx.cfg.secrets.threads_access_token:
        ensure_threads_token(ctx.cfg, ctx.db, log)
    plan = make_plan(ctx, only_sources, only_directions)
    log.info("направления: %s; лент: %d", ", ".join(plan.directions), len(plan.feeds))
    for name, state in plan.sources.items():
        log.info("  источник %-8s %s", name, state)
    run_collect(ctx, plan)
    return plan


def _process(ctx: Ctx, explore: bool | None) -> str:
    if not ctx.llm.available:
        ctx.stats["llm"] = "нет ключей ИИ — только сбор"
        log.warning("ИИ недоступен: задайте DEEPSEEK_API_KEY или REPLICATE_API_TOKEN")
        return "ok"
    for name, stage in LLM_STAGES:
        try:
            stage(ctx)
        except BudgetExceeded as e:
            ctx.stats.setdefault(name, {})["budget_stop"] = 1
            log.warning("[%s] %s", name, e)
            prune_directions(ctx)
            return "budget"
    if explore or (explore is None and not ctx.scope and should_explore(ctx)):
        try:
            run_explore(ctx)
        except (BudgetExceeded, LLMError) as e:
            log.warning("[explore] %s", e)
    prune_directions(ctx)
    return "ok"


def digest_due(ctx: Ctx) -> bool:
    now = datetime.now(timezone.utc)
    return now.hour >= ctx.cfg.schedule.digest_hour_utc and ctx.db.kv_get("digest_last_date") != f"{now:%Y-%m-%d}"


def daemon(cfg: Config, config_path: str | None = None) -> None:
    """Runs, writes the digest when due and sleeps, until SIGTERM or SIGINT."""
    stop = threading.Event()

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("получен сигнал %s, завершаюсь после текущего шага", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    rng = random.Random()
    while not stop.is_set():
        cfg = _reload(cfg, config_path)
        busy = _daemon_cycle(cfg)
        sleep_s = BUSY_RETRY_S if busy else _interval(cfg, rng)
        log.info("следующий прогон через %.0f мин", sleep_s / 60)
        stop.wait(sleep_s)


def _reload(cfg: Config, config_path: str | None) -> Config:
    """Edits to config.yaml apply without a restart; a broken file keeps the previous config."""
    try:
        return load_config(config_path)
    except Exception as e:  # noqa: BLE001
        log.error("config.yaml не читается, беру прежний: %s", e)
        return cfg


def _daemon_cycle(cfg: Config) -> bool:
    """One scheduled run; True when another run held the lock."""
    ctx = make_ctx(cfg)
    busy = False
    try:
        busy = run_once(ctx, trigger="daemon")["status"] == "busy"
        if digest_due(ctx):
            log.info("дайджест: %s", run_digest(ctx))
    except Exception as e:  # noqa: BLE001 - the daemon must outlive a failed cycle
        log.error("цикл демона: %s", e)
    finally:
        ctx.close()
    return busy


def _interval(cfg: Config, rng: random.Random) -> float:
    s = cfg.schedule
    return max(MIN_SLEEP_S, s.run_every_minutes * 60 + rng.uniform(-1, 1) * s.jitter_minutes * 60)
