"""Merging duplicates. The same idea found in several posts becomes one cluster, and the
number of mentions is a strong demand signal that raises its score. A lexical index
finds candidates cheaply; an LLM judge settles the borderline pairs."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..core.selection import chunks
from ..core.similarity import SimIndex
from ..llm import BudgetExceeded, LLMError
from ..llm.schemas import MergeOut
from ..storage.db import Row
from ..storage.ideas import (cluster_root, cluster_roots, mark_unique, merge_into, pending_dedup,
                             refresh_mentions)

if TYPE_CHECKING:
    from ..context import Ctx

BATCH = 10
CANDIDATES = 5

Candidates = list[tuple[int, float]]
Decisions = dict[int, int | None]


def idea_text(r: Row) -> str:
    return f"{r['title']}. {r.get('one_liner') or ''}"


def run_dedup(ctx: "Ctx") -> None:
    new = pending_dedup(ctx.db)
    if not new:
        return
    roots = cluster_roots(ctx.db)
    known = {r["id"]: r for r in roots + new}
    index = SimIndex([idea_text(r) for r in roots + new])
    for r in roots:
        index.add(r["id"], idea_text(r))
    system = ctx.prompt("merge")
    for batch in chunks(new, BATCH):
        decisions, disputed = _match(ctx, batch, index)
        verdicts = _judge(ctx, system, disputed, known) if disputed else {}
        _apply(ctx, batch, decisions | (verdicts or {}), index)
        if verdicts is None:
            break
    refresh_mentions(ctx.db, ctx.cfg.scoring.mention_bonus)


def _match(ctx: "Ctx", batch: list[Row], index: SimIndex) -> tuple[Decisions, list[tuple[Row, Candidates]]]:
    cfg = ctx.cfg.dedup
    decisions: Decisions = {}
    disputed: list[tuple[Row, Candidates]] = []
    for r in batch:
        candidates = [(cid, score) for cid, score in index.query(idea_text(r), top=CANDIDATES, exclude=r["id"])
                      if score >= cfg.candidate_threshold]
        if candidates and candidates[0][1] >= cfg.auto_merge_threshold:
            decisions[r["id"]] = candidates[0][0]
            ctx.stat("dedup", "auto_merged")
            continue
        # Until judged, the idea counts as standalone and may match later ideas of the batch.
        index.add(r["id"], idea_text(r))
        if candidates and cfg.llm_judge and ctx.llm.available:
            disputed.append((r, candidates))
        else:
            decisions[r["id"]] = None
    return decisions, disputed


def _judge(ctx: "Ctx", system: str, disputed: list[tuple[Row, Candidates]], known: dict[int, Row]) -> Decisions | None:
    """The judge's verdicts, or None when the budget ran out and disputed ideas must wait."""
    payload = [{
        "id": r["id"], "idea": idea_text(r), "audience": r.get("audience"),
        "candidates": [{"id": cid, "idea": idea_text(known[cid]), "audience": known[cid].get("audience")}
                       for cid, _ in candidates],
    } for r, candidates in disputed]
    verdicts: Decisions = {r["id"]: None for r, _ in disputed}
    try:
        out = ctx.llm.json("merge", system, json.dumps(payload, ensure_ascii=False), MergeOut,
                           max_tokens=40 * len(disputed) + 100, temperature=0)
    except BudgetExceeded:
        ctx.stat("dedup", "budget_stop")
        return None
    except LLMError as e:
        ctx.log.warning("[dedup] судья недоступен: %s", e)
        return verdicts
    allowed = {r["id"]: {cid for cid, _ in candidates} for r, candidates in disputed}
    for d in out.decisions:
        if d.id in allowed:
            verdicts[d.id] = d.same_as if d.same_as in allowed[d.id] else None
    return verdicts


def _apply(ctx: "Ctx", batch: list[Row], decisions: Decisions, index: SimIndex) -> None:
    with ctx.db.tx():
        for r in batch:
            if r["id"] not in decisions:
                continue
            target = decisions[r["id"]]
            root = r["id"] if target is None else cluster_root(ctx.db, target)
            if root == r["id"]:
                mark_unique(ctx.db, r["id"])
                if target is None:
                    ctx.stat("dedup", "unique")
                continue
            merge_into(ctx.db, r["id"], root)
            index.remove(r["id"])
            ctx.stat("dedup", "merged")
