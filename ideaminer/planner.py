"""Планировщик прогона: какие направления и ленты читать сейчас.

Каждый прогон = обязательные направления + случайная выборка остальных.
Выборка взвешена «отдачей» направления (сколько хороших идей на пост прошедший
триаж, со сглаживанием), а доля epsilon — чистый случай, чтобы система не
застревала в одних и тех же темах. Плюс горсть случайных сабреддитов из пула.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .accounts import sync_config_accounts
from .config import Config, Direction
from .db import DB, dumps, loads, utcnow
from .models import FeedSpec

if TYPE_CHECKING:
    from .context import Ctx

SYSTEM_DIRECTIONS = {
    "wildcard": "Случайные сабреддиты",
    "inbox": "Импорт из файлов",
}
SPEC_FIELDS = ("subreddits", "reddit_search", "x_queries", "threads_queries", "search_scope", "region")


@dataclass
class Plan:
    directions: list[str] = field(default_factory=list)
    feeds: list[FeedSpec] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "directions": self.directions,
            "sources": self.sources,
            "feeds": [f.key for f in self.feeds],
        }


def sync_directions(cfg: Config, db: DB) -> None:
    """Переносит направления из config.yaml в БД. Переключатель «вкл/выкл» из
    веб-интерфейса сохраняется; удалённые из конфига направления выключаются."""
    now = utcnow()
    existing = {r["key"]: r for r in db.query("SELECT key, origin, enabled FROM directions")}
    with db.tx():
        for key, d in cfg.directions.items():
            spec = dumps({k: getattr(d, k) for k in SPEC_FIELDS})
            if key in existing:
                db.execute(
                    "UPDATE directions SET name=?, spec_json=?, weight=?, origin='config', updated_at=?,"
                    " enabled=CASE WHEN ? THEN enabled ELSE 0 END WHERE key=?",
                    (d.name, spec, d.weight, now, int(d.enabled), key),
                )
            else:
                db.execute(
                    "INSERT INTO directions(key, name, origin, enabled, weight, spec_json, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (key, d.name, "config", int(d.enabled), d.weight, spec, now, now),
                )
        for key, r in existing.items():
            if r["origin"] == "config" and key not in cfg.directions:
                db.execute("UPDATE directions SET enabled=0, updated_at=? WHERE key=?", (now, key))
        sync_config_accounts(cfg, db)
        for key, name in SYSTEM_DIRECTIONS.items():
            if key not in existing:
                db.execute(
                    "INSERT INTO directions(key, name, origin, enabled, weight, spec_json, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (key, name, "system", 1, 1.0, "{}", now, now),
                )


def load_directions(db: DB, enabled_only: bool = True) -> dict[str, Direction]:
    sql = "SELECT * FROM directions WHERE origin != 'system'" + (" AND enabled=1" if enabled_only else "")
    out = {}
    for r in db.query(sql):
        spec = loads(r["spec_json"], {})
        out[r["key"]] = Direction(key=r["key"], name=r["name"] or r["key"], weight=r["weight"],
                                  enabled=bool(r["enabled"]), **{k: v for k, v in spec.items() if k in SPEC_FIELDS})
    return out


def direction_stats(db: DB, cfg: Config) -> dict[str, dict[str, float]]:
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
        # Отдача = «сколько качества выше среднего» на оценённый пост, со сглаживанием:
        # без истории — 0.1, дальше реальные данные постепенно перевешивают априорное значение
        s["yield"] = round((s.get("value", 0) + 1) / (s.get("triaged", 0) + 10), 4)
    return stats


def pick_directions(directions: dict[str, Direction], stats: dict[str, dict[str, float]],
                    k: int, epsilon: float, rng: random.Random, exclude: set[str]) -> list[str]:
    pool = [key for key in directions if key not in exclude]
    chosen: list[str] = []
    prior = 1 / 10  # отдача направления без истории: (0+1)/(0+10)
    while pool and len(chosen) < k:
        weights = [directions[key].weight * stats.get(key, {}).get("yield", prior) for key in pool]
        total = sum(weights) or 1.0
        probs = [(1 - epsilon) * w / total + epsilon / len(pool) for w in weights]
        r = rng.random() * sum(probs)
        for key, p in zip(pool, probs):
            r -= p
            if r <= 0:
                break
        chosen.append(key)
        pool.remove(key)
    return chosen


def make_plan(ctx: "Ctx", only_sources: list[str] | None = None,
              only_directions: list[str] | None = None) -> Plan:
    cfg, db, rng = ctx.cfg, ctx.db, ctx.rng
    sync_directions(cfg, db)
    directions = load_directions(db)
    plan = Plan()

    available = {}
    for name, src in ctx.sources.items():
        ok, why = src.status()
        if only_sources and name not in only_sources:
            ok, why = False, "не выбран в этом запуске"
        plan.sources[name] = ("✓ " if ok else "✗ ") + why
        if ok:
            available[name] = src

    if only_directions:
        all_dirs = load_directions(db, enabled_only=False)
        chosen = [k for k in only_directions if k in all_dirs]
        directions.update({k: all_dirs[k] for k in chosen})
    else:
        focus = set(cfg.planner.regions)
        if focus:
            # Фокус на регионах: чужие региональные направления не читаем, свои — в каждом прогоне
            directions = {k: d for k, d in directions.items() if not d.region or d.region in focus}
        # Усиление региона: его направления выпадают чаще, остальные регионы не выключаются
        for d in directions.values():
            d.weight *= cfg.planner.boost_regions.get(d.region or "", 1.0)
        core = [k for k in cfg.planner.core_directions if k in directions]
        core += [k for k, d in directions.items() if focus and d.region in focus and k not in core]
        stats = direction_stats(db, cfg)
        chosen = core + pick_directions(directions, stats, cfg.planner.random_directions,
                                        cfg.planner.epsilon, rng, exclude=set(core))
    plan.directions = chosen

    feeds: list[FeedSpec] = []
    for key in chosen:
        for src in available.values():
            feeds += src.plan(directions[key], rng)

    reddit = available.get("reddit")
    if reddit and not only_directions and cfg.planner.wildcard_subreddits and cfg.wildcard_pool:
        subs = rng.sample(cfg.wildcard_pool, min(cfg.planner.wildcard_subreddits, len(cfg.wildcard_pool)))
        wild = Direction(key="wildcard", name=SYSTEM_DIRECTIONS["wildcard"], subreddits=subs)
        feeds += reddit.plan(wild, rng)
        plan.directions.append("wildcard")

    for src in available.values():
        feeds += src.plan_global(rng)

    seen: set[str] = set()
    plan.feeds = [f for f in feeds if not (f.key in seen or seen.add(f.key))]

    now = utcnow()
    db.executemany("UPDATE directions SET last_run_at=?, runs=runs+1 WHERE key=?",
                   [(now, k) for k in plan.directions])
    return plan
