"""Threads через публичные страницы — пока нет токена API.

Без входа Threads показывает профили и страницы постов, но не поиск. Страница
собирается JavaScript-ом, поэтому её рендерит честный headless-браузер (см.
browser.py), а данные берутся не из вёрстки с обфусцированными классами, а из
JSON, который страница сама кладёт в <script type="application/json">.

Страница поста приносит две полезные вещи: первые ответы под постом (без входа —
обычно до 5–8) и пять «похожих тредов» — по теме и языку близких к посту.

Правила игры: только то, что видно без входа; пауза между страницами; если
страницы раз за разом пустые (Threads начал требовать вход или блокировать
headless) — веб-режим выключается до конца процесса, обходов нет.
Автоматический сбор противоречит правилам Meta — режим выключаемый.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from ...net.browser import BrowserError, HeadlessBrowser
from ...core.dates import parse_dt
from ...core.models import Comment, FeedSpec, RawPost
from ..base import SourceUnavailable


class ProfileUnavailable(SourceUnavailable):
    """Профиль не открылся без входа (нет такого или закрыт)."""

log = logging.getLogger(__name__)

PROFILE_URL = "https://www.threads.com/@{username}"
POST_URL = "https://www.threads.com/@{username}/post/{code}"
_JSON_SCRIPT = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)
_POST_LINK = re.compile(r"threads\.(?:com|net)/@([A-Za-z0-9._]+)/post/([A-Za-z0-9_-]+)")


def iter_threads(obj: Any) -> Iterator[list[dict[str, Any]]]:
    """Все списки thread_items в произвольно вложенном JSON страницы.
    Обход в глубину не зависит от точного пути — тот меняется от релиза к релизу."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            items = cur.get("thread_items")
            if isinstance(items, list) and items:
                yield items
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def thread_to_post(items: list[dict[str, Any]], direction: str | None = None,
                   feed: str | None = None) -> RawPost | None:
    """Тред → один пост. Цепочка из нескольких постов того же автора склеивается:
    мысль часто разложена на 3–5 сообщений подряд."""
    first = (items[0] or {}).get("post") or {}
    author = (first.get("user") or {}).get("username")
    if not first or not author:
        return None
    parts = []
    for it in items:
        p = (it or {}).get("post") or {}
        if (p.get("user") or {}).get("username") != author:
            break
        text = ((p.get("caption") or {}).get("text") or "").strip()
        if text:
            parts.append(text)
    if not parts:
        return None  # пост без текста (картинка, видео) идей не даст
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


def post_page_url(url: str | None) -> str | None:
    """Ссылка на пост (в т.ч. старый домен threads.net из API) → каноническая страница поста."""
    m = _POST_LINK.search(url or "")
    return POST_URL.format(username=m.group(1), code=m.group(2)) if m else None


def relay_results(html: str) -> dict[str, Any]:
    """Ответы Relay-запросов, которые сервер заранее положил в страницу: {имя запроса: data}.
    Каждый лежит в записи ["RelayPrefetchedStreamCache", "next", [], [ключ, {"__bbox": {"result": …}}]],
    где ключ — «adp_<Имя>RelayPreloader_<хеш>»."""
    out: dict[str, Any] = {}
    for block in _JSON_SCRIPT.findall(html):
        try:
            stack = [json.loads(block)]
        except ValueError:
            continue
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                stack.extend(cur.values())
            elif isinstance(cur, list):
                if (len(cur) >= 4 and cur[0] == "RelayPrefetchedStreamCache" and isinstance(cur[3], list)
                        and len(cur[3]) >= 2 and isinstance(cur[3][0], str) and isinstance(cur[3][1], dict)):
                    name = cur[3][0].split("RelayPreloader")[0].removeprefix("adp_")
                    result = (cur[3][1].get("__bbox") or {}).get("result") or {}
                    out.setdefault(name, result.get("data") if isinstance(result, dict) else None)
                stack.extend(cur)
    return out


@dataclass
class PostPage:
    """Что даёт страница поста без входа."""
    found: bool                                             # сам пост на странице есть
    replies: list[Comment] = field(default_factory=list)    # закреплённые и первые ответы
    related: list[RawPost] = field(default_factory=list)    # «похожие треды»


def _reply_posts(conn: Any) -> Iterator[dict[str, Any]]:
    """Посты из связки ответов: edges → node (цепочка ответа) → posts.edges → node."""
    for edge in (conn or {}).get("edges") or []:
        chain = ((edge or {}).get("node") or {}).get("posts") or {}
        for e in chain.get("edges") or []:
            if isinstance((e or {}).get("node"), dict):
                yield e["node"]


