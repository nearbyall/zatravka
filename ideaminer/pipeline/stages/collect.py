"""Collecting posts according to the plan, and the cheap prefilter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ...core.dates import iso, parse_dt
from ...core.models import FeedSpec
from ...core.prefilter import evaluate
from ...net.http import HttpError, redact
from ...sources.base import SourceLimitReached
from ...storage.directions import regions_by_direction
from ...storage.feeds import record_feed_fetch
from ...storage.posts import expire_candidates, insert_posts, save_prefilter, unfiltered_posts

if TYPE_CHECKING:
    from ..context import Ctx
    from ..planner import Plan

# The post language is a weak region hint; Spanish and Portuguese are too ambiguous to use.
LANG_REGION = {"ru": "cis", "uk": "east_europe", "de": "eu", "fr": "eu"}


def run_collect(ctx: "Ctx", plan: "Plan") -> None:
    for i, feed in enumerate(plan.feeds, 1):
        try:
            fetched, new = _ingest(ctx, feed)
        except SourceLimitReached as e:
            ctx.stat("collect", "feeds_skipped")
            ctx.log.info("[collect] %s пропущена: %s", feed.key, e)
            continue
        except Exception as e:  # noqa: BLE001 - one broken feed must not fail the run
            status = f"http {e.status}" if isinstance(e, HttpError) else redact(f"error: {type(e).__name__}: {e}")[:200]
            ctx.stat("collect", "feeds_failed")
            ctx.log.warning("[collect] %s → %s", feed.key, status)
            record_feed_fetch(ctx.db, feed.key, status, 0, 0)
            continue
        ctx.log.info("[collect %d/%d] %s → %d постов, новых %d", i, len(plan.feeds), feed.key, fetched, new)
        record_feed_fetch(ctx.db, feed.key, "ok", fetched, new)


def _ingest(ctx: "Ctx", feed: FeedSpec) -> tuple[int, int]:
    source = ctx.sources[feed.source]
    posts = source.fetch(feed)
    new = insert_posts(ctx.db, posts, ctx.run_id)
    source.after_ingest(feed)
    for key, value in (("feeds_ok", 1), ("fetched", len(posts)), ("new_posts", new), (f"new_{feed.source}", new)):
        ctx.stat("collect", key, value)
    return len(posts), new


def run_prefilter(ctx: "Ctx") -> None:
    cfg = ctx.cfg.prefilter
    region_of = regions_by_direction(ctx.db)
    verdicts = []
    for r in unfiltered_posts(ctx.db):
        v = evaluate(cfg, r["title"], r["body"], parse_dt(r["created_at"]), r["score"], r["num_comments"],
                     bool(r["nsfw"]), r["lang"])
        hint = region_of.get(r["direction"] or "") or LANG_REGION.get(v.lang)
        verdicts.append(("candidate" if v.keep else "filtered", v.score, v.reason, v.lang, hint, r["id"]))
        ctx.stat("prefilter", "kept" if v.keep else f"drop_{v.reason.split(':')[0]}")
    if verdicts:
        save_prefilter(ctx.db, verdicts)
    stale = expire_candidates(ctx.db, iso(datetime.now(timezone.utc) - timedelta(days=cfg.max_age_days)))
    if stale:
        ctx.stat("prefilter", "stale", stale)
