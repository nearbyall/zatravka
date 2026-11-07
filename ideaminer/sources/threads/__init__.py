"""Threads: один источник, два взаимозаменяемых способа чтения.

- api — официальный Threads API: поиск по словам и профили (нужен токен);
- web — публичные профили без входа через headless-браузер (пока токена нет).

sources.threads.mode: auto (API, если есть токен, иначе веб), api или web.
Остальной конвейер разницы не видит: на выходе одинаковые RawPost с platform=threads.
Что читать в профилях — решает реестр аккаунтов (accounts.py). Он растёт сам:
страница удачного поста показывает «похожие треды», и их авторы становятся
кандидатами того же направления.
"""
from __future__ import annotations

import logging
import random

from ...accounts import adopt_accounts, pick_accounts, record_fetch
from ...models import Comment, FeedSpec, RawPost
from ..base import Source, SourceUnavailable, sample
from .api import ThreadsApiBackend, ensure_token, exchange_token, parse_threads, refresh_token, whoami
from .web import ThreadsWebBackend, parse_post_page, parse_rendered

log = logging.getLogger(__name__)

ORDER = {"auto": ("api", "web"), "api": ("api",), "web": ("web",)}


class ThreadsSource(Source):
    name = "threads"

    def __init__(self, cfg, http, db=None, backends: dict | None = None):
        super().__init__(cfg, http, db)
        self.backends = backends or {"api": ThreadsApiBackend(cfg, http), "web": ThreadsWebBackend(cfg)}

    def active(self) -> list:
        """Доступные способы чтения в порядке предпочтения; в auto — только лучший."""
        c = self.cfg.sources.threads
        if c.enabled is False:
            return []
        ready = [self.backends[n] for n in ORDER.get(c.mode, ORDER["auto"]) if self.backends[n].status()[0]]
        return ready[:1] if c.mode == "auto" else ready

    def kinds(self) -> frozenset[str]:
        return frozenset().union(*(b.kinds for b in self.active()))

    def status(self) -> tuple[bool, str]:
        if self.cfg.sources.threads.enabled is False:
            return False, "выключен в config.yaml"
        active = self.active()
        if active:
            return True, active[0].status()[1]
        return False, "; ".join(f"{n}: {b.status()[1]}" for n, b in self.backends.items())

    def plan(self, direction, rng: random.Random) -> list[FeedSpec]:
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
        backend = next((b for b in self.active() if feed.kind in b.kinds), None)
        if backend is None:
            raise SourceUnavailable(f"нет способа читать Threads «{feed.kind}» сейчас")
        posts = backend.fetch(feed)
        if feed.kind == "profile" and self.db:
            record_fetch(self.db, "threads", feed.target, len(posts))
        return posts

    def comments(self, post: dict) -> list[Comment]:
        """Ответы под постом со страницы поста (без входа видны только первые).
        Похожие треды с той же страницы пополняют реестр аккаунтов направления поста:
        enrich выбирает лучшие посты прогона, так что реестр растёт от удачных находок."""
        backend = next((b for b in self.active() if hasattr(b, "post_page")), None)
        if backend is None:
            return []
        direction = post.get("direction")
        direction = None if direction in (None, "wildcard") else direction  # общий список аккаунтов
        page = backend.post_page(post["url"], direction)
        c = self.cfg.sources.threads
        if self.db and c.related_authors and page.related:
            added = adopt_accounts(self.db, "threads", [p.author for p in page.related if p.author],
                                   direction, "related", f"похожий тред к {post['url']}",
                                   c.max_accounts_per_direction)
            if added:
                log.info("Threads: новые аккаунты из похожих тредов (%s): %s",
                         direction or "общие", ", ".join("@" + a for a in added))
        return page.replies


__all__ = ["ThreadsSource", "ThreadsApiBackend", "ThreadsWebBackend", "parse_threads", "parse_rendered",
           "parse_post_page",
           "ensure_token", "exchange_token", "refresh_token", "whoami"]
