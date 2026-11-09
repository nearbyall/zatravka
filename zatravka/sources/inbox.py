"""Importing posts from JSON, JSONL and CSV files in storage.inbox_dir.

Handy for exports of third-party tools (Apify, browser extensions, manual dumps).
Fields are recognised loosely; the platform comes from a `platform` field or from
the file name prefix (x_*.json, threads_*.csv, reddit_*.jsonl). Imported files
move to inbox/processed/.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import shutil
from pathlib import Path
from typing import Any

from ..core.dates import parse_dt
from ..core.models import FeedSpec, RawPost
from .base import Availability, Source

TEXT_KEYS = ("text", "body", "content", "full_text", "selftext", "caption", "description")
TITLE_KEYS = ("title", "headline")
URL_KEYS = ("url", "link", "permalink", "post_url", "tweet_url")
AUTHOR_KEYS = ("author", "username", "user", "screen_name", "handle")
DATE_KEYS = ("created_at", "date", "timestamp", "published", "created_utc", "time")
ID_KEYS = ("id", "ext_id", "post_id", "tweet_id", "name")
SCORE_KEYS = ("score", "likes", "like_count", "favorite_count")
LIST_KEYS = ("items", "data", "posts", "results")
PLATFORMS = ("reddit", "x", "twitter", "threads")
SUFFIXES = (".json", ".jsonl", ".csv")


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = row.get(k)
        if isinstance(v, dict):
            v = v.get("username") or v.get("screen_name") or v.get("name")
        if v not in (None, ""):
            return v
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def rows_from_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            return next((data[k] for k in LIST_KEYS if isinstance(data.get(k), list)), [data])
        return list(data)
    if suffix == ".csv":
        return list(csv.DictReader(text.splitlines()))
    raise ValueError(f"неизвестный формат {path.name}")


def row_to_post(row: dict[str, Any], default_platform: str, feed: str) -> RawPost | None:
    body, title = _pick(row, TEXT_KEYS), _pick(row, TITLE_KEYS)
    if not body and not title:
        return None
    platform = str(row.get("platform") or default_platform).lower()
    url = _pick(row, URL_KEYS)
    return RawPost(
        platform="x" if platform == "twitter" else platform,
        ext_id=str(_pick(row, ID_KEYS) or url or hashlib.sha1(f"{title}{body}".encode()).hexdigest()),
        url=str(url) if url else None,
        author=str(_pick(row, AUTHOR_KEYS) or "") or None,
        title=str(title) if title else None,
        body=str(body or ""),
        created_at=parse_dt(_pick(row, DATE_KEYS)),
        community=row.get("subreddit") or row.get("community"),
        score=_int_or_none(next((row[k] for k in SCORE_KEYS if row.get(k)), None)),
        direction=row.get("direction") or "inbox",
        feed=feed,
    )


class InboxSource(Source):
    name = "inbox"

    @property
    def dir(self) -> Path:
        return self.cfg.path(self.cfg.storage.inbox_dir)

    def status(self) -> Availability:
        if not self.cfg.sources.inbox.enabled:
            return Availability(False, "выключен в config.yaml")
        return Availability(True, f"папка {self.dir}")

    def files(self) -> list[Path]:
        if not self.dir.exists():
            return []
        return sorted(p for p in self.dir.iterdir() if p.is_file() and p.suffix.lower() in SUFFIXES)

    def plan_global(self, rng: random.Random) -> list[FeedSpec]:
        return [FeedSpec("inbox", "file", str(p), "inbox") for p in self.files()]

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        path = Path(feed.target)
        prefix = path.name.split("_", 1)[0].lower()
        platform = prefix if prefix in PLATFORMS else "import"
        posts = (row_to_post(row, platform, feed.key) for row in rows_from_file(path) if isinstance(row, dict))
        return [p for p in posts if p]

    def after_ingest(self, feed: FeedSpec) -> None:
        path = Path(feed.target)
        processed = self.dir / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.move(str(path), str(processed / path.name))
