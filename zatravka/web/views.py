"""Template helpers: filters, URL builders and the shared page renderer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..core.labels import (ACCOUNT_ORIGIN, INSIGHT, NATURE, PLATFORM, POST_STATUS, SCORE_LABELS, STATUS,
                           region_short)
from ..core.taxonomy import Taxonomy
from ..storage.db import DB, loads
from ..storage.usage import spent_today
from .runner import Runner

TEMPLATES = Path(__file__).parent / "templates"
PAGE_SIZE = 40
MAP_AXES = {
    "market": {"param": "market", "label": "по рынку"},
    "region": {"param": "region", "label": "по региону"},
    "nature": {"param": "nature", "label": "по типу продукта"},
}
SAFE_SCHEMES = ("http", "https")


def ago(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 90:
        return "только что"
    if s < 3600:
        return f"{int(s // 60)} мин назад"
    if s < 86400:
        return f"{int(s // 3600)} ч назад"
    if s < 86400 * 30:
        return f"{int(s // 86400)} дн назад"
    return dt.strftime("%d.%m.%Y")


def plural(n: Any, one: str, few: str, many: str) -> str:
    """Russian number agreement: 1 идея, 3 идеи, 7 идей."""
    try:
        k = int(n)
    except (TypeError, ValueError):
        return f"{n} {many}"
    if k % 10 == 1 and k % 100 != 11:
        return f"{k} {one}"
    if k % 10 in (2, 3, 4) and k % 100 not in (12, 13, 14):
        return f"{k} {few}"
    return f"{k} {many}"


def safe_url(url: str | None) -> str:
    """Post links come from external feeds, so only http(s) may reach an href."""
    return url if url and urlparse(url).scheme.lower() in SAFE_SCHEMES else "#"


def url_with(request: Request, **changes: Any) -> str:
    """The current URL with changed query parameters; None or "" drops one, paging resets."""
    params = {k: v for k, v in request.query_params.items() if k != "page"}
    for k, v in changes.items():
        if v is None or v == "":
            params.pop(k, None)
        else:
            params[k] = str(v)
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


def page_url(request: Request, page: int) -> str:
    base = url_with(request)
    return f"{base}{'&' if '?' in base else '?'}page={page}"


def page_count(total: int) -> int:
    return (total + PAGE_SIZE - 1) // PAGE_SIZE


def build_templates(tax: Taxonomy) -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATES)
    env = templates.env
    env.globals.update(
        tax=tax, ACCOUNT_ORIGIN=ACCOUNT_ORIGIN, NATURE=NATURE, INSIGHT=INSIGHT, STATUS=STATUS,
        POST_STATUS=POST_STATUS, PLATFORM=PLATFORM, SCORE_LABELS=SCORE_LABELS, AXES=MAP_AXES, loads=loads,
        url_with=url_with, page_url=page_url,
    )
    env.filters.update(
        ago=ago, plural=plural, safe_url=safe_url,
        region=lambda k: region_short(k, tax["regions"]),
        sphere=lambda k: tax["spheres"].get(k or "", k or "—"),
        model=lambda k: tax["business_models"].get(k or "", k or "—"),
    )
    return templates


@dataclass
class WebState:
    """What every route needs; stored on app.state."""
    cfg: Config
    db: DB
    tax: Taxonomy
    templates: Jinja2Templates
    runner: Runner | None = None

    @property
    def public(self) -> bool:
        """The showcase: a read-only snapshot with no runs, statuses or notes."""
        return self.runner is None

    def render(self, request: Request, name: str, **context: Any) -> HTMLResponse:
        context.update(request=request, nav=name, public=self.public)
        if self.runner:
            context.update(spent_today=spent_today(self.db), budget=self.cfg.budget.daily_usd,
                           runner=self.runner.state())
        return self.templates.TemplateResponse(request, f"{name}.html", context)


def get_state(request: Request) -> WebState:
    return request.app.state.web
