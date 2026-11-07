from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..core.models import Comment, FeedSpec, RawPost

if TYPE_CHECKING:
    from ..config import Config, Direction
    from ..storage.db import DB
    from ..http import PoliteHttp


class SourceLimitReached(RuntimeError):
    """Источник упёрся в собственный лимит (например, платное чтение X) — ленту пропускаем."""


class SourceUnavailable(SourceLimitReached):
    """Источник сейчас не может читать (нет браузера, площадка закрылась) — ленту пропускаем."""


class Source(ABC):
    """Источник постов. Планирует ленты для направления и читает их."""

    name: str = ""

    def __init__(self, cfg: "Config", http: "PoliteHttp", db: "DB | None" = None):
        self.cfg = cfg
        self.http = http
        self.db = db

    @abstractmethod
    def status(self) -> tuple[bool, str]:
        """(доступен ли, пояснение) — для `ideaminer doctor` и планировщика."""

    def plan(self, direction: "Direction", rng: random.Random) -> list[FeedSpec]:
        """Ленты этого источника для одного направления."""
        return []

    def plan_global(self, rng: random.Random) -> list[FeedSpec]:
        """Ленты, не привязанные к направлениям (свои RSS, файлы, аккаунты)."""
        return []

    @abstractmethod
    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        ...

    def comments(self, post: dict) -> list[Comment]:
        """Лучшие комментарии к посту (строка из таблицы posts). По умолчанию нет."""
        return []

    def after_ingest(self, feed: FeedSpec) -> None:
        """Хук после сохранения постов ленты в БД."""
