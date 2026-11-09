"""Threads through the official API (graph.threads.net); needs THREADS_ACCESS_TOKEN.

Keyword search uses keyword_search (threads_keyword_search permission; until Meta
reviews the app it only returns the owner's posts). Public profiles use
profile_posts (threads_profile_discovery permission).
"""
from __future__ import annotations

from ...config import Config
from ...core.dates import parse_dt
from ...core.models import FeedSpec, RawPost
from ...net.http import PoliteHttp
from ..base import Availability
from .auth import API

FIELDS = "id,text,media_type,permalink,timestamp,username,has_replies,is_quote_post,is_reply"
METRICS = ("media_type", "has_replies", "is_quote_post", "is_reply")


def parse_threads(data: dict, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    return [RawPost(
        platform="threads",
        ext_id=str(t["id"]),
        url=t.get("permalink"),
        author=t.get("username"),
        title=None,
        body=t["text"],
        created_at=parse_dt(t.get("timestamp")),
        metrics={k: t.get(k) for k in METRICS if k in t},
        direction=direction,
        feed=feed,
    ) for t in data.get("data", []) or [] if t.get("text")]


class ThreadsApiBackend:
    name = "api"
    kinds = frozenset({"keyword", "profile"})

    def __init__(self, cfg: Config, http: PoliteHttp):
        self.cfg = cfg
        self.http = http

    def status(self) -> Availability:
        if not self.cfg.secrets.threads_access_token:
            return Availability(False, "нет THREADS_ACCESS_TOKEN")
        return Availability(True, "официальный Threads API")

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        common = {"fields": FIELDS, "limit": self.cfg.sources.threads.limit,
                  "access_token": self.cfg.secrets.threads_access_token}
        if feed.kind == "profile":
            url, params = f"{API}/profile_posts", {"username": feed.target}
        else:
            url, params = f"{API}/keyword_search", {"q": feed.target,
                                                    "search_type": feed.params.get("search_type", "TOP")}
        return parse_threads(self.http.get(url, params=params | common).json(), feed.direction, feed.key)
