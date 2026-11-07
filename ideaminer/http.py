"""Вежливый HTTP-клиент: честный User-Agent, пауза между запросами к хосту,
повторы на 429/5xx с учётом Retry-After. На 401/403/404 не настаивает —
это сигнал, что доступ закрыт, и его надо открывать ключами, а не обходить.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Http

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


SECRET_PARAMS = re.compile(r"((?:access_token|token|key|client_secret|api_key)=)[^&\s]+", re.I)


def redact(text: str) -> str:
    """Прячет секреты в URL и текстах ошибок: токены не должны попадать в логи и интерфейс."""
    return SECRET_PARAMS.sub(r"\1***", text or "")


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = redact(url)
        self.body = redact(body)
        super().__init__(f"HTTP {status} {self.url} {self.body[:200]}")


class PoliteHttp:
    def __init__(self, cfg: Http):
        self.cfg = cfg
        self.client = httpx.Client(
            timeout=cfg.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": cfg.user_agent},
        )
        self._last: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def _wait_turn(self, host: str) -> None:
        interval = self.cfg.min_interval_s.get(host, self.cfg.min_interval_s.get("default", 1.0))
        with self._lock:
            now = time.monotonic()
            next_ok = max(self._last.get(host, 0.0) + interval, self._blocked_until.get(host, 0.0))
            delay = max(0.0, next_ok - now)
            self._last[host] = max(now, next_ok)
        if delay > 10:
            log.info("лимит запросов %s: жду %.0f с", host, delay)
        if delay:
            time.sleep(delay)

    def _note_limits(self, host: str, resp: httpx.Response) -> float | None:
        """Читает x-ratelimit-* (Reddit, X) и Retry-After. Возвращает, сколько ждать до сброса."""
        h = resp.headers
        wait = None
        try:
            if "retry-after" in h:
                wait = float(h["retry-after"])
            remaining = h.get("x-ratelimit-remaining")
            reset = h.get("x-ratelimit-reset")
            if remaining is not None and reset is not None and float(remaining) < 1:
                r = float(reset)
                # X отдаёт момент сброса (unix time), Reddit — секунды до сброса
                r = r - time.time() if r > 1e9 else r
                wait = max(wait or 0, r)
        except ValueError:
            pass
        if wait is not None and wait > 0:
            wait = min(wait + 1, self.cfg.max_rate_wait_s)
            with self._lock:
                self._blocked_until[host] = max(self._blocked_until.get(host, 0.0), time.monotonic() + wait)
        return wait

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        host = urlparse(url).hostname or ""
        attempt = 0
        while True:
            self._wait_turn(host)
            try:
                resp = self.client.request(method, url, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                attempt += 1
                if attempt > self.cfg.max_retries:
                    raise HttpError(0, url, str(e)) from e
                time.sleep(min(60, 2 ** attempt + random.random()))
                continue
            limit_wait = self._note_limits(host, resp)
            if resp.status_code in RETRY_STATUSES and attempt < self.cfg.max_retries:
                attempt += 1
                if not limit_wait:
                    backoff = min(2 ** attempt * 3, self.cfg.max_rate_wait_s) + random.random()
                    with self._lock:
                        self._blocked_until[host] = time.monotonic() + backoff
                log.warning("HTTP %s от %s, повтор %d/%d", resp.status_code, host, attempt, self.cfg.max_retries)
                continue
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, url, resp.text[:500])
            return resp

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)
