"""X (Twitter) through the official API v2 recent search; needs X_BEARER_TOKEN.

Reads are paid per post, so results per query stay small and the daily read
count is capped by sources.x.daily_read_limit.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone

from ..config import Direction
from ..core.dates import parse_dt
from ..core.models import FeedSpec, RawPost
from ..core.selection import sample
from .base import Availability, Source, SourceLimitReached

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
TWEET_FIELDS = "created_at,public_metrics,lang,author_id,conversation_id,possibly_sensitive,note_tweet"
MIN_RESULTS, MAX_RESULTS = 10, 100
MAX_QUERY_CHARS = 512
SEARCH_WINDOW = timedelta(days=6, hours=23)
SINCE_OVERLAP = timedelta(seconds=30)


def parse_search(data: dict, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    posts = []
    for t in data.get("data", []) or []:
        username = users.get(t.get("author_id"), {}).get("username")
        m = t.get("public_metrics", {}) or {}
        posts.append(RawPost(
            platform="x",
            ext_id=str(t["id"]),
            url=f"https://x.com/{username or 'i'}/status/{t['id']}",
            author=username,
            title=None,
            body=t.get("note_tweet", {}).get("text") or t.get("text", ""),
            created_at=parse_dt(t.get("created_at")),
            score=(m.get("like_count") or 0) + 2 * (m.get("retweet_count") or 0),
            num_comments=m.get("reply_count"),
            metrics=m,
            lang=t.get("lang"),
            nsfw=bool(t.get("possibly_sensitive")),
            direction=direction,
            feed=feed,
        ))
    return posts


class XSource(Source):
    name = "x"

    def status(self) -> Availability:
        if self.cfg.sources.x.enabled is False:
            return Availability(False, "выключен в config.yaml")
        if not self.cfg.secrets.x_bearer_token:
            return Availability(False, "нет X_BEARER_TOKEN (официальный API v2)")
        return Availability(True, "официальный API v2")

    def plan(self, direction: Direction, rng: random.Random) -> list[FeedSpec]:
        return [FeedSpec("x", "search", q, direction.key)
                for q in sample(direction.x_queries, self.cfg.sources.x.queries_per_direction, rng)]

    def reads_today(self) -> int:
        return int(self.db.kv_get(self._reads_key(), 0) or 0) if self.db else 0

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        c = self.cfg.sources.x
        per_query = max(MIN_RESULTS, min(MAX_RESULTS, c.max_results))
        used = self.reads_today()
        if c.daily_read_limit and used + per_query > c.daily_read_limit:
            raise SourceLimitReached(f"дневной лимит чтения X: {used} из {c.daily_read_limit} постов")
        # The parentheses matter: AND binds tighter than OR in X queries, so the suffix
        # would otherwise stick to the last alternative only.
        query = f"({feed.target}) {c.query_suffix}".strip()[:MAX_QUERY_CHARS]
        params = {"query": query, "max_results": per_query, "tweet.fields": TWEET_FIELDS,
                  "expansions": "author_id", "user.fields": "username,name"}
        since_key = "x_since:" + hashlib.sha1(query.encode()).hexdigest()[:16]
        started = datetime.now(timezone.utc)
        start_time = self._start_time(since_key, started)
        if start_time:
            params["start_time"] = start_time
        data = self.http.get(SEARCH_URL, params=params,
                             headers={"Authorization": f"Bearer {self.cfg.secrets.x_bearer_token}"}).json()
        if self.db:
            # X bills every returned post, so reads are counted before parsing.
            self.db.kv_set(self._reads_key(), self.reads_today() + len(data.get("data") or []))
            self.db.kv_set(since_key, (started - SINCE_OVERLAP).isoformat())
        return parse_search(data, feed.direction, feed.key)

    def _reads_key(self) -> str:
        return f"x_reads:{datetime.now(timezone.utc):%Y-%m-%d}"

    def _start_time(self, since_key: str, now: datetime) -> str | None:
        """Only posts newer than the previous read of the query; otherwise X returns the same
        top again and the paid results go to already processed posts. Search sees 7 days back."""
        last = self.db.kv_get(since_key) if self.db else None
        if not last:
            return None
        return max(datetime.fromisoformat(last), now - SEARCH_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
