"""Дайджест лучших новых идей: Markdown-файл и, по желанию, сообщение в Telegram."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..db import loads
from ..labels import INSIGHT, NATURE, region_short
from .classify import taxonomy

if TYPE_CHECKING:
    from ..context import Ctx


def top_ideas(ctx: "Ctx", days: float, min_score: float, limit: int) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    return ctx.db.query(
        "SELECT i.*, (SELECT p.url FROM idea_evidence e JOIN posts p ON p.id=e.post_id"
        "  JOIN ideas m ON m.id=e.idea_id WHERE m.cluster_id=i.id ORDER BY p.score DESC LIMIT 1) AS url"
        " FROM ideas i WHERE i.cluster_id=i.id AND i.updated_at >= ? AND i.score >= ?"
        " AND i.status NOT IN ('rejected','archived') ORDER BY i.score DESC LIMIT ?",
        (since, min_score, limit),
    )


def render_digest(ctx: "Ctx", ideas: list[dict[str, Any]], days: float) -> str:
    tax = taxonomy(ctx.cfg)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    n_new = ctx.db.scalar("SELECT COUNT(*) FROM ideas WHERE created_at >= ?", (since,), 0)
    n_posts = ctx.db.scalar("SELECT COUNT(*) FROM posts WHERE fetched_at >= ?", (since,), 0)
    cost = ctx.db.spent_since(since)
    lines = [f"# Идеи — {today}", "",
             f"За {days:g} сут.: постов {n_posts}, новых идей {n_new}, расход ИИ ${cost:.4f}.", ""]
    if not ideas:
        lines.append("Новых идей выше порога нет.")
        return "\n".join(lines)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for i in ideas:
        groups[i["sphere"] or "other"].append(i)
    for sphere, items in sorted(groups.items(), key=lambda kv: -max(x["score"] or 0 for x in kv[1])):
        lines += [f"## {tax['spheres'].get(sphere, sphere)}", ""]
        for i in items:
            tags = ", ".join(loads(i["tags_json"], [])[:4])
            mentions = f", упоминаний: {i['mentions']}" if (i["mentions"] or 1) > 1 else ""
            regions = ", ".join(region_short(g, tax["regions"]) for g in loads(i.get("regions_json"), []))
            lines.append(f"**{i['title']}** — {i['score']:.1f}, {i['market']}, {NATURE.get(i['nature'], i['nature'])},"
                         f" {INSIGHT.get(i['insight_type'], i['insight_type'])}{mentions}"
                         + (f"; регионы: {regions}" if regions else ""))
            if i["one_liner"]:
                lines.append(i["one_liner"])
            if tags:
                lines.append(f"_{tags}_")
            if i.get("url"):
                lines.append(i["url"])
            lines.append("")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    parts, buf = [], ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > 3900:
            parts.append(buf)
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        parts.append(buf)
    with httpx.Client(timeout=30) as c:
        for p in parts:
            r = c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": p, "disable_web_page_preview": True})
            r.raise_for_status()


def run_digest(ctx: "Ctx", days: float = 1, send: bool | None = None) -> Path:
    cfg = ctx.cfg.digest
    ideas = top_ideas(ctx, days, cfg.min_score, cfg.top_n)
    text = render_digest(ctx, ideas, days)
    out_dir = ctx.cfg.path(ctx.cfg.storage.digests_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now(timezone.utc):%Y-%m-%d}.md"
    path.write_text(text, encoding="utf-8")
    send = cfg.telegram if send is None else send
    s = ctx.cfg.secrets
    if send:
        if s.telegram_bot_token and s.telegram_chat_id:
            send_telegram(s.telegram_bot_token, s.telegram_chat_id, text)
            ctx.log.info("[digest] отправлен в Telegram")
        else:
            ctx.log.warning("[digest] Telegram включён, но нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
    ctx.db.kv_set("digest_last_date", f"{datetime.now(timezone.utc):%Y-%m-%d}")
    return path
