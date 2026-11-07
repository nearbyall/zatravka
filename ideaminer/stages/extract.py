"""Извлечение идей из перспективных постов, включая неочевидные (lateral)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..db import loads
from ..core.dates import utcnow
from ..llm import BudgetExceeded
from ..llm.schemas import ExtractOut
from ..core.selection import select_diverse
from ..core.text import truncate, verify_quote
from . import parallel

if TYPE_CHECKING:
    from ..context import Ctx

def post_context(r: dict[str, Any], max_body: int, max_comments: int) -> str:
    meta = f"Платформа: {r['platform']}"
    if r.get("community"):
        meta += f", сообщество: {r['community']}"
    if r.get("score") is not None:
        meta += f", рейтинг: {r['score']}"
    if r.get("num_comments"):
        meta += f", комментариев: {r['num_comments']}"
    parts = [meta]
    if r.get("title"):
        parts.append(f"Заголовок: {r['title']}")
    parts.append("Текст поста:\n" + (truncate(r.get("body") or "", max_body) or "(без текста)"))
    comments = loads(r.get("comments_json"), []) or []
    if comments:
        lines, used = [], 0
        for c in comments:
            body = truncate(c.get("body", ""), 600)
            if used + len(body) > max_comments:
                break
            used += len(body)
            score = f" [{c['score']}]" if c.get("score") is not None else ""
            lines.append(f"- {body}{score}")
        parts.append("Лучшие комментарии:\n" + "\n".join(lines))
    if r.get("triage_note"):
        parts.append(f"Пометка скаута: {r['triage_note']}")
    return "\n\n".join(parts)


def source_text(r: dict[str, Any]) -> str:
    comments = loads(r.get("comments_json"), []) or []
    return "\n".join([r.get("title") or "", r.get("body") or ""] + [c.get("body", "") for c in comments])


def run_extract(ctx: "Ctx", limit: int | None = None) -> None:
    cfg = ctx.cfg.extract
    limit = limit or cfg.max_posts_per_run
    scope, params = ctx.scope.sql()
    pool = ctx.db.query(
        f"SELECT * FROM posts WHERE status='promising'{scope}"
        " ORDER BY triage_score DESC, prefilter_score DESC LIMIT ?", (*params, limit * 5))
    rows = select_diverse(pool, limit, cfg.max_share_per_direction)
    if not rows:
        return
    system = ctx.prompt("extract", max_ideas=cfg.max_ideas_per_post)

    def work(r: dict[str, Any]) -> ExtractOut:
        user = post_context(r, cfg.max_body_chars, cfg.max_comment_chars)
        return ctx.llm.json("extract", system, user, ExtractOut, max_tokens=900 * cfg.max_ideas_per_post,
                            temperature=0.7)

    for r, out, err in parallel(ctx, "extract", rows, work):
        if isinstance(err, BudgetExceeded):
            continue
        if err or out is None:
            # Первая ошибка — пост остаётся в очереди, вторая — помечается как сбойный
            status = "error" if r.get("error") else "promising"
            ctx.db.execute("UPDATE posts SET error=?, status=? WHERE id=?", (str(err)[:500], status, r["id"]))
            continue
        save_ideas(ctx, r, out)


def save_ideas(ctx: "Ctx", post: dict[str, Any], out: ExtractOut) -> int:
    now = utcnow()
    src = source_text(post)
    n = 0
    with ctx.db.tx():
        for idea in out.ideas[: ctx.cfg.extract.max_ideas_per_post]:
            if not idea.title.strip():
                continue
            cur = ctx.db.execute(
                "INSERT INTO ideas(created_at, updated_at, run_id, title, one_liner, problem, solution, audience,"
                " insight_type, reasoning, why_now, monetization, risks, confidence)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, now, ctx.run_id, idea.title[:200], idea.one_liner, idea.problem, idea.solution,
                 idea.audience, idea.insight_type, idea.reasoning, idea.why_now, idea.monetization,
                 idea.risks, idea.confidence),
            )
            ctx.db.execute(
                "INSERT OR IGNORE INTO idea_evidence(idea_id, post_id, quote, quote_verified) VALUES(?,?,?,?)",
                (cur.lastrowid, post["id"], idea.evidence[:1000], int(verify_quote(idea.evidence, src))),
            )
            n += 1
            ctx.stat("extract", f"type_{idea.insight_type}")
        ctx.db.execute("UPDATE posts SET status='extracted', processed_at=?, error=NULL WHERE id=?",
                       (now, post["id"]))
    ctx.stat("extract", "posts")
    ctx.stat("extract", "ideas", n)
    if not n:
        ctx.stat("extract", "empty_posts")
    return n
