from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ...core.labels import NATURE, STATUS, region_short
from ...core.taxonomy import Taxonomy
from ...export import FORMATS, export_file_name, export_rows
from ...storage.db import DB
from ...storage.idea_queries import (cluster_members, evidence_for, facet_counts, get_idea, lead_evidence,
                                     query_ideas, region_counts, similar_ideas)
from ...storage.ideas import count_ideas, set_idea_note, set_idea_status
from ...storage.posts import count_posts
from ...storage.runs import last_finished_run
from ..views import MAP_AXES, PAGE_SIZE, WebState, get_state, page_count

router = APIRouter()
# Everything that changes ideas or serves raw records; the showcase leaves it out.
manage = APIRouter()

TEXT_FILTERS = ("market", "sphere", "nature", "insight", "model", "platform", "q", "region", "origin")
NOTE_LENGTH = 5000
QUERY_LENGTH = 100
MAX_PAGE = 10000
EXPORT_LIMIT = 10000
API_LIMIT = 1000


def filters_from(request: Request, public: bool = False) -> dict[str, Any]:
    qp = request.query_params
    f: dict[str, Any] = {k: (qp.get(k) or "")[:QUERY_LENGTH] or None for k in TEXT_FILTERS}
    f["with_global"] = qp.get("glob") != "0"
    f["status"] = "active" if public else qp.get("status") or "active"
    f["sort"] = qp.get("sort") or "score"
    try:
        f["min_score"] = float(qp["min_score"]) if qp.get("min_score") else None
        f["days"] = float(qp["days"]) if qp.get("days") else None
    except ValueError:
        f["min_score"] = f["days"] = None
    return f


def _without(f: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in f.items() if k not in keys}


def idea_map(db: DB, tax: Taxonomy, f: dict[str, Any], axis: str) -> dict[str, Any]:
    """The sphere × axis map, where the axis is market, region or product type. It ignores
    the sphere filter and the axis's own filter, or it would collapse into one cell."""
    param = MAP_AXES[axis]["param"]
    base = _without(f, "sphere", param, "sort")
    if axis == "region":
        cells = [{**c, "col": c["region"]} for c in region_counts(db, by_sphere=True, **base)]
        present = {c["col"] for c in cells}
        columns = [(k, region_short(k, tax["regions"]), label) for k, label in tax["regions"].items() if k in present]
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
    return {"grid": grid, "spheres": sorted(grid, key=lambda s: -grid[s]["total"]), "columns": columns,
            "axis": axis, "param": param, "peak": max((c["n"] for c in cells), default=0)}


def facet_totals(db: DB, f: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Counts next to each filter option, computed without that filter itself."""
    return {
        "natures": {r["nature"]: r["n"] for r in facet_counts(db, ("nature",), **_without(f, "nature", "sort"))},
        "insights": {r["insight_type"]: r["n"]
                     for r in facet_counts(db, ("insight_type",), **_without(f, "insight", "sort"))},
        "regions": {r["region"]: r["n"] for r in region_counts(db, **_without(f, "region", "sort"))},
    }


@router.get("/", response_class=HTMLResponse)
def ideas_page(request: Request, page: int = 1, web: WebState = Depends(get_state)) -> HTMLResponse:
    db = web.db
    f = filters_from(request, web.public)
    page = min(max(1, page), MAX_PAGE)
    rows, total = query_ideas(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, **f)
    axis = request.query_params.get("axis") or "market"
    totals = {
        "posts": count_posts(db),
        "ideas": count_ideas(db, clusters_only=True),
        "queue": count_posts(db, statuses=("candidate", "promising")),
        "last_run": last_finished_run(db),
        "snapshot_at": db.kv_get("snapshot_at") if web.public else None,
    }
    return web.render(request, "ideas", rows=rows, total=total, page=page, pages=page_count(total), f=f,
                      evidence={r["id"]: lead_evidence(db, r["id"]) for r in rows}, totals=totals,
                      **idea_map(db, web.tax, f, axis if axis in MAP_AXES else "market"), **facet_totals(db, f))


@router.get("/idea/{idea_id}", response_class=HTMLResponse)
def idea_page(request: Request, idea_id: int, web: WebState = Depends(get_state)) -> HTMLResponse:
    db = web.db
    idea = get_idea(db, idea_id)
    if not idea:
        raise HTTPException(404, "Идея не найдена")
    root = idea["cluster_id"] or idea["id"]
    canonical = (get_idea(db, root) or idea) if root != idea["id"] else None
    variants = cluster_members(db, root, idea["id"])
    if canonical:
        variants = [canonical] + [v for v in variants if v["id"] != canonical["id"]]
    similar = similar_ideas(db, idea["sphere"], root) if idea.get("sphere") else []
    return web.render(request, "idea", idea=idea, variants=variants, evidence=evidence_for(db, root),
                      similar=similar, canonical=canonical)


@manage.post("/api/ideas/{idea_id}/status")
def set_status(idea_id: int, payload: dict = Body(...), web: WebState = Depends(get_state)) -> dict[str, Any]:
    status = payload.get("status")
    if not isinstance(status, str) or status not in STATUS:
        raise HTTPException(400, f"Статус должен быть одним из: {', '.join(STATUS)}")
    if not set_idea_status(web.db, idea_id, status):
        raise HTTPException(404, "Идея не найдена")
    return {"ok": True, "status": status, "label": STATUS[status]}


@manage.post("/api/ideas/{idea_id}/note")
def set_note(idea_id: int, payload: dict = Body(...), web: WebState = Depends(get_state)) -> dict[str, Any]:
    if not set_idea_note(web.db, idea_id, str(payload.get("note") or "")[:NOTE_LENGTH]):
        raise HTTPException(404, "Идея не найдена")
    return {"ok": True}


@manage.get("/api/ideas")
def api_ideas(request: Request, limit: int = 100, offset: int = 0,
              web: WebState = Depends(get_state)) -> dict[str, Any]:
    rows, total = query_ideas(web.db, limit=max(1, min(limit, API_LIMIT)), offset=max(0, offset),
                              **filters_from(request))
    return {"total": total, "items": export_rows(web.db, rows)}


@router.get("/export.{fmt}")
def export(request: Request, fmt: str, web: WebState = Depends(get_state)) -> Response:
    export_format = FORMATS.get(fmt)
    if not export_format:
        raise HTTPException(404)
    rows, _ = query_ideas(web.db, limit=EXPORT_LIMIT, **filters_from(request, web.public))
    content = export_format.render(export_rows(web.db, rows), web.tax)
    disposition = {"Content-Disposition": f'attachment; filename="{export_file_name(fmt)}"'}
    return Response(content, headers=disposition, media_type=export_format.media_type)
