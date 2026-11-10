from __future__ import annotations

from typing import Any

from ..core.dates import iso, utcnow
from ..core.models import Comment, RawPost, Scope
from .db import DB, Row, dumps, placeholders

TRIAGE_COLUMNS = "id, platform, community, title, body, score, num_comments, created_at, lang, direction"
_GROUPABLE = ("status", "platform")


def _scope_sql(scope: Scope) -> tuple[str, list[str]]:
    """An " AND …" condition on posts for the scope, and its parameters."""
    parts, params = [], []
    if scope.sources:
        # A post's source is the prefix of its feed key: "x:search:…", "threads:profile:…"
        parts.append(f"substr(feed, 1, instr(feed, ':') - 1) IN ({placeholders(scope.sources)})")
        params += scope.sources
    if scope.directions:
        parts.append(f"direction IN ({placeholders(scope.directions)})")
        params += scope.directions
    return "".join(f" AND {p}" for p in parts), params


def insert_posts(db: DB, posts: list[RawPost], run_id: int | None) -> int:
    """Stores new posts and returns how many were new. Duplicates by id or by text
    (cross-posts) are skipped; known posts get their score refreshed."""
    new = 0
    now = utcnow()
    with db.tx():
        for p in posts:
            content_hash = p.content_hash()
            if db.one("SELECT 1 FROM posts WHERE content_hash=? AND NOT (platform=? AND ext_id=?)",
                      (content_hash, p.platform, p.ext_id)):
                continue
            inserted = db.execute(
                "INSERT OR IGNORE INTO posts(platform, ext_id, url, author, community, title, body, lang,"
                " created_at, fetched_at, score, num_comments, metrics_json, direction, feed, run_id,"
                " content_hash, nsfw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.platform, p.ext_id, p.url, p.author, p.community, p.title, p.body, p.lang,
                 iso(p.created_at), now, p.score, p.num_comments, dumps(p.metrics) if p.metrics else None,
                 p.direction, p.feed, run_id, content_hash, int(p.nsfw)),
            ).rowcount
            if inserted:
                new += 1
            elif p.score is not None or p.num_comments is not None:
                db.execute("UPDATE posts SET score=COALESCE(?, score), num_comments=COALESCE(?, num_comments)"
                           " WHERE platform=? AND ext_id=?", (p.score, p.num_comments, p.platform, p.ext_id))
    return new


def unfiltered_posts(db: DB) -> list[Row]:
    return db.query("SELECT id, title, body, created_at, score, num_comments, nsfw, lang, direction"
                    " FROM posts WHERE status='new'")


def save_prefilter(db: DB, verdicts: list[tuple[str, float, str, str, str | None, int]]) -> None:
    """Rows of (status, score, reason, lang, region_hint, post_id)."""
    db.executemany("UPDATE posts SET status=?, prefilter_score=?, prefilter_reason=?, lang=?, region_hint=?"
                   " WHERE id=?", verdicts)


def expire_candidates(db: DB, created_before: str) -> int:
    return db.execute("UPDATE posts SET status='filtered', prefilter_reason='stale'"
                      " WHERE status='candidate' AND created_at IS NOT NULL AND created_at < ?",
                      (created_before,)).rowcount


def top_candidates(db: DB, scope: Scope, limit: int) -> list[Row]:
    where, params = _scope_sql(scope)
    return db.query(f"SELECT {TRIAGE_COLUMNS} FROM posts WHERE status='candidate'{where}"
                    " ORDER BY prefilter_score DESC, created_at DESC LIMIT ?", (*params, limit))


def random_candidates(db: DB, scope: Scope, limit: int, exclude: list[int]) -> list[Row]:
    if limit <= 0:
        return []
    where, params = _scope_sql(scope)
    return db.query(f"SELECT {TRIAGE_COLUMNS} FROM posts WHERE status='candidate'{where}"
                    f" AND id NOT IN ({placeholders(exclude) or 'NULL'}) ORDER BY RANDOM() LIMIT ?",
                    (*params, *exclude, limit))


def save_triage(db: DB, results: list[tuple[float, str, str, str, int]]) -> None:
    """Rows of (score, signals_json, note, status, post_id)."""
    db.executemany("UPDATE posts SET triage_score=?, triage_signals=?, triage_note=?, status=? WHERE id=?", results)


def promising_posts(db: DB, scope: Scope, limit: int, without_comments: bool = False) -> list[Row]:
    where, params = _scope_sql(scope)
    if without_comments:
        where += " AND comments_json IS NULL"
    return db.query(f"SELECT * FROM posts WHERE status='promising'{where}"
                    " ORDER BY triage_score DESC, prefilter_score DESC LIMIT ?", (*params, limit))


def unvisited_posts(db: DB, platform: str, direction: str, limit: int) -> list[Row]:
    """A direction's best-rated posts whose page has not been opened yet, whatever their status."""
    return db.query("SELECT * FROM posts WHERE platform=? AND direction=? AND comments_json IS NULL"
                    " ORDER BY triage_score IS NULL, triage_score DESC, prefilter_score DESC LIMIT ?",
                    (platform, direction, limit))


def save_comments(db: DB, post_id: int, comments: list[Comment]) -> None:
    db.execute("UPDATE posts SET comments_json=? WHERE id=?", (dumps([c.__dict__ for c in comments]), post_id))


def record_extract_failure(db: DB, post: Row, error: str) -> None:
    """The first failure keeps the post queued; the second one parks it as an error."""
    status = "error" if post.get("error") else "promising"
    db.execute("UPDATE posts SET error=?, status=? WHERE id=?", (error[:500], status, post["id"]))


def mark_extracted(db: DB, post_id: int) -> None:
    db.execute("UPDATE posts SET status='extracted', processed_at=?, error=NULL WHERE id=?", (utcnow(), post_id))


def post_counts_by(db: DB, column: str) -> dict[str, int]:
    if column not in _GROUPABLE:
        raise ValueError(f"posts cannot be grouped by {column}")
    return {r["k"]: r["n"] for r in db.query(f"SELECT {column} AS k, COUNT(*) AS n FROM posts"
                                             f" GROUP BY {column} ORDER BY n DESC")}


def count_posts(db: DB, *, statuses: tuple[str, ...] = (), fetched_since: str | None = None) -> int:
    where, params = ["1=1"], []
    if statuses:
        where.append(f"status IN ({placeholders(statuses)})")
        params += statuses
    if fetched_since:
        where.append("fetched_at >= ?")
        params.append(fetched_since)
    return int(db.scalar(f"SELECT COUNT(*) FROM posts WHERE {' AND '.join(where)}", params, 0))


def browse_posts(db: DB, *, status: str | None = None, platform: str | None = None, direction: str | None = None,
           q: str | None = None, limit: int = 40, offset: int = 0) -> tuple[list[Row], int]:
    where, params = ["1=1"], []
    for column, value in (("status", status), ("platform", platform), ("direction", direction)):
        if value:
            where.append(f"{column}=?")
            params.append(value)
    if q:
        where.append("(title LIKE ? OR body LIKE ?)")
        params += [f"%{q}%"] * 2
    w = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM posts WHERE {w}", params, 0)
    rows = db.query(f"SELECT p.*, (SELECT COUNT(*) FROM idea_evidence e WHERE e.post_id=p.id) AS n_ideas"
                    f" FROM posts p WHERE {w} ORDER BY p.id DESC LIMIT ? OFFSET ?", [*params, limit, offset])
    return rows, total


def directions_in_use(db: DB) -> list[Any]:
    return [r["direction"] for r in db.query("SELECT DISTINCT direction FROM posts ORDER BY direction")]
