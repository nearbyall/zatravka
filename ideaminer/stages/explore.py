"""ИИ-исследователь: раз в несколько прогонов предлагает новые направления
поиска на основе того, что уже дало хорошие идеи, плюс случайная затравка
для разнообразия. Слабые ИИ-направления со временем отключаются."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..db import dumps, utcnow
from ..llm.schemas import ExploreOut
from ..planner import direction_stats, load_directions

if TYPE_CHECKING:
    from ..context import Ctx

# Случайные затравки: толкают ИИ в территории, о которых он сам бы не подумал
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


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (s or "").lower()).strip("_")[:40] or "direction"


def overlapping(subs: list[str], dirs: dict, extra: dict[str, list[str]], share: float = 0.5) -> str | None:
    """Ключ направления, с которым у предложения совпадает больше share сабреддитов."""
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
    last = ctx.db.kv_get("explore_last_run_id", 0) or 0
    return ctx.run_id - last >= cfg.every_n_runs


def run_explore(ctx: "Ctx") -> list[str]:
    cfg, db = ctx.cfg, ctx.db
    stats = direction_stats(db, cfg)
    dirs = load_directions(db, enabled_only=False)
    lines = []
    for key, d in dirs.items():
        s = stats.get(key, {})
        lines.append(f"- {key} ({d.name}): сабреддиты {', '.join(d.subreddits[:8])}; постов {int(s.get('posts', 0))},"
                     f" прошли триаж {int(s.get('triaged', 0))}, хороших идей {int(s.get('good', 0))}")
    spheres = db.query(
        "SELECT sphere, COUNT(*) AS n, ROUND(AVG(score), 2) AS avg FROM ideas"
        " WHERE cluster_id=id AND sphere IS NOT NULL GROUP BY sphere ORDER BY avg DESC LIMIT 12")
    top = db.query("SELECT title, sphere, score FROM ideas WHERE cluster_id=id AND score IS NOT NULL"
                   " ORDER BY score DESC LIMIT 12")
    seeds = ctx.rng.sample(SEEDS, 3)
    sphere_lines = [f"- {r['sphere']}: идей {r['n']}, средний балл {r['avg']}" for r in spheres]
    top_lines = [f"- [{r['sphere']}, {r['score']}] {r['title']}" for r in top]
    user = "\n".join([
        "Существующие направления:", *lines, "",
        "Сферы по среднему баллу идей:", *(sphere_lines or ["- пока нет данных"]), "",
        "Лучшие идеи:", *(top_lines or ["- пока нет"]), "",
        f"Случайные затравки для неочевидных направлений (используй хотя бы одну): {', '.join(seeds)}",
    ])
    out = ctx.llm.json("explore", ctx.prompt("explore", count=cfg.explore.new_directions), user, ExploreOut,
                       max_tokens=2500, temperature=0.9)
    reddit = ctx.sources.get("reddit")
    created: list[str] = []
    created_specs: dict[str, list[str]] = {}
    now = utcnow()
    for d in out.directions[: cfg.explore.new_directions]:
        key = slug(d.key)
        if key in dirs or key in created:
            key = f"{key}_{ctx.run_id}"
        subs = [re.sub(r"^/?r/", "", s).strip("/ ") for s in d.subreddits]
        subs = [s for s in subs if re.fullmatch(r"[A-Za-z0-9_]{2,21}", s)]
        twin = overlapping(subs, dirs, created_specs)
        if twin:
            # Тот же набор сообществ под новым названием ничего не добавит — только съест прогоны
            ctx.log.info("[explore] направление %s отброшено: сабреддиты почти как у %s", key, twin)
            ctx.stat("explore", "duplicates")
            continue
        if reddit and reddit.status()[0]:
            checked = []
            for s in subs[:5]:  # без OAuth каждая проверка стоит ~минуту ожидания лимита
                ok = reddit.subreddit_exists(s)
                if ok is not False:
                    checked.append(s)
            subs = checked
        if len(subs) < 2 and not (d.x_queries or d.threads_queries):
            ctx.log.info("[explore] направление %s отброшено: нет живых сабреддитов", key)
            continue
        spec = {"subreddits": subs, "reddit_search": d.reddit_search[:3], "x_queries": d.x_queries[:3],
                "threads_queries": d.threads_queries[:3], "search_scope": "subs"}
        db.execute(
            "INSERT INTO directions(key, name, origin, enabled, weight, spec_json, rationale, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (key, d.name or key, "llm", 1, 1.0, dumps(spec), d.rationale, now, now),
        )
        created.append(key)
        created_specs[key] = subs
        ctx.log.info("[explore] новое направление %s: %s", key, d.name)
    db.kv_set("explore_last_run_id", ctx.run_id)
    ctx.stat("explore", "created", len(created))
    return created


def prune_directions(ctx: "Ctx") -> list[str]:
    cfg = ctx.cfg.explore
    stats = direction_stats(ctx.db, ctx.cfg)
    pruned = []
    for r in ctx.db.query("SELECT key FROM directions WHERE origin='llm' AND enabled=1"):
        s = stats.get(r["key"], {})
        triaged = s.get("triaged", 0)
        if triaged >= cfg.prune_min_triaged and s.get("value", 0) / triaged < cfg.prune_min_yield:
            ctx.db.execute("UPDATE directions SET enabled=0, updated_at=? WHERE key=?", (utcnow(), r["key"]))
            pruned.append(r["key"])
    if pruned:
        ctx.log.info("[explore] отключены слабые направления: %s", ", ".join(pruned))
        ctx.stat("explore", "pruned", len(pruned))
    return pruned
