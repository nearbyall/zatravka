from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Source, SourceLimitReached
from .inbox import InboxSource
from .reddit import RedditSource
from .rss import RssSource
from .threads import ThreadsSource
from .x import XSource

if TYPE_CHECKING:
    from ..config import Config
    from ..storage.db import DB
    from ..http import PoliteHttp

ALL = (RedditSource, XSource, ThreadsSource, RssSource, InboxSource)


def build_sources(cfg: "Config", http: "PoliteHttp", db: "DB | None" = None) -> dict[str, Source]:
    return {cls.name: cls(cfg, http, db) for cls in ALL}


__all__ = ["Source", "SourceLimitReached", "build_sources", "ALL"]
