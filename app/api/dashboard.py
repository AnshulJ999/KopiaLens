"""Dashboard and index-management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from config import REPOS, repo_kind

from ..core.metrics import table_rows
from ..core.sources import sources_index
from ..index.builder import start_build, index_status_with_summary
from ..settings import load_settings, mutate_settings

router = APIRouter(tags=["dashboard"])


def _require_kopia(repo: str) -> None:
    """These endpoints are DuckDB/kopia-only. Reject a non-kopia repo (e.g. backblaze, which
    has its own /api/backblaze/* routes) so it can never open a stray DuckDB file."""
    if repo_kind(repo) != "kopia":
        raise HTTPException(status_code=400, detail=f"repo {repo!r} is not a kopia repo (use /api/backblaze/*)")


@router.get("/repos")
def repos() -> dict:
    settings = load_settings()
    return {"repos": [{"id": key, "name": value["name"], "kind": value.get("kind", "kopia")}
                      for key, value in REPOS.items()], "settings": settings}


@router.get("/dashboard")
def dashboard(repo: str = "1", mode: str = "all", refresh: bool = False) -> dict:
    _require_kopia(repo)
    try:
        return table_rows(repo, refresh=refresh, mode=mode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/sources")
def sources(repo: str = "1", force: bool = False) -> dict:
    _require_kopia(repo)
    srcs = sources_index(repo, force=force)
    return {"sources": sorted(srcs)}


@router.get("/index/status")
def get_index_status(repo: str = "1") -> dict:
    _require_kopia(repo)
    try:
        return index_status_with_summary(repo)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/index/build")
def post_index_build(repo: str = "1", source: str | None = None, full: bool = False) -> dict:
    _require_kopia(repo)
    try:
        return start_build(repo, source=source, full=full)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/settings")
def post_settings(payload: dict) -> dict:
    kl = payload.pop("kopialens", None)
    bz = payload.pop("backblaze", None)

    def _apply(settings: dict) -> None:
        settings.update(payload)
        if isinstance(kl, dict):  # merge nested sub-dicts, don't clobber the whole block
            settings["kopialens"] = {**settings.get("kopialens", {}), **kl}
        if isinstance(bz, dict):
            settings["backblaze"] = {**settings.get("backblaze", {}), **bz}

    return mutate_settings(_apply)


@router.get("/settings/tables")
def get_table_prefs() -> dict:
    return load_settings().get("kopialens", {}).get("tables", {})


@router.post("/settings/table")
def post_table_pref(payload: dict) -> dict:
    """Persist one table's Tabulator config (columns/sort) in the backend settings file."""
    table_id = payload.get("id")
    if not table_id:
        return {"ok": False}
    mutate_settings(lambda s: s.setdefault("kopialens", {}).setdefault("tables", {}).__setitem__(
        table_id, payload.get("config")))
    return {"ok": True}
