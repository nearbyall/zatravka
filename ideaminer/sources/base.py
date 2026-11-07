from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, NamedTuple

from ..core.models import Comment, FeedSpec, RawPost

if TYPE_CHECKING:
    from ..config import Config, Direction
    from ..net.http import PoliteHttp
    from ..storage.db import DB, Row


class SourceLimitReached(RuntimeError):
    """The source hit its own limit (such as paid X reads); the feed is skipped."""


class SourceUnavailable(SourceLimitReached):
    """The source cannot read right now (no browser, the site closed up); the feed is skipped."""


class Availability(NamedTuple):
    ok: bool
    reason: str


class Source(ABC):
    """A source of posts: plans feeds for a direction and reads them."""

    name: str = ""

    def __init__(self, cfg: "Config", http: "PoliteHttp", db: "DB | None" = None):
        self.cfg = cfg
        self.http = http
        self.db = db

    @abstractmethod
    def status(self) -> Availability:
        ...

    @abstractmethod
    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        ...

    def plan(self, direction: "Direction", rng: random.Random) -> list[FeedSpec]:
        """Feeds of this source for one direction."""
        return []

    def plan_global(self, rng: random.Random) -> list[FeedSpec]:
        """Feeds not tied to a direction: own RSS feeds, files, shared accounts."""
        return []

    def comments(self, post: "Row") -> list[Comment]:
        """Top comments for a stored post."""
        return []

    def after_ingest(self, feed: FeedSpec) -> None:
        """Called after the feed's posts are stored."""
