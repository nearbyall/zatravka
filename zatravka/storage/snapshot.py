"""A sanitized copy of the idea base for the public showcase.

The snapshot holds only what a visitor may see: classified, non-rejected ideas and
links to the posts behind them. Notes, statuses, quotes, post texts and author names
stay in the working database, so the showcase cannot leak them whatever it serves.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..core.dates import utcnow
from .db import DB
from .idea_queries import INACTIVE

IDEA_COLUMNS = ("id, created_at, updated_at, title, one_liner, problem, solution, audience, insight_type, reasoning,"
                " why_now, monetization, risks, confidence, market, sphere, spheres_json, nature, business_model,"
                " tags_json, scores_json, score_note, base_score, score, classified_at, taxonomy_version, cluster_id,"
                " mentions, dedup_done, regions_json, origin_region")
POST_COLUMNS = "id, platform, ext_id, url, community, created_at, fetched_at, score"


def write_snapshot(db: DB, path: str | Path) -> int:
    """Writes the snapshot next to its final place and swaps it in; returns the number of ideas."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    out = DB(tmp)
    inactive = ", ".join(f"'{s}'" for s in INACTIVE)
    out.execute("ATTACH DATABASE ? AS src", (db.path,))
    out.execute(f"INSERT INTO ideas({IDEA_COLUMNS}) SELECT {IDEA_COLUMNS} FROM src.ideas i"
                f" WHERE i.classified_at IS NOT NULL AND i.status NOT IN ({inactive})"
                f" AND EXISTS (SELECT 1 FROM src.ideas r WHERE r.id = i.cluster_id AND r.status NOT IN ({inactive}))")
    out.execute(f"INSERT INTO posts({POST_COLUMNS}, status) SELECT {POST_COLUMNS}, 'extracted' FROM src.posts p"
                " WHERE p.id IN (SELECT e.post_id FROM src.idea_evidence e WHERE e.idea_id IN (SELECT id FROM ideas))")
    out.execute("INSERT INTO idea_evidence(idea_id, post_id, quote, quote_verified)"
                " SELECT e.idea_id, e.post_id, NULL, 1 FROM src.idea_evidence e WHERE e.idea_id IN (SELECT id FROM ideas)")
    out.execute("DETACH DATABASE src")
    out.kv_set("snapshot_at", utcnow())
    ideas = out.scalar("SELECT COUNT(*) FROM ideas WHERE cluster_id = id", (), 0)
    # A rollback journal instead of WAL lets the file be opened from a read-only mount.
    out.execute("PRAGMA journal_mode=DELETE")
    out.execute("VACUUM")
    out.close()
    os.replace(tmp, path)
    return ideas
