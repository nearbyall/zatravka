"""Enrichment: top comments for the most promising posts. Comments often carry the real
signal: "same here", "I pay for X, but…"."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.models import Comment
from ...storage.db import Row
from ...storage.posts import promising_posts, save_comments

if TYPE_CHECKING:
    from ..context import Ctx


def run_enrich(ctx: "Ctx", limit: int | None = None) -> None:
    n = limit if limit is not None else ctx.cfg.sources.reddit.comments_for_top_posts
    if n <= 0:
        return
    for post in promising_posts(ctx.db, ctx.scope, n, without_comments=True):
        comments = _fetch_comments(ctx, post)
        save_comments(ctx.db, post["id"], comments)
        ctx.stat("enrich", "posts")
        ctx.stat("enrich", "comments", len(comments))


def _fetch_comments(ctx: "Ctx", post: Row) -> list[Comment]:
    source = ctx.sources.get(post["platform"])
    if not source or not source.status().ok:
        return []
    try:
        return source.comments(post)
    except Exception as e:  # noqa: BLE001 - the post is still worth extracting without comments
        ctx.log.warning("[enrich] комментарии к %s: %s", post["url"], e)
        ctx.stat("enrich", "errors")
        return []
