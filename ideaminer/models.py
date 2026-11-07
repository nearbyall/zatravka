"""Общие структуры данных конвейера."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any


@dataclass
class FeedSpec:
    """Одна лента/запрос, которую нужно прочитать в этом прогоне."""
    source: str                 # reddit | x | threads | rss | inbox
    kind: str                   # listing | search | keyword | profile | feed | file
    target: str                 # сабреддиты, запрос, URL ленты, путь
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
        norm = re.sub(r"\W+", " ", self.text.lower()).strip()[:2000]
        return hashlib.sha1(norm.encode()).hexdigest()


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "div", "li", "ul", "ol", "blockquote", "pre", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(fragment: str) -> str:
    p = _TextExtractor()
    p.feed(fragment or "")
    p.close()
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # миллисекунды
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(value).strip()
    if s.isdigit():
        return parse_dt(int(s))
    s = s.replace("Z", "+00:00")
    # Threads отдаёт +0000 без двоеточия
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat() if dt else None


def truncate(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.8 else cut) + " …"
