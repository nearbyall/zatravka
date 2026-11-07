"""RSS 2.0 and Atom parsing, and the "own feeds" source (for example a self-hosted RSSHub)."""
from __future__ import annotations

import hashlib
import random
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree

from ..core.dates import parse_dt
from ..core.models import FeedSpec, RawPost
from ..core.text import html_to_text
from .base import Availability, Source

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"
TITLE_ECHO_CHARS = 40


def parse_feed(xml_text: str | bytes) -> list[dict[str, Any]]:
    """Feed entries as dicts: id, title, link, author, html, published, category."""
    root = ElementTree.fromstring(xml_text)
    if root.tag == f"{ATOM}feed":
        return [_atom_entry(e) for e in root.findall(f"{ATOM}entry")]
    channel = root.find("channel")
    return [] if channel is None else [_rss_item(it) for it in channel.findall("item")]


def _atom_entry(e: Element) -> dict[str, Any]:
    link = next((x.get("href", "") for x in e.findall(f"{ATOM}link") if x.get("rel") in (None, "alternate")), "")
    content, summary = e.find(f"{ATOM}content"), e.find(f"{ATOM}summary")
    author = e.find(f"{ATOM}author/{ATOM}name")
    category = e.find(f"{ATOM}category")
    return {
        "id": (e.findtext(f"{ATOM}id") or link).strip(),
        "title": (e.findtext(f"{ATOM}title") or "").strip(),
        "link": link,
        "author": author.text.strip() if author is not None and author.text else None,
        "html": (content.text if content is not None else None) or (summary.text if summary is not None else "") or "",
        "published": e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"),
        "category": category.get("term") if category is not None else None,
    }


def _rss_item(it: Element) -> dict[str, Any]:
    link = (it.findtext("link") or "").strip()
    return {
        "id": (it.findtext("guid") or link).strip(),
        "title": (it.findtext("title") or "").strip(),
        "link": link,
        "author": it.findtext(DC_CREATOR) or it.findtext("author") or None,
        "html": it.findtext(CONTENT) or it.findtext("description") or "",
        "published": it.findtext("pubDate"),
        "category": it.findtext("category"),
    }


class RssSource(Source):
    name = "rss"

    def status(self) -> Availability:
        c = self.cfg.sources.rss
        if not c.enabled:
            return Availability(False, "выключен в config.yaml")
        if not c.feeds:
            return Availability(False, "нет лент в sources.rss.feeds")
        return Availability(True, f"{len(c.feeds)} лент")

    def plan_global(self, rng: random.Random) -> list[FeedSpec]:
        return [FeedSpec("rss", "feed", f.url, f.direction, {"platform": f.platform})
                for f in self.cfg.sources.rss.feeds]

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        platform = feed.params.get("platform", "rss")
        return [self._post(it, platform, feed) for it in parse_feed(self.http.get(feed.target).content)]

    @staticmethod
    def _post(it: dict[str, Any], platform: str, feed: FeedSpec) -> RawPost:
        body = html_to_text(it["html"])
        title = it["title"]
        # X and Threads feeds repeat the start of the text as the title.
        if title and body.startswith(title[:TITLE_ECHO_CHARS]):
            title = None
        return RawPost(
            platform=platform,
            ext_id=it["id"] or it["link"] or hashlib.sha1((title or body).encode()).hexdigest(),
            url=it["link"] or None,
            author=it["author"],
            title=title,
            body=body,
            created_at=parse_dt(it["published"]),
            community=it.get("category"),
            direction=feed.direction,
            feed=feed.key,
        )
