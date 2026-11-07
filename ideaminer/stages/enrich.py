"""Обогащение: лучшие комментарии к самым перспективным постам.
В комментариях часто сидит главное — «у меня то же самое», «я плачу за X, но…»."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..db import dumps

if TYPE_CHECKING:
    from ..context import Ctx


def run_enrich(ctx: "Ctx", limit: int | None = None) -> None:
    n = limit if limit is not None else ctx.cfg.sources.reddit.comments_for_top_posts
    if n <= 0:
        return
    scope, params = ctx.scope.sql()
    rows = ctx.db.query(
        f"SELECT * FROM posts WHERE status='promising' AND comments_json IS NULL{scope}"
        " ORDER BY triage_score DESC, prefilter_score DESC LIMIT ?", (*params, n))
    for r in rows:
        src = ctx.sources.get(r["platform"])
        comments = []
        if src and src.status()[0]:
            try:
                comments = src.comments(r)
            except Exception as e:  # noqa: BLE001 — без комментариев пост всё равно обработаем
                ctx.log.warning("[enrich] комментарии к %s: %s", r["url"], e)
                ctx.stat("enrich", "errors")
        ctx.db.execute("UPDATE posts SET comments_json=? WHERE id=?",
                       (dumps([c.__dict__ for c in comments]), r["id"]))
        ctx.stat("enrich", "posts")
        ctx.stat("enrich", "comments", len(comments))
