from __future__ import annotations

from ..core.dates import utcnow
from .db import DB, Row


def record_feed_fetch(db: DB, feed: str, status: str, fetched: int, new: int) -> None:
    db.execute(
        "INSERT INTO feed_state(feed, last_fetched_at, last_status, last_count, new_count, errors)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(feed) DO UPDATE SET last_fetched_at=excluded.last_fetched_at,"
        " last_status=excluded.last_status, last_count=excluded.last_count, new_count=excluded.new_count,"
        " errors=feed_state.errors + excluded.errors",
        (feed, utcnow(), status, fetched, new, int(status != "ok")))


def recent_feeds(db: DB, limit: int = 40) -> list[Row]:
    return db.query("SELECT * FROM feed_state ORDER BY last_fetched_at DESC LIMIT ?", (limit,))