def parse_post_page(html: str, direction: str | None = None) -> PostPage:
    """Страница поста → ответы под ним и похожие треды."""
    q = relay_results(html)
    target = ((q.get("BarcelonaPostPageTargetQuery") or {}).get("media")) or {}
    op = (target.get("user") or {}).get("username")
    info = (((q.get("BarcelonaPostPageDownwardQuery") or {}).get("media") or {}).get("text_post_app_info")) or {}
    replies, seen = [], {target.get("pk")}
    for p in [*_reply_posts(info.get("pinned_replies")), *_reply_posts(info.get("direct_replies"))]:
        author = (p.get("user") or {}).get("username")
        text = ((p.get("caption") or {}).get("text") or "").strip()
        if p.get("pk") in seen or not text:
            continue
        seen.add(p.get("pk"))
        # Ответ автора в чужой ветке — тоже контекст («да, сам ищу такой сервис»)
        replies.append(Comment(author=f"{author} (автор)" if author == op else author, body=text,
                               score=p.get("like_count")))
    rel = ((q.get("BarcelonaLoggedOutRelatedPostsWWWQuery") or {}).get("relatedPosts")) or {}
    related = [post for t in rel.get("threads") or []
               if (post := thread_to_post((t or {}).get("thread_items") or [], direction, "related"))]
    return PostPage(found=bool(target), replies=replies, related=related)


def is_login_page(html: str) -> bool:
    """Threads показывает страницу входа вместо профиля: профиля нет или пускают только после входа."""
    m = re.search(r"<title>([^<]*)</title>", html)
    return bool(m) and m.group(1).strip().lower().endswith("log in")


def parse_rendered(html: str, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    """Посты из отрендеренной страницы Threads (профиль или пост)."""
    posts: dict[str, RawPost] = {}
    for block in _JSON_SCRIPT.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for items in iter_threads(data):
            post = thread_to_post(items, direction, feed)
            # Один тред может лежать в нескольких блоках, в одних — обрезанным: берём самый полный
            if post and (post.ext_id not in posts or len(post.body) > len(posts[post.ext_id].body)):
                posts[post.ext_id] = post
    return list(posts.values())


class ThreadsWebBackend:
    """Публичные профили через headless-браузер. Поиска по словам нет — его без входа не видно."""
    name = "web"
    kinds = frozenset({"profile"})

    def __init__(self, cfg, browser: HeadlessBrowser | None = None):
        self.cfg = cfg
        w = cfg.sources.threads.web
        self.browser = browser or HeadlessBrowser(w.browser, w.render_budget_ms, no_sandbox=w.no_sandbox)
        self._last = 0.0
        self._empty_in_row = 0
        self._off: str | None = None

    def status(self) -> tuple[bool, str]:
        if not self.cfg.sources.threads.web.enabled:
            return False, "веб-режим выключен (sources.threads.web.enabled)"
        if not self.browser.available:
            return False, "веб-режиму нужен Chromium (не найден)"
        if self._off:
            return False, self._off
        return True, "публичные профили без входа (headless-браузер)"

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

    def _render(self, url: str) -> str:
        ok, why = self.status()
        if not ok:
            raise SourceUnavailable(why)
        self._pace()
        try:
            return self.browser.render(url)
        except BrowserError as e:
            self._strike(str(e))
            raise SourceUnavailable(f"Threads {url}: {e}") from e

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        html = self._render(PROFILE_URL.format(username=feed.target))
        posts = parse_rendered(html, feed.direction, feed.key)
        if posts:
            self._empty_in_row = 0
        elif is_login_page(html):
            if feed.params.get("probe"):
                # Проверка непроверенного имени: страница входа значит «такого аккаунта нет»
                raise ProfileUnavailable(f"@{feed.target}: нет такого аккаунта (Threads показывает вход)")
            self._strike("Threads показывает вход вместо проверенного профиля")
        return posts  # профиль без текстовых постов — не повод выключать режим

    def post_page(self, url: str, direction: str | None = None) -> PostPage:
        """Ответы и похожие треды со страницы поста; пустой PostPage, если ссылка не на пост."""
        page_url = post_page_url(url)
        if not page_url:
            return PostPage(found=False)
        html = self._render(page_url)
        page = parse_post_page(html, direction)
        if page.found:
            self._empty_in_row = 0
        else:  # пост удалён или Threads перестал отдавать страницы без входа
            self._strike("страница поста без данных" + (" (просит вход)" if is_login_page(html) else ""))
        return page
