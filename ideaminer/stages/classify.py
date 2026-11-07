"""Классификация идей: рынок (B2B/B2C/…), сфера, цифровое/физическое, модель
монетизации, теги и оценки. Итоговый балл считает код, а не модель: так он
стабилен и пересчитывается при смене весов в конфиге."""
from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

from ..db import dumps, loads, utcnow
from ..llm.schemas import ClassifyOut, SCORE_KEYS
from . import chunks, parallel

if TYPE_CHECKING:
    from ..config import Config
    from ..context import Ctx

DEFAULT_TAXONOMY = {
    "markets": {"B2B": "Бизнесу", "B2C": "Потребителю", "B2B2C": "Через бизнес к потребителю",
                "C2C": "Между людьми", "B2G": "Государству"},
    "natures": {"digital": "Цифровой продукт", "physical": "Физический товар",
                "hybrid": "Физическое + цифровое", "service": "Услуга"},
    "business_models": {"saas": "SaaS", "subscription": "Подписка", "freemium": "Freemium",
                        "marketplace": "Маркетплейс", "transactional": "Разовая продажа", "ads": "Реклама",
                        "hardware": "Устройства", "consumables": "Расходники", "service": "Услуги",
                        "licensing": "Лицензирование", "data_api": "Данные/API", "community": "Сообщество",
                        "other": "Другое"},
    "spheres": {"games": "Игры", "beauty": "Красота и косметика", "business_ops": "Бизнес-операции",
                "health": "Здоровье", "education": "Образование", "finance": "Финансы",
                "ecommerce": "E-commerce", "dev_tools": "Инструменты разработчика", "other": "Другое"},
    "regions": {"global": "Глобально", "north_america": "Северная Америка", "latam": "Латинская Америка",
                "eu": "ЕС и Европа", "cis": "СНГ", "east_europe": "Восточная Европа вне ЕС",
                "mena": "Ближний Восток", "africa": "Африка", "south_asia": "Южная Азия",
                "east_asia": "Восточная Азия", "oceania": "Океания"},
}


def taxonomy(cfg: "Config") -> dict[str, dict[str, str]]:
    t = cfg.taxonomy
    return {name: getattr(t, name) or DEFAULT_TAXONOMY[name] for name in DEFAULT_TAXONOMY}


def normalize_key(value: str, options: dict[str, str], fallback: str | None = None) -> str | None:
    v = (value or "").strip()
    if not v:
        return fallback
    low = {k.lower(): k for k in options}
    if v.lower() in low:
        return low[v.lower()]
    for k, label in options.items():
        low_label = label.lower()
        # Модель иногда отвечает подписью вместо ключа: «СНГ», «ЕС и остальная Европа», «Игры и гейминг»
        if v.lower() in (low_label, low_label.split(" (")[0]) or v.lower() in low_label.split(","):
            return k
    return fallback


def base_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    total_w = sum(weights.get(k, 0) for k in SCORE_KEYS) or 1.0
    return round(sum(scores.get(k, 5) * weights.get(k, 0) for k in SCORE_KEYS) / total_w, 2)


def final_score(base: float | None, mentions: int, bonus: float) -> float | None:
    if base is None:
        return None
    return round(base + bonus * math.log2(max(1, mentions)), 2)


def _block(d: dict[str, str]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in d.items())


def idea_source(ctx: "Ctx", idea_id: int) -> dict[str, Any]:
    """Откуда идея: площадка, сообщество, язык, подсказка региона, цитата — для определения региона."""
    r = ctx.db.one(
        "SELECT p.platform, p.community, p.lang, p.region_hint, e.quote FROM idea_evidence e"
        " JOIN posts p ON p.id=e.post_id WHERE e.idea_id=? LIMIT 1", (idea_id,))
    if not r:
        return {}
    return {k: v for k, v in r.items() if v}


