"""Веб-интерфейс: карта идей, карточка идеи, посты, направления, прогоны."""
from __future__ import annotations

import collections
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config, load_config
from ..core.labels import ACCOUNT_ORIGIN, INSIGHT, NATURE, PLATFORM, POST_STATUS, SCORE_LABELS, STATUS, region_short
from ..export import export_rows, to_csv, to_json, to_markdown, to_xlsx
from ..storage.accounts import accounts_by_direction
from ..storage.db import DB, loads
from ..storage.directions import (directions_overview, get_direction, set_direction_enabled,
                                  sync_directions)
from ..storage.feeds import recent_feeds
from ..storage.idea_queries import (cluster_members, evidence_for, facet_counts, get_idea, lead_evidence,
                                    query_ideas, region_counts, similar_ideas)
from ..storage.ideas import count_ideas, set_idea_note, set_idea_status
from ..storage.posts import browse_posts, count_posts, directions_in_use, post_counts_by
from ..storage.runs import last_finished_run, recent_runs
from ..storage.usage import daily_llm_usage, spent_today
from ..core.taxonomy import resolve_taxonomy

HERE = Path(__file__).parent
PAGE = 40
AXES = {
    "market": {"param": "market", "label": "по рынку"},
    "region": {"param": "region", "label": "по региону"},
    "nature": {"param": "nature", "label": "по типу продукта"},
}

class Runner:
    """Запуск прогона из интерфейса в фоне, с хвостом лога для панели."""

    def __init__(self, cfg_path: Path | None):
        self.cfg_path = cfg_path
        self.thread: threading.Thread | None = None
        self.lines: collections.deque[str] = collections.deque(maxlen=60)
        self.result: dict[str, Any] | None = None
        self.started: float | None = None
        handler = logging.Handler()
        handler.emit = self._emit  # type: ignore[method-assign]
        handler.setLevel(logging.INFO)
        logging.getLogger("ideaminer").addHandler(handler)
        logging.getLogger("ideaminer").setLevel(logging.INFO)

    def _emit(self, record: logging.LogRecord) -> None:
        if self.running:
            self.lines.append(f"{time.strftime('%H:%M:%S')} {record.getMessage()}")

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, collect: bool) -> bool:
        if self.running:
            return False
        self.lines.clear()
        self.result = None
        self.started = time.time()

        def work() -> None:
            from ..context import make_ctx
            from ..pipeline import run_once
            cfg = load_config(self.cfg_path)
            ctx = make_ctx(cfg)
            try:
                self.result = run_once(ctx, trigger="web", collect=collect)
            except Exception as e:  # noqa: BLE001
                self.result = {"status": "error", "error": str(e)}
            finally:
                ctx.close()

        self.thread = threading.Thread(target=work, daemon=True, name="ideaminer-run")
        self.thread.start()
        return True

    def state(self) -> dict[str, Any]:
        return {"running": self.running, "lines": list(self.lines), "result": self.result,
                "elapsed": round(time.time() - self.started) if self.started else None}


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
    """Русское согласование числа: 1 идея, 3 идеи, 7 идей."""
    try:
        k = int(n)
    except (TypeError, ValueError):
        return f"{n} {many}"
    if k % 10 == 1 and k % 100 != 11:
        word = one
    elif k % 10 in (2, 3, 4) and k % 100 not in (12, 13, 14):
        word = few
    else:
        word = many
    return f"{k} {word}"


def url_with(request: Request, **changes: Any) -> str:
    """Текущий URL с изменёнными параметрами; None удаляет параметр, страница сбрасывается."""
    params = dict(request.query_params)
    params.pop("page", None)
    for k, v in changes.items():
        if v is None or v == "":
            params.pop(k, None)
        else:
            params[k] = str(v)
    from urllib.parse import urlencode
    qs = urlencode(params)
    return f"{request.url.path}?{qs}" if qs else request.url.path


