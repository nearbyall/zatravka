"""SQLite access shared by the daemon, the CLI and the web UI.

One connection per process guarded by a lock: stages write from the main thread
while LLM accounting writes from worker threads. WAL lets the web UI read
while the daemon writes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .schema import MIGRATIONS

Row = dict[str, Any]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def placeholders(values: Any) -> str:
    return ",".join("?" * len(values))


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._tx_depth = 0
        with self.lock:
            for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=30000", "synchronous=NORMAL"):
                self.conn.execute(f"PRAGMA {pragma}")
        self.migrate()

    def migrate(self) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version={i}")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: Any) -> None:
        with self.lock:
            self.conn.executemany(sql, seq)

    def query(self, sql: str, params: Any = ()) -> list[Row]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Any = ()) -> Row | None:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def scalar(self, sql: str, params: Any = (), default: Any = None) -> Any:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return default if r is None or r[0] is None else r[0]

    @contextmanager
    def tx(self) -> Iterator[None]:
        """A transaction; nested calls join the outer one."""
        with self.lock:
            outer = self._tx_depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                self._tx_depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            self._tx_depth -= 1
            if outer:
                self.conn.execute("COMMIT")

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return loads(row["value"], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, dumps(value)))

    def kv_delete(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key=?", (key,))
