"""Threads through public pages, for when there is no API token.

Logged out, Threads shows profiles and post pages but not search. Pages are built
by JavaScript, so an honest headless browser renders them (see net.browser), and
data is read from the JSON the page embeds in <script type="application/json">
rather than from obfuscated markup.

A post page gives two useful things: the first replies under the post (usually up
to 5–8 when logged out) and five "related threads" close in topic and language.

Rules: only what is visible without logging in, a pause between pages, and when
pages keep coming back empty (Threads started asking for a login or blocking
headless browsers) web mode switches off for the rest of the process. Automated
collection is against Meta's terms, so the mode is opt-in.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from ...config import Config
from ...core.dates import parse_dt
from ...core.models import Comment, FeedSpec, RawPost
from ...net.browser import BrowserError, HeadlessBrowser
from ..base import Availability, SourceUnavailable

log = logging.getLogger(__name__)

PROFILE_URL = "https://www.threads.com/@{username}"
POST_URL = "https://www.threads.com/@{username}/post/{code}"
_JSON_SCRIPT = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)
_POST_LINK = re.compile(r"threads\.(?:com|net)/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)")
_TITLE = re.compile(r"<title>([^<]*)</title>")

TARGET_QUERY = "BarcelonaPostPageTargetQuery"
REPLIES_QUERY = "BarcelonaPostPageDownwardQuery"
RELATED_QUERY = "BarcelonaLoggedOutRelatedPostsWWWQuery"


class ProfileUnavailable(SourceUnavailable):
    """The profile did not open logged out: it does not exist or is private."""


@dataclass
class PostPage:
    """What a logged-out post page offers."""
    found: bool
    replies: list[Comment] = field(default_factory=list)
    related: list[RawPost] = field(default_factory=list)


def _json_blocks(html: str) -> Iterator[Any]:
    for block in _JSON_SCRIPT.findall(html):
        try:
            yield json.loads(block)
        except ValueError:
            continue


def _walk(obj: Any) -> Iterator[Any]:
    """Every dict and list nested in obj, depth first."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            yield cur
            stack.extend(cur)


def iter_threads(obj: Any) -> Iterator[list[dict[str, Any]]]:
    """All thread_items lists in the page JSON. The walk does not depend on the exact
    path, which changes from release to release."""
    for node in _walk(obj):
        if isinstance(node, dict) and isinstance(node.get("thread_items"), list) and node["thread_items"]:
            yield node["thread_items"]


def thread_to_post(items: list[dict[str, Any]], direction: str | None = None,
                   feed: str | None = None) -> RawPost | None:
    """A thread becomes one post: a chain of posts by the same author is joined, since
    a thought is often spread over 3–5 messages. Posts without text yield None."""
    first = (items[0] or {}).get("post") or {}
    author = _username(first)
    if not first or not author:
        return None
    parts = []
    for it in items:
        post = (it or {}).get("post") or {}
        if _username(post) != author:
            break
        if text := _caption(post):
            parts.append(text)
    if not parts:
        return None
    info = first.get("text_post_app_info") or {}
    code = first.get("code")
    return RawPost(
        platform="threads",
        ext_id=str(first.get("pk") or first.get("id")),
        url=POST_URL.format(username=author, code=code) if code else PROFILE_URL.format(username=author),
        author=author,
        title=None,
        body="\n\n".join(parts),
        created_at=parse_dt(first.get("taken_at")),
        score=first.get("like_count"),
        num_comments=info.get("direct_reply_count"),
        metrics={k: info.get(k) for k in ("repost_count", "quote_count") if info.get(k) is not None},
        lang=first.get("detected_language") or None,
        direction=direction,
        feed=feed,
    )


def _username(post: dict[str, Any]) -> str | None:
    return (post.get("user") or {}).get("username")


def _caption(post: dict[str, Any]) -> str:
    return ((post.get("caption") or {}).get("text") or "").strip()


def post_page_url(url: str | None) -> str | None:
    """A post link (including the old threads.net domain the API returns) → the canonical post page."""
    m = _POST_LINK.search(url or "")
    return POST_URL.format(username=m.group(1), code=m.group(2)) if m else None


def relay_results(html: str) -> dict[str, Any]:
    """Results of the Relay queries the server preloaded into the page, by query name.
    Each sits in ["RelayPrefetchedStreamCache", "next", [], [key, {"__bbox": {"result": …}}]]
    where the key looks like "adp_<Name>RelayPreloader_<hash>"."""
    out: dict[str, Any] = {}
    for block in _json_blocks(html):
        for node in _walk(block):
            if not (isinstance(node, list) and len(node) >= 4 and node[0] == "RelayPrefetchedStreamCache"):
                continue
            entry = node[3]
            if isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[0], str) and isinstance(entry[1], dict):
                name = entry[0].split("RelayPreloader")[0].removeprefix("adp_")
                result = (entry[1].get("__bbox") or {}).get("result") or {}
                out.setdefault(name, result.get("data") if isinstance(result, dict) else None)
    return out


