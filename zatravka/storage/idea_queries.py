"""Read model of ideas for the UI, CLI, exports and digests."""
from __future__ import annotations

from typing import Any

from .db import DB, Row, loads, placeholders

SORTS = {
    "score": "i.score DESC NULLS LAST, i.id DESC",
    "new": "i.id DESC",
    "mentions": "i.mentions DESC, i.score DESC",
    "novelty": "json_extract(i.scores_json, '$.novelty') DESC, i.score DESC",
    "demand": "json_extract(i.scores_json, '$.demand') DESC, i.score DESC",
    "feasibility": "json_extract(i.scores_json, '$.feasibility') DESC, i.score DESC",
}
INACTIVE = ("rejected", "archived")


def ideas_where(*, market: str | None = None, sphere: str | None = None, nature: str | None = None,
                insight: str | None = None, model: str | None = None, status: str | None = None,
                platform: str | None = None, q: str | None = None, min_score: float | None = None,
                days: float | None = None, region: str | None = None, with_global: bool = True,
                origin: str | None = None, clusters_only: bool = True, **_: Any) -> tuple[str, list[Any]]:
    """WHERE clause over ideas (alias i). `region` is where an idea can launch, `with_global`
    also accepts ideas marked global, and `origin` is where the signal came from."""
    where, params = ["i.classified_at IS NOT NULL"], []
    if region:
        values = [region] + (["global"] if with_global and region != "global" else [])
        where.append(f"EXISTS (SELECT 1 FROM json_each(COALESCE(i.regions_json, '[]')) j"
                     f" WHERE j.value IN ({placeholders(values)}))")
        params += values
    if origin:
        where.append("i.origin_region = ?")
        params.append(origin)
    if clusters_only:
        where.append("i.cluster_id = i.id")
    for column, value in (("market", market), ("sphere", sphere), ("nature", nature),
                          ("insight_type", insight), ("business_model", model)):
        if value:
            where.append(f"i.{column} = ?")
            params.append(value)
    if status == "active" or not status:
        where.append(f"i.status NOT IN ({placeholders(INACTIVE)})")
        params += INACTIVE
    elif status != "all":
        where.append("i.status = ?")
        params.append(status)
    if min_score is not None:
        where.append("i.score >= ?")
        params.append(min_score)
    if days:
        where.append("i.created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
        params.append(f"-{float(days)} days")
    if q:
        where.append("(i.title LIKE ? OR i.one_liner LIKE ? OR i.problem LIKE ? OR i.solution LIKE ?"
                     " OR i.tags_json LIKE ? OR i.audience LIKE ?)")
        params += [f"%{q}%"] * 6
    if platform:
        # An uncorrelated set: a per-idea EXISTS rescanned every post of the platform for each idea.
        where.append("i.id IN (SELECT m.cluster_id FROM posts p JOIN idea_evidence e ON e.post_id=p.id"
                     " JOIN ideas m ON m.id=e.idea_id WHERE p.platform=?)")
        params.append(platform)
    return " AND ".join(where), params


def decode_idea(r: Row) -> Row:
    r["tags"] = loads(r.get("tags_json"), [])
    r["scores"] = loads(r.get("scores_json"), {})
    r["spheres_extra"] = loads(r.get("spheres_json"), [])
    r["regions"] = loads(r.get("regions_json"), [])
    return r


def query_ideas(db: DB, *, sort: str = "score", limit: int = 100, offset: int = 0,
                **filters: Any) -> tuple[list[Row], int]:
    w, params = ideas_where(**filters)
    total = db.scalar(f"SELECT COUNT(*) FROM ideas i WHERE {w}", params, 0)
    rows = db.query(f"SELECT i.* FROM ideas i WHERE {w} ORDER BY {SORTS.get(sort, SORTS['score'])}"
                    " LIMIT ? OFFSET ?", [*params, limit, offset])
    return [decode_idea(r) for r in rows], total


def region_counts(db: DB, by_sphere: bool = False, **filters: Any) -> list[Row]:
    """Idea counts per region; an idea with several regions counts in each."""
    w, params = ideas_where(**filters)
    sphere = "i.sphere AS sphere, " if by_sphere else ""
    group = "i.sphere, j.value" if by_sphere else "j.value"
    return db.query(f"SELECT {sphere}j.value AS region, COUNT(*) AS n, ROUND(MAX(i.score), 1) AS best"
                    f" FROM ideas i, json_each(COALESCE(i.regions_json, '[]')) j WHERE {w} GROUP BY {group}", params)


def facet_counts(db: DB, columns: tuple[str, ...], **filters: Any) -> list[Row]:
    w, params = ideas_where(**filters)
    selected = ", ".join(f"i.{c} AS {c}" for c in columns)
    group = ", ".join(f"i.{c}" for c in columns)
    return db.query(f"SELECT {selected}, COUNT(*) AS n, ROUND(MAX(i.score), 1) AS best"
                    f" FROM ideas i WHERE {w} GROUP BY {group}", params)


def evidence_for(db: DB, cluster_id: int) -> list[Row]:
    return db.query(
        "SELECT e.quote, e.quote_verified, m.id AS idea_id, m.title AS idea_title, p.id AS post_id, p.platform,"
        " p.url, p.title, p.body, p.community, p.author, p.score AS post_score, p.created_at, p.triage_score,"
        " p.triage_note, p.direction FROM ideas m JOIN idea_evidence e ON e.idea_id=m.id"
        " JOIN posts p ON p.id=e.post_id WHERE m.cluster_id=? OR m.id=? ORDER BY p.score DESC",
        (cluster_id, cluster_id))


def lead_evidence(db: DB, cluster_id: int) -> Row | None:
    """The highest-rated post behind a cluster."""
    return db.one("SELECT p.platform, p.community, p.url FROM ideas m JOIN idea_evidence e ON e.idea_id=m.id"
                  " JOIN posts p ON p.id=e.post_id WHERE m.cluster_id=? ORDER BY p.score DESC LIMIT 1", (cluster_id,))


def get_idea(db: DB, idea_id: int) -> Row | None:
    row = db.one("SELECT * FROM ideas WHERE id=?", (idea_id,))
    return decode_idea(row) if row else None


def cluster_members(db: DB, root: int, exclude: int) -> list[Row]:
    return [decode_idea(r) for r in db.query("SELECT * FROM ideas WHERE cluster_id=? AND id != ? ORDER BY score DESC",
                                        (root, exclude))]


def similar_ideas(db: DB, sphere: str, exclude: int, limit: int = 6) -> list[Row]:
    return [decode_idea(r) for r in db.query(
        "SELECT * FROM ideas WHERE cluster_id=id AND sphere=? AND id != ? AND status != 'rejected'"
        " ORDER BY score DESC LIMIT ?", (sphere, exclude, limit))]


def top_recent(db: DB, since: str, min_score: float, limit: int) -> list[Row]:
    """Best active clusters updated since a moment, each with the URL of its top post."""
    return db.query(
        "SELECT i.*, (SELECT p.url FROM idea_evidence e JOIN posts p ON p.id=e.post_id"
        "  JOIN ideas m ON m.id=e.idea_id WHERE m.cluster_id=i.id ORDER BY p.score DESC LIMIT 1) AS url"
        f" FROM ideas i WHERE i.cluster_id=i.id AND i.updated_at >= ? AND i.score >= ?"
        f" AND i.status NOT IN ({placeholders(INACTIVE)}) ORDER BY i.score DESC LIMIT ?",
        (since, min_score, *INACTIVE, limit))
