"""Finding Threads accounts for a direction.

- discover: the LLM suggests and the browser verifies. LLMs readily invent plausible
  names, so only accounts whose page opened and showed fresh text posts get in;
- expand: no LLM. The pages of the direction's posts show related threads close in
  topic and language, and their authors join the direction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ...core.models import FeedSpec
from ...core.taxonomy import resolve_taxonomy
from ...llm.schemas import AccountsOut
from ...sources.base import SourceLimitReached
from ...storage.accounts import add_account, known_usernames, normalize_username
from ...storage.directions import load_directions
from ...storage.posts import save_comments, unvisited_posts

if TYPE_CHECKING:
    from ...config import Direction
    from ..context import Ctx

FRESH_DAYS = 120


def check_account(ctx: "Ctx", username: str, direction: str | None = None) -> tuple[bool, str]:
    """Opens the profile and decides whether it is worth reading: text posts, the latest one fresh."""
    source = ctx.sources["threads"]
    if "profile" not in source.kinds():
        return False, "Threads сейчас не читается: " + source.status().reason
    try:
        posts = source.fetch(FeedSpec("threads", "profile", username, direction or "wildcard", {"probe": 1}))
    except SourceLimitReached as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001 - one bad account must not stop the search
        return False, f"ошибка: {e}"
    if not posts:
        return False, "нет текстовых постов (нет такого аккаунта, закрыт или только картинки)"
    newest = max((p.created_at for p in posts if p.created_at), default=None)
    if newest and datetime.now(timezone.utc) - newest > timedelta(days=FRESH_DAYS):
        return False, f"давно не пишет (последний пост {newest:%Y-%m-%d})"
    return True, f"{len(posts)} постов, последний {newest:%Y-%m-%d}" if newest else f"{len(posts)} постов"


def discover_accounts(ctx: "Ctx", direction_key: str, count: int = 8) -> list[str]:
    """Asks the LLM for accounts of a direction and registers the ones that pass the check."""
    direction = load_directions(ctx.db, enabled_only=False).get(direction_key)
    if not direction:
        raise ValueError(f"нет направления {direction_key}")
    region = resolve_taxonomy(ctx.cfg)["regions"].get(direction.region or "", "любой")
    system = ctx.prompt("discover_accounts", direction_name=direction.name, about=_about(direction), count=count,
                        region=region)
    out = ctx.llm.json("discover", system, "Предложи аккаунты.", AccountsOut, max_tokens=900, temperature=0.6)
    known = known_usernames(ctx.db, "threads")
    added = []
    for suggestion in out.accounts[:count]:
        name = normalize_username(suggestion.username)
        if not name or name in known:
            continue
        known.add(name)
        ok, why = check_account(ctx, name, direction_key)
        ctx.log.info("[discover] @%s: %s — %s", name, "✓" if ok else "✗", why)
        ctx.stat("discover", "accepted" if ok else "rejected")
        if ok:
            add_account(ctx.db, "threads", name, direction_key, origin="llm", note=suggestion.why)
            added.append(name)
    return added


def expand_accounts(ctx: "Ctx", direction_key: str, posts: int = 5) -> list[str]:
    """Opens the pages of the direction's best posts, below the triage threshold too, so that
    seed accounts with promotional posts still lead to related authors."""
    if direction_key not in load_directions(ctx.db, enabled_only=False):
        raise ValueError(f"нет направления {direction_key}")
    source = ctx.sources["threads"]
    if "post" not in source.kinds():
        raise ValueError("страницы постов Threads сейчас не читаются: " + source.status().reason)
    before = known_usernames(ctx.db, "threads")
    for post in unvisited_posts(ctx.db, "threads", direction_key, posts):
        try:
            save_comments(ctx.db, post["id"], source.comments(post))
        except Exception as e:  # noqa: BLE001 - one broken page must not stop the expansion
            ctx.log.warning("[expand] %s: %s", post["url"], e)
            continue
        ctx.stat("expand", "pages")
    return sorted(known_usernames(ctx.db, "threads") - before)


def _about(d: "Direction") -> str:
    about = "; ".join(filter(None, [
        ", ".join(f"r/{s}" for s in d.subreddits[:8]),
        " | ".join(d.threads_queries[:3] or d.reddit_search[:3]),
    ]))
    return about or d.name
