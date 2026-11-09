from __future__ import annotations

import time
from typing import Any

from ..core.dates import utcnow
from .db import DB, Row, dumps, loads
from .usage import spent_in_run

LOCK_KEY = "run_lock"
LOCK_TTL_S = 3 * 3600


def acquire_lock(db: DB, owner: str) -> bool:
    """One run at a time: the daemon, the web button and the CLI share one queue."""
    with db.tx():
        held = db.kv_get(LOCK_KEY)
        if held and time.time() - held.get("ts", 0) < LOCK_TTL_S:
            return False
        db.kv_set(LOCK_KEY, {"ts": time.time(), "owner": owner})
    return True


def release_lock(db: DB) -> None:
    db.kv_delete(LOCK_KEY)


def start_run(db: DB, trigger: str) -> int:
    return int(db.execute("INSERT INTO runs(started_at, trigger) VALUES(?,?)", (utcnow(), trigger)).lastrowid)


def finish_run(db: DB, run_id: int, status: str, plan: Any, stats: Any, error: str | None = None) -> None:
    db.execute("UPDATE runs SET finished_at=?, status=?, plan_json=?, stats_json=?, cost_usd=?, error=? WHERE id=?",
               (utcnow(), status, dumps(plan), dumps(stats), spent_in_run(db, run_id), error, run_id))


def abort_running(db: DB) -> int:
    return db.execute("UPDATE runs SET status='aborted', finished_at=? WHERE status='running'", (utcnow(),)).rowcount


def recent_runs(db: DB, limit: int = 100) -> list[Row]:
    rows = db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["stats"] = loads(r["stats_json"], {}) or {}
        r["plan"] = loads(r["plan_json"], {}) or {}
    return rows


def last_finished_run(db: DB) -> Row | None:
    return db.one("SELECT * FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1")
