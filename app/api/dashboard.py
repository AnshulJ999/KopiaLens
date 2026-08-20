"""Dashboard and index-management endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from config import REPOS, repo_kind

from ..core.metrics import table_rows
from ..core.nightly import start_index_pass_async, state as nightly_state, worker_progress
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
    """Manual index. With no ``source`` this runs the SAME worker-subprocess pass the nightly
    scheduler runs, so a hand-triggered index costs this process no memory either. Rebuilding a
    single source stays in-process: it is small, and the worker's whole-repo handover would be
    disproportionate for one source."""
    _require_kopia(repo)
    try:
        if source:
            return start_build(repo, source=source, full=full)
        return start_index_pass_async(repo, full=full)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/index/freshness")
def index_freshness() -> dict:
    """How current every repo's data is - one call for the whole header strip, so the UI does not
    need a per-repo round trip just to say "indexed 3h ago"."""
    from ..main import nightly_success_ts, nightly_ts  # lazy: app.main imports this router

    out = []
    for key, cfg in REPOS.items():
        st = nightly_state(key)
        row = {
            "repo": key, "name": cfg.get("name", key), "kind": cfg.get("kind", "kopia"),
            # last SUCCESS, not last attempt - a crashed run must never read as "indexed just now"
            "last_index": nightly_success_ts(key) or None,
            "last_attempt": nightly_ts(key) or None,
            "running": bool(st.get("running")),
            "started": st.get("started"), "elapsed": st.get("elapsed"),
            "error": st.get("error"),
        }
        if row["running"]:
            row["progress"] = worker_progress(key)
        out.append(row)
    return {"repos": out, "now": time.time()}


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
