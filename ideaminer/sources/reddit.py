"""Reddit: официальный OAuth API (если заданы ключи) или публичные RSS-ленты.

RSS не требует ключей, но не отдаёт рейтинг поста и число комментариев.
OAuth (приложение типа script, client_credentials) даёт полные данные.
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import quote, urlencode

from ..http import HttpError
from ..models import Comment, FeedSpec, RawPost, html_to_text, parse_dt
from .base import Source, sample, weighted_choice
from .rss import parse_feed

log = logging.getLogger(__name__)

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

_MD_BLOCK = re.compile(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", re.S)
_POST_ID = re.compile(r"/comments/([a-z0-9]+)/", re.I)


def rss_body(html: str) -> str:
    """Текст поста из HTML записи RSS без хвоста «submitted by … [link] [comments]»."""
    m = _MD_BLOCK.search(html or "")
    if m:
        return html_to_text(m.group(1))
    if "submitted by" in (html or ""):
        return ""  # ссылка/картинка без текста
    return html_to_text(html)


def parse_rss_posts(xml: str | bytes, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    posts = []
    for it in parse_feed(xml):
        ext = it["id"]
        if not ext.startswith("t3_"):
            m = _POST_ID.search(it["link"] or "")
            if not m:
                continue
            ext = "t3_" + m.group(1)
        author = (it["author"] or "").removeprefix("/u/") or None
        posts.append(RawPost(
            platform="reddit",
            ext_id=ext,
            url=it["link"],
            author=author,
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
        if body and body not in ("[deleted]", "[removed]"):
            out.append(Comment(author=(it["author"] or "").removeprefix("/u/") or None, body=body))
        if len(out) >= limit:
            break
    return out


def parse_api_listing(data: dict, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    posts = []
    for child in data.get("data", {}).get("children", []):
        if child.get("kind") != "t3":
            continue
        d = child["data"]
        if d.get("stickied") or d.get("promoted"):
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
    out: list[Comment] = []
    if not isinstance(data, list) or len(data) < 2:
        return out
    for child in data[1].get("data", {}).get("children", []):
        if child.get("kind") != "t1":
            continue
        d = child["data"]
        body = d.get("body") or ""
        if body in ("[deleted]", "[removed]") or d.get("stickied"):
            continue
        out.append(Comment(author=d.get("author"), body=body, score=d.get("score")))
    out.sort(key=lambda c: c.score or 0, reverse=True)
    return out[:limit]


class RedditSource(Source):
    name = "reddit"

    def __init__(self, cfg, http, db=None):
        super().__init__(cfg, http, db)
        c = cfg.sources.reddit
        s = cfg.secrets
        has_keys = bool(s.reddit_client_id and s.reddit_client_secret)
        if c.mode == "oauth" or (c.mode == "auto" and has_keys):
            self.mode = "oauth"
        else:
            self.mode = "rss"
        self._token: str | None = None
        self._token_exp = 0.0

    def status(self) -> tuple[bool, str]:
        c = self.cfg.sources.reddit
        if not c.enabled:
            return False, "выключен в config.yaml"
        if self.mode == "oauth" and not (self.cfg.secrets.reddit_client_id and self.cfg.secrets.reddit_client_secret):
            return False, "mode=oauth, но нет REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET"
        return True, "OAuth API" if self.mode == "oauth" else "публичный RSS (без рейтинга и комментариев-метрик)"

    # ── планирование ────────────────────────────────────────────────────
    def plan(self, direction, rng: random.Random) -> list[FeedSpec]:
        c = self.cfg.sources.reddit
        feeds: list[FeedSpec] = []
        subs = list(direction.subreddits)
        rng.shuffle(subs)
        n = max(1, c.subs_per_feed)
        groups = [subs[i:i + n] for i in range(0, len(subs), n)][: c.feeds_per_direction]
        for g in groups:
            feeds.append(FeedSpec("reddit", "listing", "+".join(g), direction.key,
                                  {"listing": weighted_choice(c.listings, rng)}))
        scope = getattr(direction, "search_scope", "subs")
        for q in sample(direction.reddit_search, c.searches_per_direction, rng):
            target = "+".join(direction.subreddits) if scope == "subs" and direction.subreddits else ""
            feeds.append(FeedSpec("reddit", "search", q, direction.key,
                                  {"subs": target, "t": rng.choice(["week", "month"])}))
        return feeds

    # ── OAuth ───────────────────────────────────────────────────────────
    def _ua(self) -> str:
        user = self.cfg.secrets.reddit_username
        return f"{self.cfg.http.user_agent} (by /u/{user})" if user else self.cfg.http.user_agent

    def _auth(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_exp - 60:
            s = self.cfg.secrets
            resp = self.http.post(
                f"{WEB}/api/v1/access_token",
                data={"grant_type": "client_credentials"},
                auth=(s.reddit_client_id or "", s.reddit_client_secret or ""),
                headers={"User-Agent": self._ua()},
            )
            data = resp.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + float(data.get("expires_in", 3600))
        return {"Authorization": f"bearer {self._token}", "User-Agent": self._ua()}

    # ── чтение ──────────────────────────────────────────────────────────
    def _url(self, feed: FeedSpec) -> str:
        limit = min(100, self.cfg.sources.reddit.items_per_feed)
        rss = self.mode == "rss"
        base = WEB if rss else API
        if feed.kind == "listing":
            path, params = LISTINGS.get(feed.params.get("listing", "hot"), LISTINGS["hot"])
            subs = quote(feed.target, safe="+_")
            q = {"limit": limit, **params}
            if not rss:
                q["raw_json"] = 1
            return f"{base}/r/{subs}/{path}/{'.rss' if rss else ''}?{urlencode(q)}"
        # search
        q = {"q": feed.target, "sort": "relevance", "t": feed.params.get("t", "month"), "limit": limit}
        subs = feed.params.get("subs")
        if not rss:
            q.update({"raw_json": 1, "type": "link"})
        if subs:
            q["restrict_sr"] = "on" if rss else 1
            return f"{base}/r/{quote(subs, safe='+_')}/search{'.rss' if rss else ''}?{urlencode(q)}"
        return f"{base}/search{'.rss' if rss else ''}?{urlencode(q)}"

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        url = self._url(feed)
        if self.mode == "rss":
            resp = self.http.get(url)
            return parse_rss_posts(resp.content, feed.direction, feed.key)
        resp = self.http.get(url, headers=self._auth())
        return parse_api_listing(resp.json(), feed.direction, feed.key)

    def comments(self, post: dict) -> list[Comment]:
        limit = self.cfg.sources.reddit.comments_per_post
        pid = post["ext_id"].removeprefix("t3_")
        sub = post.get("community") or ""
        if self.mode == "rss":
            path = f"/r/{sub}/comments/{pid}/" if sub else f"/comments/{pid}/"
            resp = self.http.get(f"{WEB}{path}.rss?{urlencode({'sort': 'top', 'limit': limit * 2})}")
            return parse_rss_comments(resp.content, limit)
        resp = self.http.get(
            f"{API}/comments/{pid}?{urlencode({'sort': 'top', 'limit': limit * 2, 'depth': 1, 'raw_json': 1})}",
            headers=self._auth(),
        )
        return parse_api_comments(resp.json(), limit)

    def subreddit_exists(self, sub: str) -> bool | None:
        """True/False — сабреддит жив/мёртв; None — проверить не удалось."""
        try:
            if self.mode == "rss":
                resp = self.http.get(f"{WEB}/r/{sub}/new/.rss?limit=1")
                try:
                    return len(parse_feed(resp.content)) > 0
                except Exception:  # noqa: BLE001 — вместо ленты пришла HTML-страница
                    return False
            resp = self.http.get(f"{API}/r/{sub}/about?raw_json=1", headers=self._auth())
            d = resp.json().get("data", {})
            return (d.get("subscribers") or 0) >= 500 and not d.get("over18")
        except HttpError as e:
            return False if e.status == 404 else None
        except Exception:  # noqa: BLE001
            return None
