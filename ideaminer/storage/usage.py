"""Accounting of LLM calls and their cost."""
from __future__ import annotations

from datetime import datetime, timezone

from ..core.dates import utcnow
from .db import DB, Row


def record_llm_call(db: DB, *, run_id: int | None, stage: str, provider: str, model: str, prompt_tokens: int,
                completion_tokens: int, cached_tokens: int, cost_usd: float, duration_ms: int, ok: bool,
                error: str | None) -> None:
    db.execute(
        "INSERT INTO llm_calls(ts, run_id, stage, provider, model, prompt_tokens, completion_tokens,"
        " cached_tokens, cost_usd, duration_ms, ok, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (utcnow(), run_id, stage, provider, model, prompt_tokens, completion_tokens, cached_tokens,
         cost_usd, duration_ms, int(ok), (error or "")[:500] or None))


def spent_since(db: DB, iso_ts: str) -> float:
    return float(db.scalar("SELECT SUM(cost_usd) FROM llm_calls WHERE ts>=?", (iso_ts,), 0.0))


def spent_today(db: DB) -> float:
    return spent_since(db, datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00"))


def spent_in_run(db: DB, run_id: int) -> float:
    return float(db.scalar("SELECT SUM(cost_usd) FROM llm_calls WHERE run_id=?", (run_id,), 0.0))


def llm_totals(db: DB) -> tuple[int, float]:
    row = db.one("SELECT COUNT(*) AS calls, SUM(cost_usd) AS cost FROM llm_calls") or {}
    return int(row.get("calls") or 0), float(row.get("cost") or 0.0)


def daily_llm_usage(db: DB, days: int = 14) -> list[Row]:
    return db.query(
        "SELECT substr(ts,1,10) AS day, COUNT(*) AS calls, SUM(prompt_tokens) AS pin,"
        " SUM(completion_tokens) AS pout, ROUND(SUM(cost_usd), 4) AS cost, SUM(ok=0) AS failed"
        " FROM llm_calls GROUP BY day ORDER BY day DESC LIMIT ?", (days,))