def run_classify(ctx: "Ctx", limit: int = 500) -> None:
    """Размечает новые идеи и идеи, размеченные старой таксономией (они остаются видны
    со старыми метками, пока их не переразметят)."""
    cfg = ctx.cfg
    tax = taxonomy(cfg)
    tv = cfg.taxonomy.version()
    rows = ctx.db.query(
        "SELECT id, title, one_liner, problem, solution, audience, insight_type, mentions FROM ideas"
        " WHERE classified_at IS NULL OR taxonomy_version IS NOT ?"
        " ORDER BY classified_at IS NOT NULL, id LIMIT ?", (tv, limit))
    if not rows:
        return
    system = ctx.prompt("classify", markets=_block(tax["markets"]), spheres=_block(tax["spheres"]),
                        natures=_block(tax["natures"]), business_models=_block(tax["business_models"]),
                        regions=_block(tax["regions"]))

    def work(batch: list[dict[str, Any]]) -> ClassifyOut:
        payload = [{**{k: r[k] for k in ("id", "title", "one_liner", "problem", "solution", "audience")},
                    "source": idea_source(ctx, r["id"])} for r in batch]
        return ctx.llm.json("classify", system, json.dumps(payload, ensure_ascii=False), ClassifyOut,
                            max_tokens=300 * len(batch), temperature=0.1)

    for batch, out, err in parallel(ctx, "classify", chunks(rows, cfg.classify.batch_size), work):
        if err or out is None:
            continue
        by_id = {it.id: it for it in out.items}
        now = utcnow()
        with ctx.db.tx():
            for r in batch:
                it = by_id.get(r["id"])
                if not it:
                    ctx.stat("classify", "missed")
                    continue
                sphere = normalize_key(it.sphere, tax["spheres"], "other")
                secondary = [s for s in (normalize_key(x, tax["spheres"]) for x in it.secondary_spheres)
                             if s and s != sphere][:2]
                base = base_score(it.scores, cfg.scoring.weights)
                regions = list(dict.fromkeys(
                    x for x in (normalize_key(g, tax["regions"]) for g in it.regions) if x))[:3] or ["global"]
                if "global" in regions and len(regions) > 1:
                    regions.remove("global")  # «глобально» и конкретные регионы вместе не имеют смысла
                origin = normalize_key(it.origin_region, tax["regions"], "global")
                ctx.db.execute(
                    "UPDATE ideas SET market=?, sphere=?, spheres_json=?, nature=?, business_model=?, tags_json=?,"
                    " scores_json=?, score_note=?, base_score=?, score=?, classified_at=?, taxonomy_version=?,"
                    " regions_json=?, origin_region=?, updated_at=? WHERE id=?",
                    (normalize_key(it.market.upper(), tax["markets"], "B2C"), sphere, dumps(secondary),
                     normalize_key(it.nature, tax["natures"], "digital"),
                     normalize_key(it.business_model, tax["business_models"], "other"),
                     dumps([t.lower()[:40] for t in it.tags][:6]), dumps(it.scores), it.score_note[:400],
                     base, final_score(base, r["mentions"] or 1, cfg.scoring.mention_bonus), now, tv,
                     dumps(regions), origin, now, r["id"]),
                )
                ctx.stat("classify", "ideas")


def rescore_all(ctx: "Ctx") -> int:
    """Пересчёт итоговых баллов после смены весов в конфиге — без ИИ."""
    rows = ctx.db.query("SELECT id, scores_json, mentions FROM ideas WHERE scores_json IS NOT NULL")
    w, bonus = ctx.cfg.scoring.weights, ctx.cfg.scoring.mention_bonus
    updates = []
    for r in rows:
        base = base_score(loads(r["scores_json"], {}), w)
        updates.append((base, final_score(base, r["mentions"] or 1, bonus), r["id"]))
    ctx.db.executemany("UPDATE ideas SET base_score=?, score=? WHERE id=?", updates)
    return len(updates)


def reset_classification(ctx: "Ctx", only_outdated: bool = True) -> int:
    """Помечает идеи к переразметке. Метки сохраняются — идеи не пропадают из выдачи."""
    if only_outdated:
        return int(ctx.db.scalar("SELECT COUNT(*) FROM ideas WHERE taxonomy_version IS NOT ?",
                                 (ctx.cfg.taxonomy.version(),), 0))
    return ctx.db.execute("UPDATE ideas SET taxonomy_version=NULL").rowcount
