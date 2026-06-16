"""Dashboard and index-management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from config import REPOS

from ..core.metrics import table_rows
from ..core.sources import sources_index
from ..index.builder import start_build, index_status
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
def sources(repo: str = "1", force: bool = False) -> dict:
    srcs = sources_index(repo, force=force)
    return {"sources": sorted(srcs)}


@router.get("/index/status")
def get_index_status(repo: str = "1") -> dict:
    try:
        status = index_status(repo)
        if not status.get("building"):
            status["summary"] = index_summary(repo)
        return status
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/index/build")
def post_index_build(repo: str = "1", source: str | None = None, full: bool = False) -> dict:
    try:
        return start_build(repo, source=source, full=full)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/settings")
def post_settings(payload: dict) -> dict:
    settings = load_settings()
    kl = payload.pop("kopialens", None)
    settings.update(payload)
    if isinstance(kl, dict):  # merge nested kopialens, don't clobber the whole sub-dict
        settings["kopialens"] = {**settings.get("kopialens", {}), **kl}
    save_settings(settings)
    return settings


@router.get("/settings/tables")
def get_table_prefs() -> dict:
    return load_settings().get("kopialens", {}).get("tables", {})


@router.post("/settings/table")
def post_table_pref(payload: dict) -> dict:
    """Persist one table's Tabulator config (columns/sort) in the backend settings file."""
    table_id = payload.get("id")
    if not table_id:
        return {"ok": False}
    settings = load_settings()
    kl = settings.setdefault("kopialens", {})
    tables = kl.setdefault("tables", {})
    tables[table_id] = payload.get("config")
    save_settings(settings)
    return {"ok": True}
