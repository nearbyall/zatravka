"""Reddit through the official OAuth API when keys are set, otherwise public RSS feeds.

RSS needs no keys but carries neither post scores nor comment counts; OAuth
(a "script" app with client_credentials) returns full data.
"""
from __future__ import annotations

import random
import re
import time
from urllib.parse import quote, urlencode

from ..config import Direction
from ..core.dates import parse_dt
from ..core.models import Comment, FeedSpec, RawPost
from ..core.selection import sample, weighted_choice
from ..core.text import html_to_text
from ..net.http import HttpError
from ..storage.db import Row
from .base import Availability, Source
from .rss import parse_feed

WEB = "https://www.reddit.com"
API = "https://oauth.reddit.com"
LISTINGS = {
    "hot": ("hot", {}),
    "new": ("new", {}),
    "rising": ("rising", {}),
    "top_day": ("top", {"t": "day"}),
    "top_week": ("top", {"t": "week"}),
    "top_month": ("top", {"t": "month"}),
}
REMOVED = ("[deleted]", "[removed]")
MIN_SUBSCRIBERS = 500
TOKEN_REFRESH_MARGIN_S = 60

_MARKDOWN_BLOCK = re.compile(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", re.S)
_POST_ID = re.compile(r"/comments/([a-z0-9]+)/", re.I)


def rss_body(html: str) -> str:
    """Post text from an RSS entry without the trailing "submitted by … [link] [comments]"."""
    block = _MARKDOWN_BLOCK.search(html or "")
    if block:
        return html_to_text(block.group(1))
    if "submitted by" in (html or ""):
        return ""
    return html_to_text(html)


def _rss_author(raw: str | None) -> str | None:
    return (raw or "").removeprefix("/u/") or None


def parse_rss_posts(xml: str | bytes, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    posts = []
    for it in parse_feed(xml):
        ext = it["id"]
        if not ext.startswith("t3_"):
            m = _POST_ID.search(it["link"] or "")
            if not m:
                continue
            ext = "t3_" + m.group(1)
        posts.append(RawPost(
            platform="reddit",
            ext_id=ext,
            url=it["link"],
            author=_rss_author(it["author"]),
            title=it["title"],
            body=rss_body(it["html"]),
            created_at=parse_dt(it["published"]),
            community=it.get("category"),
            direction=direction,
            feed=feed,
        ))
    return posts


def parse_rss_comments(xml: str | bytes, limit: int) -> list[Comment]:
    out = []
    for it in parse_feed(xml):
        if not it["id"].startswith("t1_"):
            continue
        body = rss_body(it["html"]) or html_to_text(it["html"])
        if body and body not in REMOVED:
            out.append(Comment(author=_rss_author(it["author"]), body=body))
        if len(out) >= limit:
            break
    return out


def parse_api_listing(data: dict, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    posts = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data") or {}
        if child.get("kind") != "t3" or d.get("stickied") or d.get("promoted"):
            continue
        posts.append(RawPost(
            platform="reddit",
            ext_id=d["name"],
            url=WEB + d.get("permalink", ""),
            author=d.get("author"),
            title=d.get("title"),
            body=d.get("selftext") or "",
            created_at=parse_dt(d.get("created_utc")),
            community=d.get("subreddit"),
            score=d.get("score"),
            num_comments=d.get("num_comments"),
            metrics={"upvote_ratio": d.get("upvote_ratio"), "link": d.get("url") if not d.get("is_self") else None},
            nsfw=bool(d.get("over_18")),
            direction=direction,
            feed=feed,
        ))
    return posts


def parse_api_comments(data: list, limit: int) -> list[Comment]:
    if not isinstance(data, list) or len(data) < 2:
        return []
    out = []
    for child in data[1].get("data", {}).get("children", []):
        d = child.get("data") or {}
        body = d.get("body") or ""
        if child.get("kind") == "t1" and body not in REMOVED and not d.get("stickied"):
            out.append(Comment(author=d.get("author"), body=body, score=d.get("score")))
    out.sort(key=lambda c: c.score or 0, reverse=True)
    return out[:limit]


class RedditSource(Source):
    name = "reddit"

    def __init__(self, cfg, http, db=None):
        super().__init__(cfg, http, db)
        mode = cfg.sources.reddit.mode
        self.mode = "oauth" if mode == "oauth" or (mode == "auto" and self._has_keys()) else "rss"
        self._token: str | None = None
        self._token_expires = 0.0

    def _has_keys(self) -> bool:
        s = self.cfg.secrets
        return bool(s.reddit_client_id and s.reddit_client_secret)

    @property
    def _rss(self) -> bool:
        return self.mode == "rss"

    def status(self) -> Availability:
        if not self.cfg.sources.reddit.enabled:
            return Availability(False, "выключен в config.yaml")
        if not self._rss and not self._has_keys():
            return Availability(False, "mode=oauth, но нет REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET")
        return Availability(True, "публичный RSS (без рейтинга и комментариев-метрик)" if self._rss else "OAuth API")

    def plan(self, direction: Direction, rng: random.Random) -> list[FeedSpec]:
        c = self.cfg.sources.reddit
        subs = list(direction.subreddits)
        rng.shuffle(subs)
        size = max(1, c.subs_per_feed)
        groups = [subs[i:i + size] for i in range(0, len(subs), size)][: c.feeds_per_direction]
        feeds = [FeedSpec("reddit", "listing", "+".join(g), direction.key,
                          {"listing": weighted_choice(c.listings, rng)}) for g in groups]
        within = "+".join(direction.subreddits) if direction.search_scope == "subs" else ""
        feeds += [FeedSpec("reddit", "search", q, direction.key, {"subs": within, "t": rng.choice(["week", "month"])})
                  for q in sample(direction.reddit_search, c.searches_per_direction, rng)]
        return feeds

    def feed_url(self, feed: FeedSpec) -> str:
        return self._listing_url(feed) if feed.kind == "listing" else self._search_url(feed)

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        url = self.feed_url(feed)
        if self._rss:
            return parse_rss_posts(self.http.get(url).content, feed.direction, feed.key)
        return parse_api_listing(self.http.get(url, headers=self._auth()).json(), feed.direction, feed.key)

    def comments(self, post: Row) -> list[Comment]:
        limit = self.cfg.sources.reddit.comments_per_post
        post_id = post["ext_id"].removeprefix("t3_")
        if self._rss:
            sub = post.get("community")
            path = f"/r/{sub}/comments/{post_id}/" if sub else f"/comments/{post_id}/"
            resp = self.http.get(f"{WEB}{path}.rss?{urlencode({'sort': 'top', 'limit': limit * 2})}")
            return parse_rss_comments(resp.content, limit)
        query = urlencode({"sort": "top", "limit": limit * 2, "depth": 1, "raw_json": 1})
        return parse_api_comments(self.http.get(f"{API}/comments/{post_id}?{query}", headers=self._auth()).json(),
                                  limit)

    def subreddit_exists(self, sub: str) -> bool | None:
        """True or False when the subreddit is alive or dead; None when the check failed."""
        try:
            if self._rss:
                return self._has_rss_entries(f"{WEB}/r/{sub}/new/.rss?limit=1")
            d = self.http.get(f"{API}/r/{sub}/about?raw_json=1", headers=self._auth()).json().get("data", {})
            return (d.get("subscribers") or 0) >= MIN_SUBSCRIBERS and not d.get("over18")
        except HttpError as e:
            return False if e.status == 404 else None
        except Exception:  # noqa: BLE001 - an unknown failure means "could not check"
            return None

    def _has_rss_entries(self, url: str) -> bool:
        resp = self.http.get(url)
        try:
            return len(parse_feed(resp.content)) > 0
        except Exception:  # noqa: BLE001 - an HTML page came instead of a feed
            return False

    def _listing_url(self, feed: FeedSpec) -> str:
        path, params = LISTINGS.get(feed.params.get("listing", "hot"), LISTINGS["hot"])
        query = {"limit": self._limit(), **params} | ({} if self._rss else {"raw_json": 1})
        return f"{self._base()}/r/{quote(feed.target, safe='+_')}/{path}/{self._rss_suffix()}?{urlencode(query)}"

    def _search_url(self, feed: FeedSpec) -> str:
        query = {"q": feed.target, "sort": "relevance", "t": feed.params.get("t", "month"), "limit": self._limit()}
        if not self._rss:
            query |= {"raw_json": 1, "type": "link"}
        subs = feed.params.get("subs")
        if not subs:
            return f"{self._base()}/search{self._rss_suffix()}?{urlencode(query)}"
        query["restrict_sr"] = "on" if self._rss else 1
        return f"{self._base()}/r/{quote(subs, safe='+_')}/search{self._rss_suffix()}?{urlencode(query)}"

    def _limit(self) -> int:
        return min(100, self.cfg.sources.reddit.items_per_feed)

    def _base(self) -> str:
        return WEB if self._rss else API

    def _rss_suffix(self) -> str:
        return ".rss" if self._rss else ""

    def _user_agent(self) -> str:
        user = self.cfg.secrets.reddit_username
        return f"{self.cfg.http.user_agent} (by /u/{user})" if user else self.cfg.http.user_agent

    def _auth(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_expires - TOKEN_REFRESH_MARGIN_S:
            s = self.cfg.secrets
            data = self.http.post(f"{WEB}/api/v1/access_token", data={"grant_type": "client_credentials"},
                                  auth=(s.reddit_client_id or "", s.reddit_client_secret or ""),
                                  headers={"User-Agent": self._user_agent()}).json()
            self._token = data["access_token"]
            self._token_expires = time.time() + float(data.get("expires_in", 3600))
        return {"Authorization": f"bearer {self._token}", "User-Agent": self._user_agent()}
