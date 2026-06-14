"""Dashboard and index-management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from config import REPOS

from ..core.metrics import table_rows
from ..core.sources import sources_index
from ..index.builder import build_index, index_status
from ..index.queries import index_summary
from ..settings import load_settings, save_settings

router = APIRouter(tags=["dashboard"])


@router.get("/repos")
def repos() -> dict:
    settings = load_settings()
    return {"repos": [{"id": key, "name": value["name"]} for key, value in REPOS.items()], "settings": settings}


@router.get("/dashboard")
def dashboard(repo: str = "1", mode: str = "all", refresh: bool = False) -> dict:
    try:
        return table_rows(repo, refresh=refresh, mode=mode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/sources")
def sources(repo: str = "1") -> dict:
    srcs = sources_index(repo)
    return {"sources": sorted(srcs)}


@router.get("/index/status")
def get_index_status(repo: str = "1") -> dict:
    try:
        status = index_status(repo)
        status["summary"] = index_summary(repo)
        return status
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/index/build")
def post_index_build(repo: str = "1", source: str | None = None, max_snapshots: int | None = Query(default=None, ge=1)) -> dict:
    try:
        return build_index(repo, source=source, max_snapshots=max_snapshots)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/settings")
def post_settings(payload: dict) -> dict:
    settings = load_settings()
    settings.update(payload)
    save_settings(settings)
    return settings
