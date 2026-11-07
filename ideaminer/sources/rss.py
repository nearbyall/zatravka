"""Разбор RSS 2.0 / Atom и источник «свои ленты» (например, свой RSSHub для X или Threads)."""
from __future__ import annotations

import hashlib
import random
from defusedxml import ElementTree as ET
from typing import Any

from ..models import FeedSpec, RawPost, html_to_text, parse_dt
from .base import Source

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


def parse_feed(xml_text: str | bytes) -> list[dict[str, Any]]:
    """Возвращает элементы ленты как словари: id, title, link, author, html, published, category."""
    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            link = ""
            for l in e.findall(f"{ATOM}link"):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href", "")
                    break
            content = e.find(f"{ATOM}content")
            summary = e.find(f"{ATOM}summary")
            author = e.find(f"{ATOM}author/{ATOM}name")
            cat = e.find(f"{ATOM}category")
            items.append({
                "id": (e.findtext(f"{ATOM}id") or link).strip(),
                "title": (e.findtext(f"{ATOM}title") or "").strip(),
                "link": link,
                "author": author.text.strip() if author is not None and author.text else None,
                "html": (content.text if content is not None else None) or (summary.text if summary is not None else "") or "",
                "published": e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"),
                "category": cat.get("term") if cat is not None else None,
            })
        return items
    channel = root.find("channel")
    if channel is None:
        return items
    for it in channel.findall("item"):
        link = (it.findtext("link") or "").strip()
        items.append({
            "id": (it.findtext("guid") or link).strip(),
            "title": (it.findtext("title") or "").strip(),
            "link": link,
            "author": (it.findtext(DC_CREATOR) or it.findtext("author") or None),
            "html": it.findtext(CONTENT) or it.findtext("description") or "",
            "published": it.findtext("pubDate"),
            "category": it.findtext("category"),
        })
    return items


class RssSource(Source):
    name = "rss"

    def status(self) -> tuple[bool, str]:
        c = self.cfg.sources.rss
        if not c.enabled:
            return False, "выключен в config.yaml"
        if not c.feeds:
            return False, "нет лент в sources.rss.feeds"
        return True, f"{len(c.feeds)} лент"

    def plan_global(self, rng: random.Random) -> list[FeedSpec]:
        return [
            FeedSpec("rss", "feed", f.url, f.direction, {"platform": f.platform})
            for f in self.cfg.sources.rss.feeds
        ]

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        resp = self.http.get(feed.target)
        platform = feed.params.get("platform", "rss")
        posts = []
        for it in parse_feed(resp.content):
            body = html_to_text(it["html"])
            title = it["title"]
            # В лентах X/Threads заголовок обычно дублирует начало текста
            if title and body.startswith(title[:40]):
                title = None
            ext = it["id"] or it["link"] or hashlib.sha1((title or body).encode()).hexdigest()
            posts.append(RawPost(
                platform=platform,
                ext_id=ext,
                url=it["link"] or None,
                author=it["author"],
                title=title,
                body=body,
                created_at=parse_dt(it["published"]),
                community=it.get("category"),
                direction=feed.direction,
                feed=feed.key,
            ))
        return posts
