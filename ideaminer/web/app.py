"""Web UI: the idea map, idea cards, posts, directions and runs."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import Config, load_config
from ..core.taxonomy import resolve_taxonomy
from ..storage.db import DB
from ..storage.directions import sync_directions
from .routes import ideas, pages
from .runner import Runner
from .views import WebState, build_templates

STATIC = Path(__file__).parent / "static"


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db = DB(cfg.db_path)
    sync_directions(cfg, db)
    tax = resolve_taxonomy(cfg)
    app = FastAPI(title="ideaminer", docs_url="/api/docs", redoc_url=None)
    app.state.web = WebState(cfg=cfg, db=db, tax=tax, runner=Runner(cfg.base_dir / "config.yaml"),
                             templates=build_templates(tax))
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(ideas.router)
    app.include_router(pages.router)
    return app
