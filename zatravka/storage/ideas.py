"""Writes and pipeline-side reads of ideas: extraction, classification, clustering."""
from __future__ import annotations

from typing import Any

from ..core.dates import utcnow
from ..core.scoring import base_score, final_score
from .db import DB, Row, dumps, loads

IDEA_TEXT_COLUMNS = ("title", "one_liner", "problem", "solution", "audience", "insight_type", "reasoning",
                     "why_now", "monetization", "risks", "confidence")


def insert_idea(db: DB, run_id: int | None, post_id: int, fields: dict[str, Any], quote: str,
                quote_verified: bool) -> int:
    now = utcnow()
    columns = ", ".join(IDEA_TEXT_COLUMNS)
    idea_id = int(db.execute(
        f"INSERT INTO ideas(created_at, updated_at, run_id, {columns})"
        f" VALUES(?,?,?,{','.join('?' * len(IDEA_TEXT_COLUMNS))})",
        (now, now, run_id, *(fields.get(c) for c in IDEA_TEXT_COLUMNS))).lastrowid)
    db.execute("INSERT OR IGNORE INTO idea_evidence(idea_id, post_id, quote, quote_verified) VALUES(?,?,?,?)",
               (idea_id, post_id, quote, int(quote_verified)))
    return idea_id


def unclassified_ideas(db: DB, taxonomy_version: str, limit: int) -> list[Row]:
    """New ideas and ideas labelled with an older taxonomy (they keep their labels until relabelled)."""
    return db.query(
        "SELECT id, title, one_liner, problem, solution, audience, insight_type, mentions FROM ideas"
        " WHERE classified_at IS NULL OR taxonomy_version IS NOT ?"
        " ORDER BY classified_at IS NOT NULL, id LIMIT ?", (taxonomy_version, limit))


def count_unclassified(db: DB, taxonomy_version: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM ideas WHERE classified_at IS NULL OR taxonomy_version IS NOT ?",
                         (taxonomy_version,), 0))


