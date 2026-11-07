"""Реестр аккаунтов, которые читаем напрямую (публичные профили).

Нужен там, где площадка не даёт искать по словам: тогда «что читать» — это список
людей. Аккаунты приходят из config.yaml, добавляются вручную, подбираются ИИ или
берутся из «похожих тредов» под удачными постами; реестр помнит, когда аккаунт читали, и выключает те, что раз за разом пусты
(удалён, закрыт, перестал писать).
"""
from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING, Any

from .db import DB
from .core.dates import utcnow

if TYPE_CHECKING:
    from .config import Config

USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
MAX_EMPTY_IN_ROW = 3


def normalize_username(raw: str) -> str | None:
    """«@name», «threads.com/@name/post/…», «name» → «name»; мусор → None."""
    s = (raw or "").strip()
    m = re.search(r"threads\.(?:com|net)/@([A-Za-z0-9._]+)", s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip("/ ").lower()
    return s if USERNAME.match(s) else None


def add_account(db: DB, platform: str, username: str, direction: str | None,
                origin: str = "manual", note: str | None = None) -> bool:
    """Добавляет аккаунт; существующий не трогает (кроме включения, если его добавили руками)."""
    name = normalize_username(username)
    if not name:
        return False
    cur = db.execute(
        "INSERT OR IGNORE INTO accounts(platform, username, direction, origin, note, added_at)"
        " VALUES(?,?,?,?,?,?)", (platform, name, direction, origin, note, utcnow()))
    if not cur.rowcount and origin == "manual":
        db.execute("UPDATE accounts SET enabled=1, empty_fetches=0, direction=COALESCE(?, direction)"
                   " WHERE platform=? AND username=?", (direction, platform, name))
    return True


def adopt_accounts(db: DB, platform: str, usernames: list[str], direction: str | None,
                   origin: str, note: str | None, cap: int) -> list[str]:
    """Добавляет найденные аккаунты, пока у направления меньше cap включённых.
    Возвращает действительно новые имена."""
    where = "direction IS NULL" if direction is None else "direction = ?"
    params: list[Any] = [platform] + ([] if direction is None else [direction])
    have = db.query(f"SELECT COUNT(*) AS n FROM accounts WHERE platform=? AND enabled=1 AND {where}",
                    params)[0]["n"]
    added = []
    for raw in dict.fromkeys(usernames):
        name = normalize_username(raw)
        if have + len(added) >= cap or not name:
            continue
        if db.execute("INSERT OR IGNORE INTO accounts(platform, username, direction, origin, note, added_at)"
                      " VALUES(?,?,?,?,?,?)", (platform, name, direction, origin, note, utcnow())).rowcount:
            added.append(name)
    return added


def sync_config_accounts(cfg: "Config", db: DB) -> None:
    """Аккаунты из config.yaml: общие (sources.threads.accounts) и по направлениям."""
    for raw in cfg.sources.threads.accounts:
        add_account(db, "threads", raw, None, origin="config")
    for key, d in cfg.directions.items():
        for raw in d.threads_accounts:
            add_account(db, "threads", raw, key, origin="config")


def pick_accounts(db: DB, platform: str, direction: str | None, k: int, rng: random.Random) -> list[str]:
    """k включённых аккаунтов направления (None — общие): давно не читанные первыми,
    при равенстве — случайно, чтобы список обходился по кругу."""
    if k <= 0:
        return []
    where = "direction IS NULL" if direction is None else "direction = ?"
    params: list[Any] = [platform] + ([] if direction is None else [direction])
    rows = db.query(f"SELECT username, last_fetched_at FROM accounts WHERE platform=? AND enabled=1 AND {where}",
                    params)
    rng.shuffle(rows)
    rows.sort(key=lambda r: r["last_fetched_at"] or "")
    return [r["username"] for r in rows[:k]]


def record_fetch(db: DB, platform: str, username: str, n_posts: int) -> None:
    """Отмечает чтение; после MAX_EMPTY_IN_ROW пустых чтений подряд аккаунт выключается."""
    db.execute(
        "UPDATE accounts SET last_fetched_at=?, fetches=fetches+1,"
        " empty_fetches=CASE WHEN ? > 0 THEN 0 ELSE empty_fetches+1 END,"
        " enabled=CASE WHEN ? = 0 AND empty_fetches+1 >= ? THEN 0 ELSE enabled END"
        " WHERE platform=? AND username=?",
        (utcnow(), n_posts, n_posts, MAX_EMPTY_IN_ROW, platform, username))


def list_accounts(db: DB, platform: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM accounts" + (" WHERE platform=?" if platform else "") + " ORDER BY direction, username"
    return db.query(sql, (platform,) if platform else ())