def page_url(request: Request, page: int) -> str:
    base = url_with(request)
    return f"{base}{'&' if '?' in base else '?'}page={page}"


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db = DB(cfg.db_path)
    sync_directions(cfg, db)
    tax = resolve_taxonomy(cfg)
    app = FastAPI(title="ideaminer", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    runner = Runner(cfg.base_dir / "config.yaml")

    tpl.env.globals.update(
        tax=tax, ACCOUNT_ORIGIN=ACCOUNT_ORIGIN, NATURE=NATURE, INSIGHT=INSIGHT, STATUS=STATUS, POST_STATUS=POST_STATUS,
        PLATFORM=PLATFORM, SCORE_LABELS=SCORE_LABELS, loads=loads, url_with=url_with, page_url=page_url,
    )
    tpl.env.filters["ago"] = ago
    tpl.env.filters["plural"] = plural
    tpl.env.filters["region"] = lambda k: region_short(k, tax["regions"])
    tpl.env.globals["AXES"] = AXES
    tpl.env.filters["sphere"] = lambda k: tax["spheres"].get(k or "", k or "—")
    tpl.env.filters["model"] = lambda k: tax["business_models"].get(k or "", k or "—")

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.update(request=request, spent_today=spent_today(db), budget=cfg.budget.daily_usd,
                   runner=runner.state(), nav=name)
        return tpl.TemplateResponse(request, f"{name}.html", ctx)

    # ── идеи ────────────────────────────────────────────────────────────
    def filters_from(request: Request) -> dict[str, Any]:
        qp = request.query_params
        f: dict[str, Any] = {k: qp.get(k) or None for k in
                             ("market", "sphere", "nature", "insight", "model", "platform", "q", "region", "origin")}
        f["with_global"] = qp.get("glob") != "0"
        f["status"] = qp.get("status") or "active"
        f["sort"] = qp.get("sort") or "score"
        try:
            f["min_score"] = float(qp["min_score"]) if qp.get("min_score") else None
            f["days"] = float(qp["days"]) if qp.get("days") else None
        except ValueError:
            f["min_score"] = f["days"] = None
        return f

    @app.get("/", response_class=HTMLResponse)
    def ideas_page(request: Request, page: int = 1) -> HTMLResponse:
        f = filters_from(request)
        page = max(1, page)
        rows, total = query_ideas(db, limit=PAGE, offset=(page - 1) * PAGE, **f)
        # Карта «сфера × ось»: ось — рынок, регион или тип продукта. Строится без фильтров
        # по сфере и по своей оси — иначе схлопнется в одну клетку
        axis = request.query_params.get("axis") or "market"
        if axis not in AXES:
            axis = "market"
        param = AXES[axis]["param"]
        base = {k: v for k, v in f.items() if k not in ("sphere", param, "sort")}
        if axis == "region":
            cells = [{**c, "col": c["region"]} for c in region_counts(db, by_sphere=True, **base)]
            present = {c["col"] for c in cells}
            columns = [(k, region_short(k, tax["regions"]), tax["regions"][k]) for k in tax["regions"] if k in present]
        elif axis == "nature":
            cells = [{**c, "col": c["nature"]} for c in facet_counts(db, ("sphere", "nature"), **base)]
            columns = [(k, label, label) for k, label in NATURE.items()]
        else:
            cells = [{**c, "col": c["market"]} for c in facet_counts(db, ("sphere", "market"), **base)]
            columns = [(k, k, label) for k, label in tax["markets"].items()]
        grid: dict[str, dict[str, Any]] = {}
        for c in cells:
            row = grid.setdefault(c["sphere"] or "other", {"total": 0, "cells": {}})
            row["cells"][c["col"]] = c
            row["total"] += c["n"]
        spheres = sorted(grid, key=lambda s: -grid[s]["total"])
        peak = max((c["n"] for c in cells), default=0)
        natures = {r["nature"]: r["n"] for r in facet_counts(db, ("nature",), **{k: v for k, v in f.items() if k not in ("nature", "sort")})}
        insights = {r["insight_type"]: r["n"] for r in facet_counts(db, ("insight_type",), **{k: v for k, v in f.items() if k not in ("insight", "sort")})}
        regions = {r["region"]: r["n"] for r in region_counts(db, **{k: v for k, v in f.items() if k not in ("region", "sort")})}
        evidence = {r["id"]: lead_evidence(db, r["id"]) for r in rows}
        totals = {
            "posts": count_posts(db),
            "ideas": count_ideas(db, clusters_only=True),
            "queue": count_posts(db, statuses=("candidate", "promising")),
            "last_run": last_finished_run(db),
        }
        return render(request, "ideas", rows=rows, total=total, page=page, pages=(total + PAGE - 1) // PAGE,
                      f=f, grid=grid, spheres=spheres, columns=columns, axis=axis, param=param, peak=peak,
                      natures=natures, insights=insights, regions=regions, evidence=evidence, totals=totals)

    @app.get("/idea/{idea_id}", response_class=HTMLResponse)
    def idea_page(request: Request, idea_id: int) -> HTMLResponse:
        idea = get_idea(db, idea_id)
        if not idea:
            raise HTTPException(404, "Идея не найдена")
        root = idea["cluster_id"] or idea["id"]
        canonical = (get_idea(db, root) or idea) if root != idea["id"] else None
        variants = cluster_members(db, root, idea["id"])
        if canonical:
            variants = [canonical] + [v for v in variants if v["id"] != canonical["id"]]
        evidence = evidence_for(db, root)
        similar = similar_ideas(db, idea["sphere"], root) if idea.get("sphere") else []
        return render(request, "idea", idea=idea, variants=variants, evidence=evidence, similar=similar,
                      canonical=canonical)

    @app.post("/api/ideas/{idea_id}/status")
    def set_status(idea_id: int, payload: dict = Body(...)) -> dict[str, Any]:
        status = payload.get("status")
        if status not in STATUS:
            raise HTTPException(400, f"Статус должен быть одним из: {', '.join(STATUS)}")
        if not set_idea_status(db, idea_id, status):
            raise HTTPException(404, "Идея не найдена")
        return {"ok": True, "status": status, "label": STATUS[status]}

    @app.post("/api/ideas/{idea_id}/note")
    def set_note(idea_id: int, payload: dict = Body(...)) -> dict[str, Any]:
        note = str(payload.get("note") or "")[:5000]
        if not set_idea_note(db, idea_id, note):
            raise HTTPException(404, "Идея не найдена")
        return {"ok": True}

    @app.get("/api/ideas")
    def api_ideas(request: Request, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        f = filters_from(request)
        rows, total = query_ideas(db, limit=min(limit, 1000), offset=offset, **f)
        return {"total": total, "items": export_rows(db, rows)}

    @app.get("/export.{fmt}")
    def export(request: Request, fmt: str) -> Response:
        if fmt not in ("xlsx", "csv", "json", "md"):
            raise HTTPException(404)
        f = filters_from(request)
        rows, _ = query_ideas(db, limit=10000, **f)
        items = export_rows(db, rows)
        name = f"ideas-{datetime.now():%Y%m%d-%H%M}.{fmt}"
        disposition = {"Content-Disposition": f'attachment; filename="{name}"'}
        if fmt == "xlsx":
            return Response(to_xlsx(items, tax), headers=disposition,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        body = {"csv": to_csv, "json": to_json, "md": to_markdown}[fmt](items)
        media = {"csv": "text/csv", "json": "application/json", "md": "text/markdown"}[fmt]
        return PlainTextResponse(body, media_type=f"{media}; charset=utf-8", headers=disposition)

    # ── посты ───────────────────────────────────────────────────────────
    @app.get("/posts", response_class=HTMLResponse)
    def posts_page(request: Request, status: str | None = None, platform: str | None = None,
                   direction: str | None = None, q: str | None = None, page: int = 1) -> HTMLResponse:
        page = max(1, page)
        rows, total = browse_posts(db, status=status, platform=platform, direction=direction, q=q, limit=PAGE,
                                   offset=(page - 1) * PAGE)
        counts = post_counts_by(db, "status")
        directions = directions_in_use(db)
        return render(request, "posts", rows=rows, total=total, page=page, pages=(total + PAGE - 1) // PAGE,
                      f={"status": status, "platform": platform, "direction": direction, "q": q},
                      counts=counts, directions=directions)

    # ── направления ─────────────────────────────────────────────────────
    @app.get("/directions", response_class=HTMLResponse)
    def directions_page(request: Request) -> HTMLResponse:
        return render(request, "directions", rows=directions_overview(db, cfg),
                      accounts=accounts_by_direction(db, "threads"))

    @app.post("/api/directions/{key}/toggle")
    def toggle_direction(key: str) -> dict[str, Any]:
        row = get_direction(db, key)
        if not row:
            raise HTTPException(404, "Направление не найдено")
        if row["origin"] == "system":
            raise HTTPException(400, "Системное направление не отключается")
        enabled = not row["enabled"]
        set_direction_enabled(db, key, enabled)
        return {"ok": True, "enabled": enabled}

    # ── прогоны ─────────────────────────────────────────────────────────
    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request) -> HTMLResponse:
        return render(request, "runs", rows=recent_runs(db), days=daily_llm_usage(db), feeds=recent_feeds(db))

    @app.post("/api/run")
    def start_run(payload: dict = Body(default={})) -> JSONResponse:
        if not runner.start(collect=bool(payload.get("collect", True))):
            return JSONResponse({"ok": False, "error": "Прогон уже идёт"}, status_code=409)
        return JSONResponse({"ok": True})

    @app.get("/api/run")
    def run_state() -> dict[str, Any]:
        return runner.state()

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "ideas": count_ideas(db)}

    return app
