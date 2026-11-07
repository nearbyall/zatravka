"""Classification of ideas: market (B2B, B2C…), sphere, digital or physical, business model,
regions, tags and sub-scores. The final score is computed in code (core.scoring)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..config import Scoring
from ..core.scoring import base_score, final_score
from ..core.selection import chunks
from ..core.taxonomy import Taxonomy, normalize_key, resolve_taxonomy
from ..llm.schemas import ClassItem, ClassifyOut
from ..storage.db import Row
from ..storage.ideas import first_source, save_classification, unclassified_ideas
from . import parallel

if TYPE_CHECKING:
    from ..context import Ctx

PROMPT_FIELDS = ("id", "title", "one_liner", "problem", "solution", "audience")
TOKENS_PER_IDEA = 300
MAX_SECONDARY_SPHERES = 2
MAX_REGIONS = 3
MAX_TAGS = 6
TAG_LENGTH = 40
NOTE_LENGTH = 400


def _options(d: dict[str, str]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in d.items())


def run_classify(ctx: "Ctx", limit: int = 500) -> None:
    cfg = ctx.cfg
    tax = resolve_taxonomy(cfg)
    version = cfg.taxonomy.version()
    ideas = unclassified_ideas(ctx.db, version, limit)
    if not ideas:
        return
    system = ctx.prompt("classify", markets=_options(tax["markets"]), spheres=_options(tax["spheres"]),
                        natures=_options(tax["natures"]), business_models=_options(tax["business_models"]),
                        regions=_options(tax["regions"]))

    def work(batch: list[Row]) -> ClassifyOut:
        payload = [{**{k: r[k] for k in PROMPT_FIELDS}, "source": first_source(ctx.db, r["id"])} for r in batch]
        return ctx.llm.json("classify", system, json.dumps(payload, ensure_ascii=False), ClassifyOut,
                            max_tokens=TOKENS_PER_IDEA * len(batch), temperature=0.1)

    for batch, out, err in parallel(ctx, "classify", chunks(ideas, cfg.classify.batch_size), work):
        if err or out is None:
            continue
        by_id = {it.id: it for it in out.items}
        with ctx.db.tx():
            for idea in batch:
                item = by_id.get(idea["id"])
                if not item:
                    ctx.stat("classify", "missed")
                    continue
                save_classification(ctx.db, idea["id"], labels(item, idea["mentions"] or 1, tax, cfg.scoring), version)
                ctx.stat("classify", "ideas")


def labels(item: ClassItem, mentions: int, tax: Taxonomy, scoring: Scoring) -> dict[str, Any]:
    sphere = normalize_key(item.sphere, tax["spheres"], "other")
    secondary = [s for s in (normalize_key(x, tax["spheres"]) for x in item.secondary_spheres) if s and s != sphere]
    base = base_score(item.scores, scoring.weights)
    return {
        "market": normalize_key(item.market.upper(), tax["markets"], "B2C"),
        "sphere": sphere,
        "secondary_spheres": secondary[:MAX_SECONDARY_SPHERES],
        "nature": normalize_key(item.nature, tax["natures"], "digital"),
        "business_model": normalize_key(item.business_model, tax["business_models"], "other"),
        "tags": [t.lower()[:TAG_LENGTH] for t in item.tags][:MAX_TAGS],
        "scores": item.scores,
        "score_note": item.score_note[:NOTE_LENGTH],
        "base_score": base,
        "score": final_score(base, mentions, scoring.mention_bonus),
        "regions": _regions(item.regions, tax["regions"]),
        "origin_region": normalize_key(item.origin_region, tax["regions"], "global"),
    }


def _regions(answer: list[str], options: dict[str, str]) -> list[str]:
    keys = [k for k in (normalize_key(r, options) for r in answer) if k]
    regions = list(dict.fromkeys(keys))[:MAX_REGIONS] or ["global"]
    if "global" in regions and len(regions) > 1:
        regions.remove("global")
    return regions
