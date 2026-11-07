"""Threads: one source with interchangeable backends.

- api: the official Threads API, keyword search and profiles (needs a token);
- web: public profiles and post pages without logging in, via a headless browser.

sources.threads.mode is auto (API when a token is set, otherwise web), api or web.
The rest of the pipeline cannot tell them apart: both yield RawPost with
platform=threads. Which profiles to read is decided by the account registry,
which grows by itself: a good post's page shows related threads whose authors
join the same direction.
"""
from __future__ import annotations

import logging
import random
from typing import Protocol, cast

from ...config import Config, Direction
from ...core.models import Comment, FeedSpec, RawPost
from ...core.selection import sample
from ...net.http import PoliteHttp
from ...storage.accounts import adopt_accounts, pick_accounts, record_account_fetch
from ...storage.db import DB, Row
from ..base import Availability, Source, SourceUnavailable
from .api import ThreadsApiBackend, parse_threads
from .auth import ensure_token, exchange_token, refresh_token, whoami
from .web import PostPage, ThreadsWebBackend, parse_post_page, parse_rendered

log = logging.getLogger(__name__)

PREFERENCE = {"auto": ("api", "web"), "api": ("api",), "web": ("web",)}


class Backend(Protocol):
    name: str
    kinds: frozenset[str]

    def status(self) -> Availability: ...

    def fetch(self, feed: FeedSpec) -> list[RawPost]: ...


class PostPageBackend(Backend, Protocol):
    def post_page(self, url: str, direction: str | None = None) -> PostPage: ...


class ThreadsSource(Source):
    name = "threads"

    def __init__(self, cfg: Config, http: PoliteHttp, db: DB | None = None,
                 backends: dict[str, Backend] | None = None):
        super().__init__(cfg, http, db)
        self.backends: dict[str, Backend] = backends or {"api": ThreadsApiBackend(cfg, http),
                                                         "web": ThreadsWebBackend(cfg)}

    def active(self) -> list[Backend]:
        """Ready backends in order of preference; in auto mode only the best one."""
        c = self.cfg.sources.threads
        if c.enabled is False:
            return []
        names = PREFERENCE.get(c.mode, PREFERENCE["auto"])
        ready = [self.backends[n] for n in names if self.backends[n].status().ok]
        return ready[:1] if c.mode == "auto" else ready

    def kinds(self) -> frozenset[str]:
        return frozenset().union(*(b.kinds for b in self.active()))

    def status(self) -> Availability:
        if self.cfg.sources.threads.enabled is False:
            return Availability(False, "выключен в config.yaml")
        active = self.active()
        if active:
            return Availability(True, active[0].status().reason)
        return Availability(False, "; ".join(f"{n}: {b.status().reason}" for n, b in self.backends.items()))

    def plan(self, direction: Direction, rng: random.Random) -> list[FeedSpec]:
        c = self.cfg.sources.threads
        kinds = self.kinds()
        feeds = []
        if "keyword" in kinds:
            feeds += [FeedSpec("threads", "keyword", q, direction.key, {"search_type": c.search_type})
                      for q in sample(direction.threads_queries, c.queries_per_direction, rng)]
        if "profile" in kinds and self.db:
            feeds += [FeedSpec("threads", "profile", name, direction.key)
                      for name in pick_accounts(self.db, "threads", direction.key, c.accounts_per_direction, rng)]
        return feeds

    def plan_global(self, rng: random.Random) -> list[FeedSpec]:
        if "profile" not in self.kinds() or not self.db:
            return []
        k = self.cfg.sources.threads.global_accounts_per_run
        return [FeedSpec("threads", "profile", name, "wildcard")
                for name in pick_accounts(self.db, "threads", None, k, rng)]

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        backend = self._backend_for(feed.kind)
        if backend is None:
            raise SourceUnavailable(f"нет способа читать Threads «{feed.kind}» сейчас")
        posts = backend.fetch(feed)
        if feed.kind == "profile" and self.db:
            record_account_fetch(self.db, "threads", feed.target, len(posts))
        return posts

    def comments(self, post: Row) -> list[Comment]:
        """Replies from the post page (only the first ones are visible logged out). Related
        threads on the same page feed the account registry of the post's direction; enrich
        picks the best posts of a run, so the registry grows from good finds."""
        backend = cast("PostPageBackend | None", self._backend_for("post"))
        if backend is None:
            return []
        direction = None if post.get("direction") in (None, "wildcard") else post["direction"]
        page = backend.post_page(post["url"], direction)
        self._adopt_related_authors(page, direction, post["url"])
        return page.replies

    def _backend_for(self, kind: str) -> Backend | None:
        return next((b for b in self.active() if kind in b.kinds), None)

    def _adopt_related_authors(self, page: PostPage, direction: str | None, url: str) -> None:
        c = self.cfg.sources.threads
        if not (self.db and c.related_authors and page.related):
            return
        authors = [p.author for p in page.related if p.author]
        added = adopt_accounts(self.db, "threads", authors, direction, "related", f"похожий тред к {url}",
                               c.max_accounts_per_direction)
        if added:
            log.info("Threads: новые аккаунты из похожих тредов (%s): %s",
                     direction or "общие", ", ".join("@" + a for a in added))


__all__ = ["ThreadsSource", "ThreadsApiBackend", "ThreadsWebBackend", "parse_threads", "parse_rendered",
           "parse_post_page", "ensure_token", "exchange_token", "refresh_token", "whoami"]
