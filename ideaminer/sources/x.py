"""X (Twitter): официальный API v2, recent search. Нужен X_BEARER_TOKEN.

Чтение в X API платное (pay-per-use / тарифы), поэтому max_results по умолчанию
маленький, а запросов на направление — один.
"""
from __future__ import annotations

import random

import hashlib
from datetime import datetime, timedelta, timezone

from ..models import FeedSpec, RawPost, parse_dt
from .base import Source, SourceLimitReached, sample

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"


def parse_search(data: dict, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    posts = []
    for t in data.get("data", []) or []:
        u = users.get(t.get("author_id"), {})
        username = u.get("username")
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

    def status(self) -> tuple[bool, str]:
        c = self.cfg.sources.x
        if c.enabled is False:
            return False, "выключен в config.yaml"
        if not self.cfg.secrets.x_bearer_token:
            return False, "нет X_BEARER_TOKEN (официальный API v2)"
        return True, "официальный API v2"

    def plan(self, direction, rng: random.Random) -> list[FeedSpec]:
        c = self.cfg.sources.x
        return [FeedSpec("x", "search", q, direction.key)
                for q in sample(direction.x_queries, c.queries_per_direction, rng)]

    def reads_key(self) -> str:
        return f"x_reads:{datetime.now(timezone.utc):%Y-%m-%d}"

    def reads_today(self) -> int:
        return int(self.db.kv_get(self.reads_key(), 0) or 0) if self.db else 0

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        c = self.cfg.sources.x
        per_query = max(10, min(100, c.max_results))
        used = self.reads_today()
        if c.daily_read_limit and used + per_query > c.daily_read_limit:
            raise SourceLimitReached(f"дневной лимит чтения X: {used} из {c.daily_read_limit} постов")
        # Скобки обязательны: в X AND сильнее OR, и суффикс иначе прилипнет к последней ветке
        query = f"({feed.target}) {c.query_suffix}".strip()
        params = {
            "query": query[:512],
            "max_results": per_query,
            "tweet.fields": "created_at,public_metrics,lang,author_id,conversation_id,possibly_sensitive,note_tweet",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        # Только посты новее прошлого чтения этого запроса: иначе X снова отдаёт тот же топ,
        # и платные места в выдаче уходят на уже разобранное. Поиск X видит 7 дней назад.
        since_key = "x_since:" + hashlib.sha1(query.encode()).hexdigest()[:16]
        started = datetime.now(timezone.utc)
        last = self.db.kv_get(since_key) if self.db else None
        if last:
            floor = started - timedelta(days=6, hours=23)
            start = max(datetime.fromisoformat(last), floor)
            params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = self.http.get(SEARCH_URL, params=params,
                             headers={"Authorization": f"Bearer {self.cfg.secrets.x_bearer_token}"})
        data = resp.json()
        if self.db:  # X берёт деньги за каждый отданный пост — считаем их до разбора
            self.db.kv_set(self.reads_key(), self.reads_today() + len(data.get("data") or []))
            self.db.kv_set(since_key, (started - timedelta(seconds=30)).isoformat())
        return parse_search(data, feed.direction, feed.key)
