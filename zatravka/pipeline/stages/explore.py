"""LLM explorer: every few runs it proposes new search directions based on what already
produced good ideas, plus random seeds for variety. Weak LLM directions get disabled."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...config import Direction, Explore
from ...llm.schemas import ExploreDirection, ExploreOut
from ...storage.directions import (add_direction, direction_stats, disable_directions, enabled_llm_directions,
                                  load_directions)
from ...storage.ideas import best_ideas, sphere_summary

if TYPE_CHECKING:
    from ..context import Ctx

# Random seeds push the LLM toward territory it would not think of on its own.
SEEDS = [
    "пасечники", "ветеринары экзотических животных", "свадебные фотографы", "учителя химии",
    "дальнобойщики", "владельцы аквариумов", "органические фермеры", "архитекторы", "варгеймеры",
    "люди с хроническими болезнями", "клининговые компании", "перевозка животных", "косплееры",
    "реставраторы мебели", "пилоты-любители", "экспаты на пенсии", "барберы", "тату-мастера",
    "спортивные судьи", "сиделки", "диетологи", "музыкальные школы", "автосервисы", "лесничество",
    "яхтсмены", "библиотекари", "стоматологи", "ремесленные пивоварни", "ювелиры", "переводчики",
    "школьные психологи", "волонтёры приютов", "кондитеры на дому", "владельцы мини-отелей",
    "игроки в шахматы", "моделисты", "фуд-траки", "соревновательный танец", "охота и рыбалка",
    "самостоятельные родители", "люди с аллергией", "дайверы", "уличные художники", "мастера маникюра",
]
SEEDS_PER_RUN = 3
# Without Reddit OAuth every check waits about a minute for the rate limit.
MAX_SUBREDDIT_CHECKS = 5
MIN_LIVE_SUBREDDITS = 2
QUERIES_PER_KIND = 3
SUBREDDIT_NAME = re.compile(r"[A-Za-z0-9_]{2,21}")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (s or "").lower()).strip("_")[:40] or "direction"


def overlapping(subs: list[str], dirs: dict[str, Direction], extra: dict[str, list[str]],
                share: float = 0.5) -> str | None:
    """Key of a direction sharing more than `share` of the proposed subreddits."""
    mine = {s.lower() for s in subs}
    if not mine:
        return None
    pools = {k: d.subreddits for k, d in dirs.items()} | extra
    for key, theirs in pools.items():
        if len(mine & {s.lower() for s in theirs}) / len(mine) > share:
            return key
    return None


def should_explore(ctx: "Ctx") -> bool:
    cfg = ctx.cfg.explore
    if not cfg.enabled or not ctx.run_id:
        return False
    return ctx.run_id - (ctx.db.kv_get("explore_last_run_id", 0) or 0) >= cfg.every_n_runs


def run_explore(ctx: "Ctx") -> list[str]:
    cfg = ctx.cfg.explore
    dirs = load_directions(ctx.db, enabled_only=False)
    out = ctx.llm.json("explore", ctx.prompt("explore", count=cfg.new_directions), _brief(ctx, dirs), ExploreOut,
                       max_tokens=2500, temperature=0.9)
    created: dict[str, list[str]] = {}
    for proposal in out.directions[: cfg.new_directions]:
        key = slug(proposal.key)
        if key in dirs or key in created:
            key = f"{key}_{ctx.run_id}"
        subs = _valid_subreddits(proposal.subreddits)
        twin = overlapping(subs, dirs, created)
        if twin:
            ctx.log.info("[explore] направление %s отброшено: сабреддиты почти как у %s", key, twin)
            ctx.stat("explore", "duplicates")
            continue
        subs = _live_subreddits(ctx, subs)
        if len(subs) < MIN_LIVE_SUBREDDITS and not (proposal.x_queries or proposal.threads_queries):
            ctx.log.info("[explore] направление %s отброшено: нет живых сабреддитов", key)
            continue
        add_direction(ctx.db, key, proposal.name or key, _spec(proposal, subs), proposal.rationale)
        created[key] = subs
        ctx.log.info("[explore] новое направление %s: %s", key, proposal.name)
    ctx.db.kv_set("explore_last_run_id", ctx.run_id)
    ctx.stat("explore", "created", len(created))
    return list(created)


def _brief(ctx: "Ctx", dirs: dict[str, Direction]) -> str:
    stats = direction_stats(ctx.db, ctx.cfg)
    directions = []
    for key, d in dirs.items():
        s = stats.get(key, {})
        directions.append(f"- {key} ({d.name}): сабреддиты {', '.join(d.subreddits[:8])};"
                          f" постов {int(s.get('posts', 0))}, прошли триаж {int(s.get('triaged', 0))},"
                          f" хороших идей {int(s.get('good', 0))}")
    spheres = [f"- {r['sphere']}: идей {r['n']}, средний балл {r['avg']}" for r in sphere_summary(ctx.db)]
    top = [f"- [{r['sphere']}, {r['score']}] {r['title']}" for r in best_ideas(ctx.db)]
    seeds = ", ".join(ctx.rng.sample(SEEDS, SEEDS_PER_RUN))
    return "\n".join([
        "Существующие направления:", *directions, "",
        "Сферы по среднему баллу идей:", *(spheres or ["- пока нет данных"]), "",
        "Лучшие идеи:", *(top or ["- пока нет"]), "",
        f"Случайные затравки для неочевидных направлений (используй хотя бы одну): {seeds}",
    ])


def _valid_subreddits(names: list[str]) -> list[str]:
    cleaned = (re.sub(r"^/?r/", "", s).strip("/ ") for s in names)
    return [s for s in cleaned if SUBREDDIT_NAME.fullmatch(s)]


def _live_subreddits(ctx: "Ctx", subs: list[str]) -> list[str]:
    reddit = ctx.sources.get("reddit")
    if not reddit or not reddit.status().ok:
        return subs
    return [s for s in subs[:MAX_SUBREDDIT_CHECKS] if reddit.subreddit_exists(s) is not False]


def _spec(proposal: ExploreDirection, subs: list[str]) -> dict[str, object]:
    return {"subreddits": subs, "reddit_search": proposal.reddit_search[:QUERIES_PER_KIND],
            "x_queries": proposal.x_queries[:QUERIES_PER_KIND],
            "threads_queries": proposal.threads_queries[:QUERIES_PER_KIND], "search_scope": "subs"}


def prune_directions(ctx: "Ctx") -> list[str]:
    stats = direction_stats(ctx.db, ctx.cfg)
    weak = [key for key in enabled_llm_directions(ctx.db) if _is_weak(stats.get(key, {}), ctx.cfg.explore)]
    if weak:
        disable_directions(ctx.db, weak)
        ctx.log.info("[explore] отключены слабые направления: %s", ", ".join(weak))
        ctx.stat("explore", "pruned", len(weak))
    return weak


def _is_weak(stats: dict[str, float], cfg: Explore) -> bool:
    triaged = stats.get("triaged", 0)
    return triaged >= cfg.prune_min_triaged and stats.get("value", 0) / triaged < cfg.prune_min_yield
