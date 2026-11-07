"""Сбор постов по плану и дешёвый префильтр."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..db import DB, dumps, loads
from ..core.dates import utcnow
from ..http import HttpError, redact
from ..core.dates import iso, parse_dt
from ..core.models import RawPost
from ..core.prefilter import evaluate
from ..sources.base import SourceLimitReached

if TYPE_CHECKING:
    from ..context import Ctx
    from ..planner import Plan


def insert_posts(db: DB, posts: list[RawPost], run_id: int | None) -> int:
    """Сохраняет новые посты. Дубли по (платформа, id) и по тексту (кросспосты) пропускает,
    у уже известных постов освежает рейтинг."""
    new = 0
    now = utcnow()
    with db.tx():
        for p in posts:
            h = p.content_hash()
            dup = db.one("SELECT 1 FROM posts WHERE content_hash=? AND NOT (platform=? AND ext_id=?)",
                         (h, p.platform, p.ext_id))
            if dup:
                continue
            cur = db.execute(
                "INSERT OR IGNORE INTO posts(platform, ext_id, url, author, community, title, body, lang,"
                " created_at, fetched_at, score, num_comments, metrics_json, direction, feed, run_id,"
                " content_hash, nsfw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.platform, p.ext_id, p.url, p.author, p.community, p.title, p.body, p.lang,
                 iso(p.created_at), now, p.score, p.num_comments, dumps(p.metrics) if p.metrics else None,
                 p.direction, p.feed, run_id, h, int(p.nsfw)),
            )
            if cur.rowcount:
                new += 1
            elif p.score is not None or p.num_comments is not None:
                db.execute(
                    "UPDATE posts SET score=COALESCE(?, score), num_comments=COALESCE(?, num_comments)"
                    " WHERE platform=? AND ext_id=?",
                    (p.score, p.num_comments, p.platform, p.ext_id),
                )
    return new


def run_collect(ctx: "Ctx", plan: "Plan") -> None:
    db = ctx.db
    total = len(plan.feeds)
    for i, feed in enumerate(plan.feeds, 1):
        src = ctx.sources[feed.source]
        status, fetched, new = "ok", 0, 0
        try:
            posts = src.fetch(feed)
            fetched = len(posts)
            new = insert_posts(db, posts, ctx.run_id)
            src.after_ingest(feed)
            ctx.stat("collect", "feeds_ok")
            ctx.stat("collect", "fetched", fetched)
            ctx.stat("collect", "new_posts", new)
            ctx.stat("collect", f"new_{feed.source}", new)
        except SourceLimitReached as e:  # лимит или недоступность источника — пропуск, а не ошибка
            ctx.stat("collect", "feeds_skipped")
            ctx.log.info("[collect] %s пропущена: %s", feed.key, e)
            continue
        except HttpError as e:
            status = f"http {e.status}"
            ctx.stat("collect", "feeds_failed")
            ctx.log.warning("[collect] %s → HTTP %s", feed.key, e.status)
        except Exception as e:  # noqa: BLE001 — одна сломанная лента не валит прогон
            status = redact(f"error: {type(e).__name__}: {e}")[:200]
            ctx.stat("collect", "feeds_failed")
            ctx.log.warning("[collect] %s → %s", feed.key, status)
        else:
            ctx.log.info("[collect %d/%d] %s → %d постов, новых %d", i, total, feed.key, fetched, new)
        db.execute(
            "INSERT INTO feed_state(feed, last_fetched_at, last_status, last_count, new_count, errors)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(feed) DO UPDATE SET last_fetched_at=excluded.last_fetched_at,"
            " last_status=excluded.last_status, last_count=excluded.last_count, new_count=excluded.new_count,"
            " errors=feed_state.errors + excluded.errors",
            (feed.key, utcnow(), status, fetched, new, int(status != "ok")),
        )


# Язык поста как слабая подсказка региона (испанский и португальский неоднозначны — не трогаем)
LANG_REGION = {"ru": "cis", "uk": "east_europe", "de": "eu", "fr": "eu"}


def direction_regions(ctx: "Ctx") -> dict[str, str]:
    out = {}
    for r in ctx.db.query("SELECT key, spec_json FROM directions"):
        region = (loads(r["spec_json"], {}) or {}).get("region")
        if region:
            out[r["key"]] = region
    return out


def run_prefilter(ctx: "Ctx") -> None:
    cfg = ctx.cfg.prefilter
    rows = ctx.db.query(
        "SELECT id, title, body, created_at, score, num_comments, nsfw, lang, direction FROM posts WHERE status='new'")
    dir_region = direction_regions(ctx)
    updates = []
    for r in rows:
        v = evaluate(cfg, r["title"], r["body"], parse_dt(r["created_at"]), r["score"],
                     r["num_comments"], bool(r["nsfw"]), r["lang"])
        hint = dir_region.get(r["direction"] or "") or LANG_REGION.get(v.lang)
        updates.append(("candidate" if v.keep else "filtered", v.score, v.reason, v.lang, hint, r["id"]))
        ctx.stat("prefilter", "kept" if v.keep else f"drop_{v.reason.split(':')[0]}")
    if updates:
        ctx.db.executemany(
            "UPDATE posts SET status=?, prefilter_score=?, prefilter_reason=?, lang=?, region_hint=? WHERE id=?",
            updates)
    # Кандидаты, до которых не дошла очередь, со временем устаревают
    cutoff = iso(datetime.now(timezone.utc) - timedelta(days=cfg.max_age_days))
    cur = ctx.db.execute(
        "UPDATE posts SET status='filtered', prefilter_reason='stale'"
        " WHERE status='candidate' AND created_at IS NOT NULL AND created_at < ?", (cutoff,))
    if cur.rowcount:
        ctx.stat("prefilter", "stale", cur.rowcount)
