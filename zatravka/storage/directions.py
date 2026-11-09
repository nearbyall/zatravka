from __future__ import annotations

from typing import Any

from ..config import Config, Direction
from ..core.dates import utcnow
from .accounts import sync_config_accounts
from .db import DB, Row, dumps, loads

SYSTEM_DIRECTIONS = {
    "wildcard": "Случайные сабреддиты",
    "inbox": "Импорт из файлов",
}
SPEC_FIELDS = ("subreddits", "reddit_search", "x_queries", "threads_queries", "search_scope", "region")

# Smoothing for a direction's yield: without history it is 1/10, and real
# numbers gradually outweigh the prior as triaged posts accumulate.
_PRIOR_VALUE = 1
_PRIOR_POSTS = 10
PRIOR_YIELD = _PRIOR_VALUE / _PRIOR_POSTS


def _insert(db: DB, key: str, name: str, origin: str, enabled: bool, weight: float, spec: dict[str, Any],
            rationale: str | None = None) -> None:
    now = utcnow()
    db.execute(
        "INSERT INTO directions(key, name, origin, enabled, weight, spec_json, rationale, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (key, name, origin, int(enabled), weight, dumps(spec), rationale, now, now))


def sync_directions(cfg: Config, db: DB) -> None:
    """Copies directions from config.yaml into the database. The on/off switch from the
    web UI survives; directions removed from the config get disabled."""
    now = utcnow()
    existing = {r["key"]: r for r in db.query("SELECT key, origin FROM directions")}
    with db.tx():
        for key, d in cfg.directions.items():
            spec = {k: getattr(d, k) for k in SPEC_FIELDS}
            if key not in existing:
                _insert(db, key, d.name, "config", d.enabled, d.weight, spec)
                continue
            db.execute(
                "UPDATE directions SET name=?, spec_json=?, weight=?, origin='config', updated_at=?,"
                " enabled=CASE WHEN ? THEN enabled ELSE 0 END WHERE key=?",
                (d.name, dumps(spec), d.weight, now, int(d.enabled), key))
        for key, r in existing.items():
            if r["origin"] == "config" and key not in cfg.directions:
                db.execute("UPDATE directions SET enabled=0, updated_at=? WHERE key=?", (now, key))
        sync_config_accounts(cfg, db)
        for key, name in SYSTEM_DIRECTIONS.items():
            if key not in existing:
                _insert(db, key, name, "system", True, 1.0, {})


def load_directions(db: DB, enabled_only: bool = True) -> dict[str, Direction]:
    sql = "SELECT * FROM directions WHERE origin != 'system'" + (" AND enabled=1" if enabled_only else "")
    out = {}
    for r in db.query(sql):
        spec = {k: v for k, v in loads(r["spec_json"], {}).items() if k in SPEC_FIELDS}
        out[r["key"]] = Direction(key=r["key"], name=r["name"] or r["key"], weight=r["weight"],
                                  enabled=bool(r["enabled"]), **spec)
    return out


def add_direction(db: DB, key: str, name: str, spec: dict[str, Any], rationale: str) -> None:
    _insert(db, key, name, "llm", True, 1.0, spec, rationale)


def direction_stats(db: DB, cfg: Config) -> dict[str, dict[str, float]]:
    """Per direction: posts, triaged, promising, ideas, good ideas and yield, i.e. the
    above-average idea quality per triaged post."""
    stats: dict[str, dict[str, float]] = {}
    for r in db.query(
        "SELECT direction, COUNT(*) AS posts, SUM(triage_score IS NOT NULL) AS triaged,"
        " SUM(triage_score >= ?) AS promising FROM posts GROUP BY direction",
        (cfg.triage.threshold,),
    ):
        stats[r["direction"] or "?"] = {"posts": r["posts"], "triaged": r["triaged"] or 0,
                                        "promising": r["promising"] or 0, "ideas": 0, "good": 0}
    for r in db.query(
        "SELECT p.direction, COUNT(DISTINCT i.id) AS ideas,"
        " COUNT(DISTINCT CASE WHEN i.score >= ? THEN i.id END) AS good,"
        " SUM(MAX(i.score - 5, 0)) AS value"
        " FROM ideas i JOIN idea_evidence e ON e.idea_id=i.id JOIN posts p ON p.id=e.post_id"
        " GROUP BY p.direction",
        (cfg.scoring.good_idea_threshold,),
    ):
        s = stats.setdefault(r["direction"] or "?", {"posts": 0, "triaged": 0, "promising": 0})
        s["ideas"], s["good"], s["value"] = r["ideas"], r["good"], round(r["value"] or 0, 2)
    for s in stats.values():
        s["yield"] = round((s.get("value", 0) + _PRIOR_VALUE) / (s.get("triaged", 0) + _PRIOR_POSTS), 4)
    return stats


def regions_by_direction(db: DB) -> dict[str, str]:
    out = {}
    for r in db.query("SELECT key, spec_json FROM directions"):
        region = (loads(r["spec_json"], {}) or {}).get("region")
        if region:
            out[r["key"]] = region
    return out


def mark_directions_planned(db: DB, keys: list[str]) -> None:
    db.executemany("UPDATE directions SET last_run_at=?, runs=runs+1 WHERE key=?", [(utcnow(), k) for k in keys])


def enabled_llm_directions(db: DB) -> list[str]:
    return [r["key"] for r in db.query("SELECT key FROM directions WHERE origin='llm' AND enabled=1")]


def disable_directions(db: DB, keys: list[str]) -> None:
    db.executemany("UPDATE directions SET enabled=0, updated_at=? WHERE key=?", [(utcnow(), k) for k in keys])


def get_direction(db: DB, key: str) -> Row | None:
    return db.one("SELECT * FROM directions WHERE key=?", (key,))


def set_direction_enabled(db: DB, key: str, enabled: bool) -> None:
    db.execute("UPDATE directions SET enabled=?, updated_at=? WHERE key=?", (int(enabled), utcnow(), key))


def directions_overview(db: DB, cfg: Config) -> list[Row]:
    """All directions with their spec and stats: system ones last, then disabled, then by yield."""
    stats = direction_stats(db, cfg)
    rows = db.query("SELECT * FROM directions")
    for r in rows:
        r["spec"] = loads(r["spec_json"], {})
        r["stats"] = stats.get(r["key"], {})
    rows.sort(key=lambda r: (r["origin"] == "system", not r["enabled"], -r["stats"].get("yield", 0), r["key"]))
    return rows
