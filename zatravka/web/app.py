"""Web UI: the idea map, idea cards, posts, directions and runs.

There are two apps. The working one manages runs and ideas and must stay private.
The showcase serves a sanitized snapshot read-only: ideas, filters and exports.
"""
from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from ..config import Config, load_config
from ..core.taxonomy import resolve_taxonomy
from ..storage.db import DB
from ..storage.directions import sync_directions
from .routes import ideas, pages
from .runner import Runner
from .views import WebState, build_templates

STATIC = Path(__file__).parent / "static"
# Styles carry inline custom properties, hence 'unsafe-inline' for styles only.
SHOWCASE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;"
                               " frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# Pages are mostly repeated markup: gzip shrinks the ideas page about tenfold.
GZIP_MIN_BYTES = 1000


class CachedStatic(StaticFiles):
    """Assets are linked with ?v=N, so a browser may keep them until the version changes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def add_static(app: FastAPI) -> None:
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)
    app.mount("/static", CachedStatic(directory=STATIC), name="static")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db = DB(cfg.db_path)
    sync_directions(cfg, db)
    tax = resolve_taxonomy(cfg)
    app = FastAPI(title="zatravka", docs_url="/api/docs", redoc_url=None)
    app.state.web = WebState(cfg=cfg, db=db, tax=tax, templates=build_templates(tax),
                             runner=Runner(cfg.base_dir / "config.yaml"))
    add_static(app)
    app.include_router(ideas.router)
    app.include_router(ideas.manage)
    app.include_router(pages.router)
    return app


def create_showcase_app(cfg: Config, snapshot: Path) -> FastAPI:
    """The public app over a snapshot from `zatravka showcase build`. It has no routes that
    write, start runs or expose the API schema, and it opens the snapshot immutable."""
    tax = resolve_taxonomy(cfg)
    app = FastAPI(title="zatravka", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = WebState(cfg=cfg, db=DB(snapshot, readonly=True), tax=tax, templates=build_templates(tax))

    @app.middleware("http")
    async def harden(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method not in ("GET", "HEAD"):
            return Response(status_code=405, headers={"Allow": "GET, HEAD"})
        response = await call_next(request)
        response.headers.update(SHOWCASE_HEADERS)
        return response

    add_static(app)
    app.include_router(ideas.router)
    return app
