"""SQLite-хранилище: посты, идеи, направления, прогоны, учёт ИИ-вызовов.

Одно соединение на процесс с блокировкой: стадии конвейера пишут из главного
потока, а учёт ИИ-вызовов — из рабочих потоков. WAL позволяет веб-интерфейсу
читать базу параллельно с демоном.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        trigger TEXT,
        plan_json TEXT,
        stats_json TEXT,
        cost_usd REAL NOT NULL DEFAULT 0,
        error TEXT
    );

    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL,
        ext_id TEXT NOT NULL,
        url TEXT,
        author TEXT,
        community TEXT,
        title TEXT,
        body TEXT,
        lang TEXT,
        created_at TEXT,
        fetched_at TEXT NOT NULL,
        score INTEGER,
        num_comments INTEGER,
        metrics_json TEXT,
        direction TEXT,
        feed TEXT,
        run_id INTEGER,
        content_hash TEXT,
        nsfw INTEGER NOT NULL DEFAULT 0,
        prefilter_score REAL,
        prefilter_reason TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        triage_score REAL,
        triage_signals TEXT,
        triage_note TEXT,
        comments_json TEXT,
        processed_at TEXT,
        error TEXT,
        UNIQUE(platform, ext_id)
    );
    CREATE INDEX IF NOT EXISTS posts_status ON posts(status);
    CREATE INDEX IF NOT EXISTS posts_hash ON posts(content_hash);
    CREATE INDEX IF NOT EXISTS posts_direction ON posts(direction);

    CREATE TABLE IF NOT EXISTS ideas (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        run_id INTEGER,
        title TEXT NOT NULL,
        one_liner TEXT,
        problem TEXT,
        solution TEXT,
        audience TEXT,
        insight_type TEXT,
        reasoning TEXT,
        why_now TEXT,
        monetization TEXT,
        risks TEXT,
        confidence REAL,
        market TEXT,
        sphere TEXT,
        spheres_json TEXT,
        nature TEXT,
        business_model TEXT,
        tags_json TEXT,
        scores_json TEXT,
        score_note TEXT,
        base_score REAL,
        score REAL,
        classified_at TEXT,
        taxonomy_version TEXT,
        cluster_id INTEGER,
        mentions INTEGER NOT NULL DEFAULT 1,
        dedup_done INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'new',
        note TEXT
    );
    CREATE INDEX IF NOT EXISTS ideas_cluster ON ideas(cluster_id);
    CREATE INDEX IF NOT EXISTS ideas_score ON ideas(score);
    CREATE INDEX IF NOT EXISTS ideas_sphere ON ideas(sphere);

    CREATE TABLE IF NOT EXISTS idea_evidence (
        idea_id INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
        post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        quote TEXT,
        quote_verified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (idea_id, post_id)
    );

    CREATE TABLE IF NOT EXISTS directions (
        key TEXT PRIMARY KEY,
        name TEXT,
        origin TEXT NOT NULL DEFAULT 'config',
        enabled INTEGER NOT NULL DEFAULT 1,
        weight REAL NOT NULL DEFAULT 1.0,
        spec_json TEXT NOT NULL,
        rationale TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_run_at TEXT,
        runs INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS llm_calls (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        run_id INTEGER,
        stage TEXT,
        provider TEXT,
        model TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        cached_tokens INTEGER,
        cost_usd REAL NOT NULL DEFAULT 0,
        duration_ms INTEGER,
        ok INTEGER NOT NULL DEFAULT 1,
        error TEXT
    );
    CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts);

    CREATE TABLE IF NOT EXISTS feed_state (
        feed TEXT PRIMARY KEY,
        last_fetched_at TEXT,
        last_status TEXT,
        last_count INTEGER,
        new_count INTEGER,
        errors INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    # v2: регионы
    """
    ALTER TABLE posts ADD COLUMN region_hint TEXT;
    ALTER TABLE ideas ADD COLUMN regions_json TEXT;
    ALTER TABLE ideas ADD COLUMN origin_region TEXT;
    """,
    # v3: реестр аккаунтов для чтения профилей
    """
    CREATE TABLE IF NOT EXISTS accounts (
        platform TEXT NOT NULL,
        username TEXT NOT NULL,
        direction TEXT,
        origin TEXT NOT NULL DEFAULT 'manual',
        enabled INTEGER NOT NULL DEFAULT 1,
        note TEXT,
        added_at TEXT NOT NULL,
        last_fetched_at TEXT,
        fetches INTEGER NOT NULL DEFAULT 0,
        empty_fetches INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (platform, username)
    );
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def loads(v: str | None, default: Any = None) -> Any:
    if not v:
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._in_tx = 0
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.migrate()

    def migrate(self) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(SCHEMA[version:], start=version + 1):
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version={i}")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ── базовые операции ────────────────────────────────────────────────
    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: Any) -> None:
        with self.lock:
            self.conn.executemany(sql, seq)

    def query(self, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Any = ()) -> dict[str, Any] | None:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def scalar(self, sql: str, params: Any = (), default: Any = None) -> Any:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return default if r is None or r[0] is None else r[0]

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Транзакция с поддержкой вложенности (вложенные — no-op)."""
        with self.lock:
            outer = self._in_tx == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._in_tx += 1
            try:
                yield
            except BaseException:
                self._in_tx -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._in_tx -= 1
                if outer:
                    self.conn.execute("COMMIT")

    # ── key-value ───────────────────────────────────────────────────────
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return loads(row["value"], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dumps(value)),
        )

    # ── прогоны ─────────────────────────────────────────────────────────
    def start_run(self, trigger: str) -> int:
        cur = self.execute("INSERT INTO runs(started_at, trigger) VALUES(?,?)", (utcnow(), trigger))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, plan: Any, stats: Any, error: str | None = None) -> None:
        cost = self.scalar("SELECT SUM(cost_usd) FROM llm_calls WHERE run_id=?", (run_id,), 0.0)
        self.execute(
            "UPDATE runs SET finished_at=?, status=?, plan_json=?, stats_json=?, cost_usd=?, error=? WHERE id=?",
            (utcnow(), status, dumps(plan), dumps(stats), cost, error, run_id),
        )

    # ── учёт ИИ ─────────────────────────────────────────────────────────
    def spent_since(self, iso_ts: str) -> float:
        return float(self.scalar("SELECT SUM(cost_usd) FROM llm_calls WHERE ts>=?", (iso_ts,), 0.0))

    def spent_in_run(self, run_id: int) -> float:
        return float(self.scalar("SELECT SUM(cost_usd) FROM llm_calls WHERE run_id=?", (run_id,), 0.0))
