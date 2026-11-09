from __future__ import annotations

from typing import Any

from ..storage.db import DB, Row, loads
from ..storage.idea_queries import evidence_for

Record = dict[str, Any]

EXPORT_FIELDS = ["id", "score", "title", "one_liner", "market", "sphere", "nature", "business_model", "regions",
                 "origin_region", "insight_type", "mentions", "problem", "solution", "audience", "reasoning",
                 "why_now", "monetization", "risks", "tags", "status", "note", "created_at", "sources"]
_DECODED = ("tags", "sources", "regions")


def export_rows(db: DB, rows: list[Row]) -> list[Record]:
    """Idea rows → export records with decoded lists and the posts each idea came from."""
    return [_record(db, r) for r in rows]


def _record(db: DB, r: Row) -> Record:
    item = {k: r.get(k) for k in EXPORT_FIELDS if k not in _DECODED}
    item["tags"] = r.get("tags") or loads(r.get("tags_json"), [])
    item["regions"] = r.get("regions") or loads(r.get("regions_json"), [])
    item["scores"] = r.get("scores") or loads(r.get("scores_json"), {})
    item["sources"] = [{"url": e["url"], "platform": e["platform"], "quote": e["quote"],
                        "verified": bool(e["quote_verified"]), "post_title": e["title"]}
                       for e in evidence_for(db, r["id"])]
    return item
