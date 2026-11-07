"""Склейка дублей: одна и та же идея из разных постов становится кластером
с числом упоминаний — это сильный сигнал спроса и он поднимает балл.

Кандидатов ищет лексический индекс (дёшево), спорные пары решает ИИ-судья.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..core.dates import utcnow
from ..llm import BudgetExceeded, LLMError
from ..llm.schemas import MergeOut
from ..core.scoring import final_score
from ..core.selection import chunks
from ..core.similarity import SimIndex

if TYPE_CHECKING:
    from ..context import Ctx


def idea_text(r: dict[str, Any]) -> str:
    return f"{r['title']}. {r.get('one_liner') or ''}"


def resolve(ctx: "Ctx", idea_id: int) -> int:
    seen = set()
    while idea_id not in seen:
        seen.add(idea_id)
        row = ctx.db.one("SELECT cluster_id FROM ideas WHERE id=?", (idea_id,))
        if not row or row["cluster_id"] in (None, idea_id):
            return idea_id
        idea_id = row["cluster_id"]
    return idea_id


def refresh_mentions(ctx: "Ctx") -> None:
    bonus = ctx.cfg.scoring.mention_bonus
    rows = ctx.db.query(
        "SELECT c.id, c.base_score, c.mentions, (SELECT COUNT(*) FROM ideas m WHERE m.cluster_id=c.id) AS n"
        " FROM ideas c WHERE c.cluster_id=c.id")
    upd = [(r["n"], final_score(r["base_score"], r["n"], bonus), r["id"]) for r in rows if r["n"] != r["mentions"]]
    if upd:
        ctx.db.executemany("UPDATE ideas SET mentions=?, score=? WHERE id=?", upd)


def run_dedup(ctx: "Ctx") -> None:
    cfg = ctx.cfg.dedup
    db = ctx.db
    new = db.query("SELECT id, title, one_liner, sphere, audience FROM ideas"
                   " WHERE dedup_done=0 AND classified_at IS NOT NULL ORDER BY id")
    if not new:
        return
    canon = db.query("SELECT id, title, one_liner, sphere, audience FROM ideas"
                     " WHERE dedup_done=1 AND cluster_id=id")
    rows = {r["id"]: r for r in canon + new}
    index = SimIndex([idea_text(r) for r in canon + new])
    for r in canon:
        index.add(r["id"], idea_text(r))
    system = ctx.prompt("merge")
    now = utcnow()
    stop = False

    for chunk in chunks(new, 10):
        if stop:
            break
        decisions: dict[int, int | None] = {}
        judge: list[tuple[dict, list[tuple[int, float]]]] = []
        for r in chunk:
            cands = [(cid, s) for cid, s in index.query(idea_text(r), top=5, exclude=r["id"])
                     if s >= cfg.candidate_threshold]
            if cands and cands[0][1] >= cfg.auto_merge_threshold:
                decisions[r["id"]] = cands[0][0]
                ctx.stat("dedup", "auto_merged")
                continue
            # Пока решения нет — идея временно считается самостоятельной и сама
            # может стать кандидатом для следующих идей в этой же пачке
            index.add(r["id"], idea_text(r))
            if cands and cfg.llm_judge and ctx.llm.available:
                judge.append((r, cands))
            else:
                decisions[r["id"]] = None

        if judge:
            payload = [{
                "id": r["id"], "idea": idea_text(r), "audience": r.get("audience"),
                "candidates": [{"id": cid, "idea": idea_text(rows[cid]), "audience": rows[cid].get("audience")}
                               for cid, _ in cands],
            } for r, cands in judge]
            allowed = {r["id"]: {cid for cid, _ in cands} for r, cands in judge}
            try:
                out = ctx.llm.json("merge", system, json.dumps(payload, ensure_ascii=False), MergeOut,
                                   max_tokens=40 * len(judge) + 100, temperature=0)
                for d in out.decisions:
                    if d.id in allowed:
                        decisions[d.id] = d.same_as if d.same_as in allowed[d.id] else None
                for r, _ in judge:
                    decisions.setdefault(r["id"], None)
            except BudgetExceeded:
                stop = True  # спорные идеи подождут следующего прогона
                ctx.stat("dedup", "budget_stop")
            except LLMError as e:
                ctx.log.warning("[dedup] судья недоступен: %s", e)
                for r, _ in judge:
                    decisions[r["id"]] = None

        with db.tx():
            for r in chunk:
                if r["id"] not in decisions:
                    continue
                target = decisions[r["id"]]
                if target is None:
                    db.execute("UPDATE ideas SET cluster_id=id, dedup_done=1, updated_at=? WHERE id=?", (now, r["id"]))
                    ctx.stat("dedup", "unique")
                    continue
                root = resolve(ctx, target)
                if root == r["id"]:
                    db.execute("UPDATE ideas SET cluster_id=id, dedup_done=1 WHERE id=?", (r["id"],))
                    continue
                # Всё, что успело прицепиться к этой идее, переезжает к корню кластера
                db.execute("UPDATE ideas SET cluster_id=?, updated_at=? WHERE cluster_id=?", (root, now, r["id"]))
                db.execute("UPDATE ideas SET cluster_id=?, dedup_done=1, updated_at=? WHERE id=?", (root, now, r["id"]))
                index.remove(r["id"])
                ctx.stat("dedup", "merged")
    refresh_mentions(ctx)
