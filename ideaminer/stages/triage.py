"""Triage: a cheap LLM pass over batches of posts, scoring whether a post can yield an idea."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.selection import chunks, select_diverse
from ..core.text import truncate
from ..llm.schemas import TriageOut
from ..storage.db import dumps
from ..storage.posts import random_candidates, save_triage, top_candidates
from . import parallel

if TYPE_CHECKING:
    from ..context import Ctx

PLATFORM_SHARE = 0.5
POOL_FACTOR = 4
NOTE_LENGTH = 300


def format_post(r: dict[str, Any], max_chars: int) -> str:
    meta = [r["platform"]]
    if r.get("community"):
        meta.append(f"r/{r['community']}" if r["platform"] == "reddit" else r["community"])
    if r.get("score") is not None:
        meta.append(f"рейтинг {r['score']}")
    if r.get("num_comments"):
        meta.append(f"комментариев {r['num_comments']}")
    if r.get("created_at"):
        meta.append(r["created_at"][:10])
    lines = [f"### id={r['id']} | " + " | ".join(meta)]
    if r.get("title"):
        lines.append(f"Заголовок: {r['title']}")
    if r.get("body"):
        lines.append(truncate(r["body"], max_chars))
    return "\n".join(lines)


def select_posts(ctx: "Ctx", limit: int) -> list[dict[str, Any]]:
    n_random = int(limit * ctx.cfg.triage.random_share)
    pool = top_candidates(ctx.db, ctx.scope, (limit - n_random) * POOL_FACTOR)
    # No platform takes more than half the batch, so fresh X or Threads posts
    # do not drown in the Reddit backlog.
    top = select_diverse(pool, limit - n_random, PLATFORM_SHARE, key="platform")
    # The random share ignores signal phrases: non-obvious ideas often hide in plain posts.
    return top + random_candidates(ctx.db, ctx.scope, n_random, [r["id"] for r in top])


def run_triage(ctx: "Ctx", limit: int | None = None) -> None:
    cfg = ctx.cfg.triage
    posts = select_posts(ctx, limit or cfg.max_posts_per_run)
    if not posts:
        return
    system = ctx.prompt("triage")

    def work(batch: list[dict[str, Any]]) -> TriageOut:
        user = "\n\n".join(format_post(r, cfg.max_body_chars) for r in batch)
        return ctx.llm.json("triage", system, user, TriageOut, max_tokens=120 + 60 * len(batch), temperature=0.2)

    for batch, out, err in parallel(ctx, "triage", chunks(posts, cfg.batch_size), work):
        if err or out is None:
            continue
        by_id = {it.id: it for it in out.items}
        results = []
        for r in batch:
            item = by_id.get(r["id"])
            if not item:
                ctx.stat("triage", "missed")
                continue
            status = "promising" if item.score >= cfg.threshold else "skipped"
            ctx.stat("triage", status)
            results.append((item.score, dumps(item.signals), item.note[:NOTE_LENGTH], status, r["id"]))
        save_triage(ctx.db, results)
    ctx.log.info("[triage] %s", ctx.stats.get("triage"))
