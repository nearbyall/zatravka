"""Run planner: which directions and feeds to read now.

A run takes the core directions plus a random sample of the rest. The sample is
weighted by each direction's yield, while an epsilon share is pure chance so the
system does not get stuck on the same topics. A handful of random subreddits
from the wildcard pool is added on top.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .config import Direction
from .core.models import FeedSpec
from .core.selection import weighted_choice
from .sources import Source
from .storage.directions import (PRIOR_YIELD, SYSTEM_DIRECTIONS, direction_stats, load_directions,
                                 mark_directions_planned, sync_directions)

if TYPE_CHECKING:
    from .context import Ctx


@dataclass
class Plan:
    directions: list[str] = field(default_factory=list)
    feeds: list[FeedSpec] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"directions": self.directions, "sources": self.sources, "feeds": [f.key for f in self.feeds]}


def pick_directions(directions: dict[str, Direction], stats: dict[str, dict[str, float]],
                    k: int, epsilon: float, rng: random.Random, exclude: set[str]) -> list[str]:
    pool = [key for key in directions if key not in exclude]
    chosen: list[str] = []
    while pool and len(chosen) < k:
        weights = [directions[key].weight * stats.get(key, {}).get("yield", PRIOR_YIELD) for key in pool]
        total = sum(weights) or 1.0
        odds = {key: (1 - epsilon) * w / total + epsilon / len(pool) for key, w in zip(pool, weights)}
        key = weighted_choice(odds, rng)
        chosen.append(key)
        pool.remove(key)
    return chosen


def make_plan(ctx: "Ctx", only_sources: list[str] | None = None,
              only_directions: list[str] | None = None) -> Plan:
    cfg, db, rng = ctx.cfg, ctx.db, ctx.rng
    sync_directions(cfg, db)
    plan = Plan()
    available = _available_sources(ctx.sources, only_sources, plan)
    directions = load_directions(db)
    if only_directions:
        every = load_directions(db, enabled_only=False)
        plan.directions = [k for k in only_directions if k in every]
        directions.update({k: every[k] for k in plan.directions})
    else:
        plan.directions = _sample_directions(ctx, directions)

    feeds = [f for key in plan.directions for src in available.values() for f in src.plan(directions[key], rng)]
    reddit = available.get("reddit")
    if reddit and not only_directions and cfg.planner.wildcard_subreddits and cfg.wildcard_pool:
        subs = rng.sample(cfg.wildcard_pool, min(cfg.planner.wildcard_subreddits, len(cfg.wildcard_pool)))
        feeds += reddit.plan(Direction(key="wildcard", name=SYSTEM_DIRECTIONS["wildcard"], subreddits=subs), rng)
        plan.directions.append("wildcard")
    for src in available.values():
        feeds += src.plan_global(rng)

    plan.feeds = _unique(feeds)
    mark_directions_planned(db, plan.directions)
    return plan


def _unique(feeds: list[FeedSpec]) -> list[FeedSpec]:
    by_key: dict[str, FeedSpec] = {}
    for f in feeds:
        by_key.setdefault(f.key, f)
    return list(by_key.values())


def _available_sources(sources: dict[str, Source], only: list[str] | None, plan: Plan) -> dict[str, Source]:
    available = {}
    for name, src in sources.items():
        ok, why = src.status()
        if only and name not in only:
            ok, why = False, "не выбран в этом запуске"
        plan.sources[name] = ("✓ " if ok else "✗ ") + why
        if ok:
            available[name] = src
    return available


def _sample_directions(ctx: "Ctx", directions: dict[str, Direction]) -> list[str]:
    cfg = ctx.cfg.planner
    focus = set(cfg.regions)
    if focus:
        # With a regional focus, other regions' directions are skipped and focused ones run every time.
        directions = {k: d for k, d in directions.items() if not d.region or d.region in focus}
    for d in directions.values():
        d.weight *= cfg.boost_regions.get(d.region or "", 1.0)
    core = [k for k in cfg.core_directions if k in directions]
    core += [k for k, d in directions.items() if focus and d.region in focus and k not in core]
    stats = direction_stats(ctx.db, ctx.cfg)
    return core + pick_directions(directions, stats, cfg.random_directions, cfg.epsilon, ctx.rng, exclude=set(core))
