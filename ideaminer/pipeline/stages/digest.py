"""Digest of the best new ideas: a Markdown file and, optionally, a Telegram message."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ...core.dates import iso
from ...core.labels import INSIGHT, NATURE, region_short
from ...core.taxonomy import resolve_taxonomy
from ...storage.db import Row, loads
from ...storage.idea_queries import top_recent
from ...storage.ideas import count_ideas
from ...storage.posts import count_posts
from ...storage.usage import spent_since

if TYPE_CHECKING:
    from ..context import Ctx

TELEGRAM_MESSAGE_LIMIT = 3900
TAGS_SHOWN = 4


def render_digest(ctx: "Ctx", ideas: list[Row], days: float, since: str) -> str:
    tax = resolve_taxonomy(ctx.cfg)
    lines = [f"# Идеи — {datetime.now(timezone.utc):%Y-%m-%d}", "",
             f"За {days:g} сут.: постов {count_posts(ctx.db, fetched_since=since)},"
             f" новых идей {count_ideas(ctx.db, created_since=since)}, расход ИИ ${spent_since(ctx.db, since):.4f}.",
             ""]
    if not ideas:
        lines.append("Новых идей выше порога нет.")
        return "\n".join(lines)
    by_sphere: dict[str, list[Row]] = defaultdict(list)
    for idea in ideas:
        by_sphere[idea["sphere"] or "other"].append(idea)
    for sphere, items in sorted(by_sphere.items(), key=lambda kv: -max(x["score"] or 0 for x in kv[1])):
        lines += [f"## {tax['spheres'].get(sphere, sphere)}", ""]
        for idea in items:
            lines += _idea_lines(idea, tax["regions"]) + [""]
    return "\n".join(lines)


def _idea_lines(idea: Row, region_labels: dict[str, str]) -> list[str]:
    mentions = f", упоминаний: {idea['mentions']}" if (idea["mentions"] or 1) > 1 else ""
    regions = ", ".join(region_short(g, region_labels) for g in loads(idea.get("regions_json"), []))
    lines = [f"**{idea['title']}** — {idea['score']:.1f}, {idea['market']}, "
             f"{NATURE.get(idea['nature'], idea['nature'])}, {INSIGHT.get(idea['insight_type'], idea['insight_type'])}"
             f"{mentions}" + (f"; регионы: {regions}" if regions else "")]
    tags = ", ".join(loads(idea["tags_json"], [])[:TAGS_SHOWN])
    lines += [x for x in (idea["one_liner"], f"_{tags}_" if tags else "", idea.get("url")) if x]
    return lines


def send_telegram(token: str, chat_id: str, text: str) -> None:
    parts, buf = [], ""
    for paragraph in text.split("\n\n"):
        if len(buf) + len(paragraph) + 2 > TELEGRAM_MESSAGE_LIMIT:
            parts.append(buf)
            buf = ""
        buf += paragraph + "\n\n"
    if buf.strip():
        parts.append(buf)
    with httpx.Client(timeout=30) as client:
        for part in parts:
            client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": part, "disable_web_page_preview": True}).raise_for_status()


def run_digest(ctx: "Ctx", days: float = 1, send: bool | None = None) -> Path:
    cfg = ctx.cfg.digest
    now = datetime.now(timezone.utc)
    since = iso(now - timedelta(days=days))
    text = render_digest(ctx, top_recent(ctx.db, since, cfg.min_score, cfg.top_n), days, since)
    out_dir = ctx.cfg.path(ctx.cfg.storage.digests_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now:%Y-%m-%d}.md"
    path.write_text(text, encoding="utf-8")
    if cfg.telegram if send is None else send:
        _deliver(ctx, text)
    ctx.db.kv_set("digest_last_date", f"{now:%Y-%m-%d}")
    return path


def _deliver(ctx: "Ctx", text: str) -> None:
    s = ctx.cfg.secrets
    if not (s.telegram_bot_token and s.telegram_chat_id):
        ctx.log.warning("[digest] Telegram включён, но нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
        return
    send_telegram(s.telegram_bot_token, s.telegram_chat_id, text)
    ctx.log.info("[digest] отправлен в Telegram")
