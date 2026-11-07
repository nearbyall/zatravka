"""Extraction of ideas from promising posts, including non-obvious (lateral) ones."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.selection import select_diverse
from ..core.text import truncate, verify_quote
from ..llm import BudgetExceeded
from ..llm.schemas import ExtractOut
from ..storage.db import Row, loads
from ..storage.ideas import IDEA_TEXT_COLUMNS, insert_idea
from ..storage.posts import mark_extracted, promising_posts, record_extract_failure
from . import parallel

if TYPE_CHECKING:
    from ..context import Ctx

POOL_FACTOR = 5
COMMENT_CHARS = 600
TITLE_LENGTH = 200
QUOTE_LENGTH = 1000
TOKENS_PER_IDEA = 900


def post_context(r: Row, max_body: int, max_comments: int) -> str:
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
    comments = _comment_lines(loads(r.get("comments_json"), []) or [], max_comments)
    if comments:
        parts.append("Лучшие комментарии:\n" + "\n".join(comments))
    if r.get("triage_note"):
        parts.append(f"Пометка скаута: {r['triage_note']}")
    return "\n\n".join(parts)


def _comment_lines(comments: list[dict[str, Any]], budget: int) -> list[str]:
    lines, used = [], 0
    for c in comments:
        body = truncate(c.get("body", ""), COMMENT_CHARS)
        if used + len(body) > budget:
            break
        used += len(body)
        score = f" [{c['score']}]" if c.get("score") is not None else ""
        lines.append(f"- {body}{score}")
    return lines


def source_text(r: Row) -> str:
    comments = loads(r.get("comments_json"), []) or []
    return "\n".join([r.get("title") or "", r.get("body") or ""] + [c.get("body", "") for c in comments])


def run_extract(ctx: "Ctx", limit: int | None = None) -> None:
    cfg = ctx.cfg.extract
    limit = limit or cfg.max_posts_per_run
    posts = select_diverse(promising_posts(ctx.db, ctx.scope, limit * POOL_FACTOR), limit, cfg.max_share_per_direction)
    if not posts:
        return
    system = ctx.prompt("extract", max_ideas=cfg.max_ideas_per_post)

    def work(r: Row) -> ExtractOut:
        user = post_context(r, cfg.max_body_chars, cfg.max_comment_chars)
        return ctx.llm.json("extract", system, user, ExtractOut, max_tokens=TOKENS_PER_IDEA * cfg.max_ideas_per_post,
                            temperature=0.7)

    for post, out, err in parallel(ctx, "extract", posts, work):
        if isinstance(err, BudgetExceeded):
            continue
        if err or out is None:
            record_extract_failure(ctx.db, post, str(err))
            continue
        save_ideas(ctx, post, out)


def save_ideas(ctx: "Ctx", post: Row, out: ExtractOut) -> int:
    source = source_text(post)
    ideas = [i for i in out.ideas[: ctx.cfg.extract.max_ideas_per_post] if i.title.strip()]
    with ctx.db.tx():
        for idea in ideas:
            fields = idea.model_dump(include=set(IDEA_TEXT_COLUMNS))
            fields["title"] = idea.title[:TITLE_LENGTH]
            insert_idea(ctx.db, ctx.run_id, post["id"], fields, idea.evidence[:QUOTE_LENGTH],
                        verify_quote(idea.evidence, source))
            ctx.stat("extract", f"type_{idea.insight_type}")
        mark_extracted(ctx.db, post["id"])
    ctx.stat("extract", "posts")
    ctx.stat("extract", "ideas", len(ideas))
    if not ideas:
        ctx.stat("extract", "empty_posts")
    return len(ideas)
