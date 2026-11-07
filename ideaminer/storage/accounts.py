"""Registry of accounts read through their public profiles.

It stands in for keyword search where a platform has none. Accounts come from
config.yaml, manual additions, LLM suggestions and "related threads" under good
posts. Accounts that come back empty several times in a row are switched off.
"""
from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING, Any

from ..core.dates import utcnow
from .db import DB, Row

if TYPE_CHECKING:
    from ..config import Config

USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
PROFILE_LINK = re.compile(r"threads\.(?:com|net)/@([A-Za-z0-9._]+)")
MAX_EMPTY_IN_ROW = 3


def normalize_username(raw: str) -> str | None:
    """"@name", "threads.com/@name/post/…" or "name" → "name"; anything else → None."""
    s = (raw or "").strip()
    link = PROFILE_LINK.search(s)
    if link:
        s = link.group(1)
    s = s.lstrip("@").strip("/ ").lower()
    return s if USERNAME.match(s) else None


def _direction_filter(direction: str | None) -> tuple[str, list[Any]]:
    return ("direction IS NULL", []) if direction is None else ("direction = ?", [direction])


def _insert(db: DB, platform: str, name: str, direction: str | None, origin: str, note: str | None) -> bool:
    return bool(db.execute(
        "INSERT OR IGNORE INTO accounts(platform, username, direction, origin, note, added_at) VALUES(?,?,?,?,?,?)",
        (platform, name, direction, origin, note, utcnow())).rowcount)


def add_account(db: DB, platform: str, username: str, direction: str | None,
                origin: str = "manual", note: str | None = None) -> bool:
    """Adds an account. A manual addition also re-enables a known one."""
    name = normalize_username(username)
    if not name:
        return False
    if not _insert(db, platform, name, direction, origin, note) and origin == "manual":
        db.execute("UPDATE accounts SET enabled=1, empty_fetches=0, direction=COALESCE(?, direction)"
                   " WHERE platform=? AND username=?", (direction, platform, name))
    return True


def adopt_accounts(db: DB, platform: str, usernames: list[str], direction: str | None,
                   origin: str, note: str | None, cap: int) -> list[str]:
    """Adds discovered accounts while the direction has fewer than `cap` enabled ones.
    Returns the names that were actually new."""
    where, params = _direction_filter(direction)
    have = db.scalar(f"SELECT COUNT(*) FROM accounts WHERE platform=? AND enabled=1 AND {where}",
                     [platform, *params], 0)
    added: list[str] = []
    for raw in dict.fromkeys(usernames):
        name = normalize_username(raw)
        if name and have + len(added) < cap and _insert(db, platform, name, direction, origin, note):
            added.append(name)
    return added


def sync_config_accounts(cfg: "Config", db: DB) -> None:
    for raw in cfg.sources.threads.accounts:
        add_account(db, "threads", raw, None, origin="config")
    for key, d in cfg.directions.items():
        for raw in d.threads_accounts:
            add_account(db, "threads", raw, key, origin="config")


def pick_accounts(db: DB, platform: str, direction: str | None, k: int, rng: random.Random) -> list[str]:
    """Up to k enabled accounts of a direction (None means shared ones), least recently
    read first and random among equals, so the list is walked round-robin."""
    if k <= 0:
        return []
    where, params = _direction_filter(direction)
    rows = db.query(f"SELECT username, last_fetched_at FROM accounts WHERE platform=? AND enabled=1 AND {where}",
                    [platform, *params])
    rng.shuffle(rows)
    rows.sort(key=lambda r: r["last_fetched_at"] or "")
    return [r["username"] for r in rows[:k]]


def record_account_fetch(db: DB, platform: str, username: str, n_posts: int) -> None:
    db.execute(
        "UPDATE accounts SET last_fetched_at=?, fetches=fetches+1,"
        " empty_fetches=CASE WHEN ? > 0 THEN 0 ELSE empty_fetches+1 END,"
        " enabled=CASE WHEN ? = 0 AND empty_fetches+1 >= ? THEN 0 ELSE enabled END"
        " WHERE platform=? AND username=?",
        (utcnow(), n_posts, n_posts, MAX_EMPTY_IN_ROW, platform, username))


def remove_account(db: DB, platform: str, username: str) -> bool:
    return bool(db.execute("DELETE FROM accounts WHERE platform=? AND username=?", (platform, username)).rowcount)


def known_usernames(db: DB, platform: str) -> set[str]:
    return {r["username"] for r in db.query("SELECT username FROM accounts WHERE platform=?", (platform,))}


def list_accounts(db: DB, platform: str | None = None) -> list[Row]:
    sql = "SELECT * FROM accounts" + (" WHERE platform=?" if platform else "") + " ORDER BY direction, username"
    return db.query(sql, (platform,) if platform else ())


def accounts_by_direction(db: DB, platform: str) -> dict[str, list[Row]]:
    grouped: dict[str, list[Row]] = {}
    for a in db.query("SELECT * FROM accounts WHERE platform=? ORDER BY username", (platform,)):
        grouped.setdefault(a["direction"] or "wildcard", []).append(a)
    return grouped