def _reply_posts(connection: Any) -> Iterator[dict[str, Any]]:
    """Posts of a replies connection: edges → node (a reply chain) → posts.edges → node."""
    for edge in (connection or {}).get("edges") or []:
        chain = ((edge or {}).get("node") or {}).get("posts") or {}
        for e in chain.get("edges") or []:
            if isinstance((e or {}).get("node"), dict):
                yield e["node"]


def parse_post_page(html: str, direction: str | None = None) -> PostPage:
    queries = relay_results(html)
    target = (queries.get(TARGET_QUERY) or {}).get("media") or {}
    op = _username(target)
    info = ((queries.get(REPLIES_QUERY) or {}).get("media") or {}).get("text_post_app_info") or {}
    replies, seen = [], {target.get("pk")}
    for post in [*_reply_posts(info.get("pinned_replies")), *_reply_posts(info.get("direct_replies"))]:
        text = _caption(post)
        if post.get("pk") in seen or not text:
            continue
        seen.add(post.get("pk"))
        author = _username(post)
        # The author answering in their own thread is context too ("yes, I'm looking for such a service").
        replies.append(Comment(author=f"{author} (автор)" if author == op else author, body=text,
                               score=post.get("like_count")))
    related_threads = ((queries.get(RELATED_QUERY) or {}).get("relatedPosts") or {}).get("threads") or []
    related = [p for t in related_threads if (p := thread_to_post((t or {}).get("thread_items") or [], direction,
                                                                  "related"))]
    return PostPage(found=bool(target), replies=replies, related=related)


def is_login_page(html: str) -> bool:
    """Threads shows a login page instead of a profile that does not exist or needs a login."""
    m = _TITLE.search(html)
    return bool(m) and m.group(1).strip().lower().endswith("log in")


def parse_rendered(html: str, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    """Posts of a rendered Threads page (a profile or a post)."""
    posts: dict[str, RawPost] = {}
    for block in _json_blocks(html):
        for items in iter_threads(block):
            post = thread_to_post(items, direction, feed)
            # A thread may appear in several blocks, truncated in some: keep the fullest.
            if post and (post.ext_id not in posts or len(post.body) > len(posts[post.ext_id].body)):
                posts[post.ext_id] = post
    return list(posts.values())


class ThreadsWebBackend:
    """Public profiles and post pages through a headless browser; no keyword search logged out."""
    name = "web"
    kinds = frozenset({"profile", "post"})

    def __init__(self, cfg: Config, browser: HeadlessBrowser | None = None):
        self.cfg = cfg
        w = cfg.sources.threads.web
        self.browser = browser or HeadlessBrowser(w.browser, w.render_budget_ms, no_sandbox=w.no_sandbox)
        self._last = 0.0
        self._empty_in_row = 0
        self._off: str | None = None

    def status(self) -> Availability:
        if not self.cfg.sources.threads.web.enabled:
            return Availability(False, "веб-режим выключен (sources.threads.web.enabled)")
        if not self.browser.available:
            return Availability(False, "веб-режиму нужен Chromium (не найден)")
        if self._off:
            return Availability(False, self._off)
        return Availability(True, "публичные профили без входа (headless-браузер)")

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        html = self._render(PROFILE_URL.format(username=feed.target))
        posts = parse_rendered(html, feed.direction, feed.key)
        if posts:
            self._empty_in_row = 0
        elif is_login_page(html):
            if feed.params.get("probe"):
                # When probing an unverified name, the login page means the account does not exist.
                raise ProfileUnavailable(f"@{feed.target}: нет такого аккаунта (Threads показывает вход)")
            self._strike("Threads показывает вход вместо проверенного профиля")
        return posts

    def post_page(self, url: str, direction: str | None = None) -> PostPage:
        """Replies and related threads of a post; an empty PostPage when the link is not a post."""
        page_url = post_page_url(url)
        if not page_url:
            return PostPage(found=False)
        html = self._render(page_url)
        page = parse_post_page(html, direction)
        if page.found:
            self._empty_in_row = 0
        else:
            self._strike("страница поста без данных" + (" (просит вход)" if is_login_page(html) else ""))
        return page

    def _render(self, url: str) -> str:
        state = self.status()
        if not state.ok:
            raise SourceUnavailable(state.reason)
        self._pace()
        try:
            return self.browser.render(url)
        except BrowserError as e:
            self._strike(str(e))
            raise SourceUnavailable(f"Threads {url}: {e}") from e

    def _pace(self) -> None:
        wait = self._last + self.cfg.sources.threads.web.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _strike(self, why: str) -> None:
        self._empty_in_row += 1
        if self._empty_in_row >= self.cfg.sources.threads.web.max_empty_in_row:
            self._off = f"веб-режим отключён до конца прогона: {self._empty_in_row} пустых страниц подряд ({why})"
            log.warning("Threads: %s", self._off)
