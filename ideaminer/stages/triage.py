"""Триаж: дешёвая ИИ-оценка пачек постов — есть ли в посте материал для идеи."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..db import dumps
from ..llm.schemas import TriageOut
from ..core.selection import chunks, select_diverse
from ..core.text import truncate
from . import parallel

if TYPE_CHECKING:
    from ..context import Ctx

COLUMNS = "id, platform, community, title, body, score, num_comments, created_at, lang, direction"


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
    cfg = ctx.cfg.triage
    n_rand = int(limit * cfg.random_share)
    scope, params = ctx.scope.sql()
    pool = ctx.db.query(
        f"SELECT {COLUMNS} FROM posts WHERE status='candidate'{scope}"
        " ORDER BY prefilter_score DESC, created_at DESC LIMIT ?", (*params, (limit - n_rand) * 4))
    # Ни одна площадка не забирает больше половины очереди: свежие посты X или Threads
    # не должны тонуть в накопленном хвосте Reddit
    top = select_diverse(pool, limit - n_rand, 0.5, key="platform")
    seen = [r["id"] for r in top]
    # Случайная доля — посты без сигнальных фраз: неочевидные идеи часто там
    rand = ctx.db.query(
        f"SELECT {COLUMNS} FROM posts WHERE status='candidate'{scope}"
        f" AND id NOT IN ({','.join('?' * len(seen)) or 'NULL'}) ORDER BY RANDOM() LIMIT ?",
        (*params, *seen, n_rand),
    ) if n_rand else []
    return top + rand


def run_triage(ctx: "Ctx", limit: int | None = None) -> None:
    cfg = ctx.cfg.triage
    posts = select_posts(ctx, limit or cfg.max_posts_per_run)
    if not posts:
        return
    system = ctx.prompt("triage")

    def work(batch: list[dict[str, Any]]) -> TriageOut:
        user = "\n\n".join(format_post(r, cfg.max_body_chars) for r in batch)
        return ctx.llm.json("triage", system, user, TriageOut,
                            max_tokens=120 + 60 * len(batch), temperature=0.2)

    for batch, out, err in parallel(ctx, "triage", chunks(posts, cfg.batch_size), work):
        if err or out is None:
            continue
        by_id = {it.id: it for it in out.items}
        updates = []
        for r in batch:
            it = by_id.get(r["id"])
            if not it:
                ctx.stat("triage", "missed")
                continue
            status = "promising" if it.score >= cfg.threshold else "skipped"
            ctx.stat("triage", status)
            updates.append((it.score, dumps(it.signals), it.note[:300], status, r["id"]))
        ctx.db.executemany(
            "UPDATE posts SET triage_score=?, triage_signals=?, triage_note=?, status=? WHERE id=?", updates)
    ctx.log.info("[triage] %s", ctx.stats.get("triage"))
