"""Threads через официальный API (graph.threads.net). Нужен THREADS_ACCESS_TOKEN.

Поиск по ключевым словам — эндпоинт keyword_search (право threads_keyword_search;
пока приложение не прошло ревью Meta, поиск возвращает только собственные посты).
Публичные профили — profile_posts (право threads_profile_discovery).
"""
from __future__ import annotations

from ...net.http import redact
from ...core.dates import parse_dt
from ...core.models import FeedSpec, RawPost

BASE = "https://graph.threads.net/v1.0"
FIELDS = "id,text,media_type,permalink,timestamp,username,has_replies,is_quote_post,is_reply"


def parse_threads(data: dict, direction: str | None = None, feed: str | None = None) -> list[RawPost]:
    posts = []
    for t in data.get("data", []) or []:
        text = t.get("text") or ""
        if not text:
            continue
        posts.append(RawPost(
            platform="threads",
            ext_id=str(t["id"]),
            url=t.get("permalink"),
            author=t.get("username"),
            title=None,
            body=text,
            created_at=parse_dt(t.get("timestamp")),
            metrics={k: t.get(k) for k in ("media_type", "has_replies", "is_quote_post", "is_reply") if k in t},
            direction=direction,
            feed=feed,
        ))
    return posts


class ThreadsApiBackend:
    """Официальный API: поиск по словам и публичные профили."""
    name = "api"
    kinds = frozenset({"keyword", "profile"})

    def __init__(self, cfg, http):
        self.cfg = cfg
        self.http = http

    def status(self) -> tuple[bool, str]:
        if not self.cfg.secrets.threads_access_token:
            return False, "нет THREADS_ACCESS_TOKEN"
        return True, "официальный Threads API"

    def fetch(self, feed: FeedSpec) -> list[RawPost]:
        c = self.cfg.sources.threads
        token = self.cfg.secrets.threads_access_token
        if feed.kind == "profile":
            url = f"{BASE}/profile_posts"
            params = {"username": feed.target, "fields": FIELDS, "limit": c.limit, "access_token": token}
        else:
            url = f"{BASE}/keyword_search"
            params = {"q": feed.target, "search_type": feed.params.get("search_type", "TOP"),
                      "fields": FIELDS, "limit": c.limit, "access_token": token}
        resp = self.http.get(url, params=params)
        return parse_threads(resp.json(), feed.direction, feed.key)


# ── токен: обмен короткого на долгий и автопродление ───────────────────
GRAPH = "https://graph.threads.net"
REFRESH_EVERY_S = 3 * 86400  # долгий токен живёт 60 дней; продлевать можно, когда ему больше суток


def _graph_get(url: str, params: dict) -> dict:
    """GET к Graph API с понятной ошибкой без токена в тексте."""
    import httpx
    r = httpx.get(url, timeout=30, params=params)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text[:200]
        raise RuntimeError(f"Threads API {r.status_code}: {redact(msg)}")
    return r.json()


def exchange_token(short_token: str, app_secret: str) -> dict:
    """Короткий токен (1 час, из OAuth) → долгий (60 дней)."""
    return _graph_get(f"{GRAPH}/access_token", {
        "grant_type": "th_exchange_token", "client_secret": app_secret, "access_token": short_token})


def refresh_token(token: str) -> dict:
    return _graph_get(f"{GRAPH}/refresh_access_token", {"grant_type": "th_refresh_token", "access_token": token})


def whoami(token: str) -> dict:
    return _graph_get(f"{BASE}/me", {"fields": "id,username", "access_token": token})


def ensure_token(cfg, db, log=None) -> str | None:
    """Берёт токен из .env, продлевает его раз в несколько дней и хранит свежий в БД.
    Если в .env положили новый токен, он заменяет сохранённый."""
    import hashlib
    import os
    import time
    env = os.environ.get("THREADS_ACCESS_TOKEN") or cfg.secrets.threads_access_token
    if not env:
        return None
    env_hash = hashlib.sha1(env.encode()).hexdigest()[:12]
    st = db.kv_get("threads_token") or {}
    if st.get("env_hash") != env_hash:
        st = {"env_hash": env_hash, "token": env, "refreshed_at": 0, "attempt_at": 0}
    now = time.time()
    if now - st.get("refreshed_at", 0) > REFRESH_EVERY_S and now - st.get("attempt_at", 0) > 6 * 3600:
        st["attempt_at"] = now
        try:
            st["token"] = refresh_token(st["token"])["access_token"]
            st["refreshed_at"] = now
            if log:
                log.info("Threads: токен продлён на 60 дней")
        except Exception as e:  # noqa: BLE001 — старый токен ещё может работать
            if log:
                log.warning("Threads: продлить токен не удалось (%s). Если токен истёк — получите новый", e)
    db.kv_set("threads_token", st)
    cfg.secrets.threads_access_token = st["token"]
    return st["token"]
