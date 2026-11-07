"""Импорт постов из файлов: JSON, JSONL, CSV в папке storage.inbox_dir.

Удобно для выгрузок из сторонних инструментов (Apify, расширения браузера,
ручной экспорт). Поля распознаются гибко; платформа — из поля `platform` или
из префикса имени файла (x_*.json, threads_*.csv, reddit_*.jsonl).
После импорта файл переезжает в inbox/processed/.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import random
import shutil
from pathlib import Path
from typing import Any

from ..models import FeedSpec, RawPost, parse_dt
from .base import Source

log = logging.getLogger(__name__)

TEXT_KEYS = ("text", "body", "content", "full_text", "selftext", "caption", "description")
TITLE_KEYS = ("title", "headline")
URL_KEYS = ("url", "link", "permalink", "post_url", "tweet_url")
AUTHOR_KEYS = ("author", "username", "user", "screen_name", "handle")
DATE_KEYS = ("created_at", "date", "timestamp", "published", "created_utc", "time")
ID_KEYS = ("id", "ext_id", "post_id", "tweet_id", "name")
PLATFORMS = ("reddit", "x", "twitter", "threads")


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = row.get(k)
        if isinstance(v, dict):  # {"user": {"username": ...}}
            v = v.get("username") or v.get("screen_name") or v.get("name")
        if v not in (None, ""):
            return v
    return None


def rows_from_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".jsonl":
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            for k in ("items", "data", "posts", "results"):
                if isinstance(data.get(k), list):
                    return data[k]
            return [data]
        return list(data)
    if suffix == ".csv":
        return list(csv.DictReader(text.splitlines()))
    raise ValueError(f"неизвестный формат {path.name}")


def row_to_post(row: dict[str, Any], default_platform: str, feed: str) -> RawPost | None:
    body = _pick(row, TEXT_KEYS)
    title = _pick(row, TITLE_KEYS)
    if not body and not title:
        return None
    platform = str(row.get("platform") or default_platform).lower()
    if platform == "twitter":
        platform = "x"
    url = _pick(row, URL_KEYS)
    ext = _pick(row, ID_KEYS) or url or hashlib.sha1(f"{title}{body}".encode()).hexdigest()
    score = row.get("score") or row.get("likes") or row.get("like_count") or row.get("favorite_count")
    try:
        score = int(score) if score not in (None, "") else None
    except (TypeError, ValueError):
        score = None
    return RawPost(
        platform=platform,
        ext_id=str(ext),
        url=str(url) if url else None,
        author=str(_pick(row, AUTHOR_KEYS) or "") or None,
        title=str(title) if title else None,
        body=str(body or ""),
        created_at=parse_dt(_pick(row, DATE_KEYS)),
        community=row.get("subreddit") or row.get("community"),
        score=score,
        direction=row.get("direction") or "inbox",
        feed=feed,
    )


class InboxSource(Source):
    name = "inbox"

    @property
    def dir(self) -> Path:
        return self.cfg.path(self.cfg.storage.inbox_dir)

    def status(self) -> tuple[bool, str]:
        if not self.cfg.sources.inbox.enabled:
            return False, "выключен в config.yaml"
        return True, f"папка {self.dir}"

    def files(self) -> list[Path]:
        if not self.dir.exists():
            return []
        return sorted(p for p in self.dir.iterdir()
                      if p.is_file() and p.suffix.lower() in (".json", ".jsonl", ".csv"))

    def plan_global(self, rng: random.Random) -> list[FeedSpec]:
        return [FeedSpec("inbox", "file", str(p), "inbox") for p in self.files()]

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        path = Path(feed.target)
        prefix = path.name.split("_", 1)[0].lower()
        default_platform = prefix if prefix in PLATFORMS else "import"
        posts = []
        for row in rows_from_file(path):
            if isinstance(row, dict):
                p = row_to_post(row, default_platform, feed.key)
                if p:
                    posts.append(p)
        return posts

    def after_ingest(self, feed: FeedSpec) -> None:
        path = Path(feed.target)
        done = self.dir / "processed"
        done.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.move(str(path), str(done / path.name))