def count_outdated(db: DB, taxonomy_version: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM ideas WHERE taxonomy_version IS NOT ?", (taxonomy_version,), 0))


def forget_taxonomy(db: DB) -> int:
    """Queues every idea for relabelling; current labels stay visible meanwhile."""
    return db.execute("UPDATE ideas SET taxonomy_version=NULL").rowcount


def first_source(db: DB, idea_id: int) -> dict[str, Any]:
    """Where an idea came from: platform, community, language, region hint and quote."""
    row = db.one("SELECT p.platform, p.community, p.lang, p.region_hint, e.quote FROM idea_evidence e"
                 " JOIN posts p ON p.id=e.post_id WHERE e.idea_id=? LIMIT 1", (idea_id,))
    return {k: v for k, v in (row or {}).items() if v}


def save_classification(db: DB, idea_id: int, labels: dict[str, Any], taxonomy_version: str) -> None:
    now = utcnow()
    db.execute(
        "UPDATE ideas SET market=?, sphere=?, spheres_json=?, nature=?, business_model=?, tags_json=?,"
        " scores_json=?, score_note=?, base_score=?, score=?, classified_at=?, taxonomy_version=?,"
        " regions_json=?, origin_region=?, updated_at=? WHERE id=?",
        (labels["market"], labels["sphere"], dumps(labels["secondary_spheres"]), labels["nature"],
         labels["business_model"], dumps(labels["tags"]), dumps(labels["scores"]), labels["score_note"],
         labels["base_score"], labels["score"], now, taxonomy_version, dumps(labels["regions"]),
         labels["origin_region"], now, idea_id))


def rescore_all(db: DB, weights: dict[str, float], mention_bonus: float) -> int:
    rows = db.query("SELECT id, scores_json, mentions FROM ideas WHERE scores_json IS NOT NULL")
    updates = []
    for r in rows:
        base = base_score(loads(r["scores_json"], {}), weights)
        updates.append((base, final_score(base, r["mentions"] or 1, mention_bonus), r["id"]))
    db.executemany("UPDATE ideas SET base_score=?, score=? WHERE id=?", updates)
    return len(updates)


def pending_dedup(db: DB) -> list[Row]:
    return db.query("SELECT id, title, one_liner, sphere, audience FROM ideas"
                    " WHERE dedup_done=0 AND classified_at IS NOT NULL ORDER BY id")


def cluster_roots(db: DB) -> list[Row]:
    return db.query("SELECT id, title, one_liner, sphere, audience FROM ideas WHERE dedup_done=1 AND cluster_id=id")


def cluster_root(db: DB, idea_id: int) -> int:
    seen = set()
    while idea_id not in seen:
        seen.add(idea_id)
        row = db.one("SELECT cluster_id FROM ideas WHERE id=?", (idea_id,))
        if not row or row["cluster_id"] in (None, idea_id):
            return idea_id
        idea_id = row["cluster_id"]
    return idea_id


def mark_unique(db: DB, idea_id: int) -> None:
    db.execute("UPDATE ideas SET cluster_id=id, dedup_done=1, updated_at=? WHERE id=?", (utcnow(), idea_id))


def merge_into(db: DB, idea_id: int, root: int) -> None:
    """Moves the idea and everything already attached to it under the cluster root."""
    now = utcnow()
    db.execute("UPDATE ideas SET cluster_id=?, updated_at=? WHERE cluster_id=?", (root, now, idea_id))
    db.execute("UPDATE ideas SET cluster_id=?, dedup_done=1, updated_at=? WHERE id=?", (root, now, idea_id))


def refresh_mentions(db: DB, mention_bonus: float) -> None:
    rows = db.query("SELECT c.id, c.base_score, c.mentions,"
                    " (SELECT COUNT(*) FROM ideas m WHERE m.cluster_id=c.id) AS n"
                    " FROM ideas c WHERE c.cluster_id=c.id")
    updates = [(r["n"], final_score(r["base_score"], r["n"], mention_bonus), r["id"])
               for r in rows if r["n"] != r["mentions"]]
    if updates:
        db.executemany("UPDATE ideas SET mentions=?, score=? WHERE id=?", updates)


def sphere_summary(db: DB, limit: int = 12) -> list[Row]:
    return db.query("SELECT sphere, COUNT(*) AS n, ROUND(AVG(score), 2) AS avg FROM ideas"
                    " WHERE cluster_id=id AND sphere IS NOT NULL GROUP BY sphere ORDER BY avg DESC LIMIT ?", (limit,))


def best_ideas(db: DB, limit: int = 12) -> list[Row]:
    return db.query("SELECT title, sphere, score FROM ideas WHERE cluster_id=id AND score IS NOT NULL"
                    " ORDER BY score DESC LIMIT ?", (limit,))


def count_ideas(db: DB, *, clusters_only: bool = False, created_since: str | None = None) -> int:
    where, params = ["1=1"], []
    if clusters_only:
        where.append("cluster_id=id")
    if created_since:
        where.append("created_at >= ?")
        params.append(created_since)
    return int(db.scalar(f"SELECT COUNT(*) FROM ideas WHERE {' AND '.join(where)}", params, 0))


def idea_breakdown(db: DB, column: str) -> list[Row]:
    if column not in ("market", "nature", "sphere", "insight_type", "origin_region"):
        raise ValueError(f"ideas cannot be grouped by {column}")
    return db.query(f"SELECT {column} AS k, COUNT(*) AS n FROM ideas WHERE cluster_id=id"
                    f" GROUP BY {column} ORDER BY n DESC")


def region_breakdown(db: DB) -> list[Row]:
    return db.query("SELECT j.value AS k, COUNT(*) AS n FROM ideas i, json_each(COALESCE(i.regions_json,'[]')) j"
                    " WHERE i.cluster_id=i.id GROUP BY j.value ORDER BY n DESC")


def set_idea_status(db: DB, idea_id: int, status: str) -> bool:
    return bool(db.execute("UPDATE ideas SET status=?, updated_at=? WHERE id=?", (status, utcnow(), idea_id)).rowcount)


def set_idea_note(db: DB, idea_id: int, note: str) -> bool:
    return bool(db.execute("UPDATE ideas SET note=?, updated_at=? WHERE id=?", (note, utcnow(), idea_id)).rowcount)
