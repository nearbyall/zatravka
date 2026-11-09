"""Threads access tokens: exchanging a short-lived token and refreshing the long-lived one."""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import httpx

from ...net.http import redact

if TYPE_CHECKING:
    from ...config import Config
    from ...storage.db import DB

GRAPH = "https://graph.threads.net"
API = f"{GRAPH}/v1.0"
STATE_KEY = "threads_token"
# A long-lived token lasts 60 days and may be refreshed once it is a day old.
REFRESH_EVERY_S = 3 * 86400
RETRY_FAILED_REFRESH_S = 6 * 3600


def _graph_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    r = httpx.get(url, timeout=30, params=params)
    if r.status_code >= 400:
        try:
            message = r.json().get("error", {}).get("message", "")
        except ValueError:
            message = r.text[:200]
        raise RuntimeError(f"Threads API {r.status_code}: {redact(message)}")
    return r.json()


def exchange_token(short_token: str, app_secret: str) -> dict[str, Any]:
    """A one-hour OAuth token → a 60-day token."""
    return _graph_get(f"{GRAPH}/access_token", {
        "grant_type": "th_exchange_token", "client_secret": app_secret, "access_token": short_token})


def refresh_token(token: str) -> dict[str, Any]:
    return _graph_get(f"{GRAPH}/refresh_access_token", {"grant_type": "th_refresh_token", "access_token": token})


def whoami(token: str) -> dict[str, Any]:
    return _graph_get(f"{API}/me", {"fields": "id,username", "access_token": token})


def ensure_token(cfg: "Config", db: "DB", log: logging.Logger | None = None) -> str | None:
    """Takes the token from .env, refreshes it every few days and keeps the fresh one in the
    database. A new token placed in .env replaces the stored one."""
    env = os.environ.get("THREADS_ACCESS_TOKEN") or cfg.secrets.threads_access_token
    if not env:
        return None
    env_hash = hashlib.sha1(env.encode()).hexdigest()[:12]
    state = db.kv_get(STATE_KEY) or {}
    if state.get("env_hash") != env_hash:
        state = {"env_hash": env_hash, "token": env, "refreshed_at": 0, "attempt_at": 0}
    now = time.time()
    if now - state.get("refreshed_at", 0) > REFRESH_EVERY_S and now - state.get("attempt_at", 0) > RETRY_FAILED_REFRESH_S:
        state["attempt_at"] = now
        try:
            state["token"] = refresh_token(state["token"])["access_token"]
            state["refreshed_at"] = now
            if log:
                log.info("Threads: токен продлён на 60 дней")
        except Exception as e:  # noqa: BLE001 - the old token may still work
            if log:
                log.warning("Threads: продлить токен не удалось (%s). Если токен истёк — получите новый", e)
    db.kv_set(STATE_KEY, state)
    cfg.secrets.threads_access_token = state["token"]
    return state["token"]
