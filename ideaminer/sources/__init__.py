from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Availability, Source, SourceLimitReached, SourceUnavailable
from .inbox import InboxSource
from .reddit import RedditSource
from .rss import RssSource
from .threads import ThreadsSource
from .x import XSource

if TYPE_CHECKING:
    from ..config import Config
    from ..net.http import PoliteHttp
    from ..storage.db import DB

ALL = (RedditSource, XSource, ThreadsSource, RssSource, InboxSource)


def build_sources(cfg: "Config", http: "PoliteHttp", db: "DB | None" = None) -> dict[str, Source]:
    return {cls.name: cls(cfg, http, db) for cls in ALL}


__all__ = ["ALL", "Availability", "Source", "SourceLimitReached", "SourceUnavailable", "build_sources"]
