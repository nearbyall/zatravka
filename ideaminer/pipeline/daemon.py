"""The daemon: runs the pipeline on a schedule and writes the daily digest."""
from __future__ import annotations

import logging
import random
import signal
import threading
from datetime import datetime, timezone
from typing import Any

from ..config import Config, load_config
from .context import Ctx, make_ctx
from .runner import run_once
from .stages.digest import run_digest

log = logging.getLogger("ideaminer")

MIN_SLEEP_S = 60
BUSY_RETRY_S = 600


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
