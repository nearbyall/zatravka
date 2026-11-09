"""Posts, directions and runs pages with their small APIs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ...storage.accounts import accounts_by_direction
from ...storage.directions import directions_overview, get_direction, set_direction_enabled
from ...storage.feeds import recent_feeds
from ...storage.ideas import count_ideas
from ...storage.posts import browse_posts, directions_in_use, post_counts_by
from ...storage.runs import recent_runs
from ...storage.usage import daily_llm_usage
from ..views import PAGE_SIZE, WebState, get_state, page_count

router = APIRouter()


@router.get("/posts", response_class=HTMLResponse)
def posts_page(request: Request, status: str | None = None, platform: str | None = None,
               direction: str | None = None, q: str | None = None, page: int = 1,
               web: WebState = Depends(get_state)) -> HTMLResponse:
    page = max(1, page)
    rows, total = browse_posts(web.db, status=status, platform=platform, direction=direction, q=q,
                               limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    return web.render(request, "posts", rows=rows, total=total, page=page, pages=page_count(total),
                      f={"status": status, "platform": platform, "direction": direction, "q": q},
                      counts=post_counts_by(web.db, "status"), directions=directions_in_use(web.db))


@router.get("/directions", response_class=HTMLResponse)
def directions_page(request: Request, web: WebState = Depends(get_state)) -> HTMLResponse:
    return web.render(request, "directions", rows=directions_overview(web.db, web.cfg),
                      accounts=accounts_by_direction(web.db, "threads"))


@router.post("/api/directions/{key}/toggle")
def toggle_direction(key: str, web: WebState = Depends(get_state)) -> dict[str, Any]:
    row = get_direction(web.db, key)
    if not row:
        raise HTTPException(404, "Направление не найдено")
    if row["origin"] == "system":
        raise HTTPException(400, "Системное направление не отключается")
    enabled = not row["enabled"]
    set_direction_enabled(web.db, key, enabled)
    return {"ok": True, "enabled": enabled}


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, web: WebState = Depends(get_state)) -> HTMLResponse:
    return web.render(request, "runs", rows=recent_runs(web.db), days=daily_llm_usage(web.db),
                      feeds=recent_feeds(web.db))


@router.post("/api/run")
def start_run(payload: dict = Body(default={}), web: WebState = Depends(get_state)) -> JSONResponse:
    if not web.runner.start(collect=bool(payload.get("collect", True))):
        return JSONResponse({"ok": False, "error": "Прогон уже идёт"}, status_code=409)
    return JSONResponse({"ok": True})


@router.get("/api/run")
def run_state(web: WebState = Depends(get_state)) -> dict[str, Any]:
    return web.runner.state()


@router.get("/healthz")
def healthz(web: WebState = Depends(get_state)) -> dict[str, Any]:
    return {"ok": True, "ideas": count_ideas(web.db)}
