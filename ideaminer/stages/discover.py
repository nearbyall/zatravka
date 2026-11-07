"""Подбор аккаунтов Threads для направления: ИИ предлагает, браузер проверяет.

ИИ легко выдумывает правдоподобные имена, поэтому в реестр попадают только
аккаунты, чья страница открылась и показала свежие текстовые посты.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..accounts import add_account, normalize_username
from ..llm.schemas import AccountsOut
from ..models import FeedSpec
from ..planner import load_directions
from ..sources.base import SourceLimitReached
from .classify import taxonomy

if TYPE_CHECKING:
    from ..context import Ctx

FRESH_DAYS = 120


def check_account(ctx: "Ctx", username: str, direction: str | None = None) -> tuple[bool, str]:
    """Открывает профиль и решает, стоит ли его читать: есть текстовые посты, последний свежий."""
    src = ctx.sources["threads"]
    if "profile" not in src.kinds():
        return False, "Threads сейчас не читается: " + src.status()[1]
    try:
        posts = src.fetch(FeedSpec("threads", "profile", username, direction or "wildcard", {"probe": 1}))
    except SourceLimitReached as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001 — проверка одного аккаунта не должна валить подбор
        return False, f"ошибка: {e}"
    if not posts:
        return False, "нет текстовых постов (нет такого аккаунта, закрыт или только картинки)"
    newest = max((p.created_at for p in posts if p.created_at), default=None)
    if newest and datetime.now(timezone.utc) - newest > timedelta(days=FRESH_DAYS):
        return False, f"давно не пишет (последний пост {newest:%Y-%m-%d})"
    return True, f"{len(posts)} постов, последний {newest:%Y-%m-%d}" if newest else f"{len(posts)} постов"


def discover_accounts(ctx: "Ctx", direction_key: str, count: int = 8) -> list[str]:
    """ИИ предлагает аккаунты для направления; проверенные добавляются в реестр."""
    directions = load_directions(ctx.db, enabled_only=False)
    d = directions.get(direction_key)
    if not d:
        raise ValueError(f"нет направления {direction_key}")
    tax = taxonomy(ctx.cfg)
    about = "; ".join(filter(None, [
        ", ".join(f"r/{s}" for s in d.subreddits[:8]),
        " | ".join(d.threads_queries[:3] or d.reddit_search[:3]),
    ]))
    system = ctx.prompt("discover_accounts", direction_name=d.name, about=about or d.name, count=count,
                        region=tax["regions"].get(d.region or "", "любой"))
    out = ctx.llm.json("discover", system, "Предложи аккаунты.", AccountsOut, max_tokens=900, temperature=0.6)
    known = {r["username"] for r in ctx.db.query("SELECT username FROM accounts WHERE platform='threads'")}
    added = []
    for a in out.accounts[:count]:
        name = normalize_username(a.username)
        if not name or name in known:
            continue
        known.add(name)
        ok, why = check_account(ctx, name, direction_key)
        ctx.log.info("[discover] @%s: %s — %s", name, "✓" if ok else "✗", why)
        if ok:
            add_account(ctx.db, "threads", name, direction_key, origin="llm", note=a.why)
            added.append(name)
        ctx.stat("discover", "accepted" if ok else "rejected")
    return added
