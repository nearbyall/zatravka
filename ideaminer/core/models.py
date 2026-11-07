from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_NON_WORD = re.compile(r"\W+")
_HASHED_CHARS = 2000


@dataclass
class FeedSpec:
    """One feed or query to read during a run."""
    source: str
    kind: str
    target: str
    direction: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        extra = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.source}:{self.kind}:{self.target}" + (f"?{extra}" if extra else "")


@dataclass
class Comment:
    author: str | None
    body: str
    score: int | None = None


@dataclass
class RawPost:
    platform: str
    ext_id: str
    url: str | None
    author: str | None
    title: str | None
    body: str
    created_at: datetime | None
    community: str | None = None
    score: int | None = None
    num_comments: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    lang: str | None = None
    nsfw: bool = False
    direction: str | None = None
    feed: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(p for p in (self.title, self.body) if p)

    def content_hash(self) -> str:
        norm = _NON_WORD.sub(" ", self.text.lower()).strip()[:_HASHED_CHARS]
        return hashlib.sha1(norm.encode()).hexdigest()


@dataclass(frozen=True)
class Scope:
    """Which posts the LLM stages handle. Empty means the whole queue; a run limited by
    --sources or --directions handles only its own posts, not the backlog of others."""
    sources: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.sources or self.directions)

    def describe(self) -> str:
        parts = (("источники", self.sources), ("направления", self.directions))
        return "; ".join(f"{label} {', '.join(values)}" for label, values in parts if values)
