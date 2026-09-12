"""Dashboard and index-management endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from config import REPOS, repo_kind

from ..core.metrics import table_rows
from ..core.nightly import start_index_pass_async, state as nightly_state, worker_progress
from ..core.sources import sources_index
from ..index.schema import RepoOffline
from ..index.builder import index_status_with_summary
from ..settings import load_settings, mutate_settings

router = APIRouter(tags=["dashboard"])

# /api/repos serves the whole settings file to the UI (the Settings modal reads it). Nothing in it
# may ever carry a credential - the known case is kopialens.server.password (the loopback token
# KopiaLens generates for the optional server listing backend). Scrubbed recursively so a secret
# added under a future block cannot leak by accident. Safe to strip: saveSettings() merges a
# hand-picked subset back, so removing a key from the response can never erase the stored value.
_SECRET_KEY_NAMES = {"password", "password_hash", "secret", "token", "api_key", "api_key_hash"}


def _scrub_secrets(value):
    if isinstance(value, dict):
        return {k: _scrub_secrets(v) for k, v in value.items()
                if str(k).lower() not in _SECRET_KEY_NAMES}
    if isinstance(value, list):
        return [_scrub_secrets(v) for v in value]
    return value


def _require_kopia(repo: str) -> None:
    """These endpoints are DuckDB/kopia-only. Reject a non-kopia repo (e.g. backblaze, which
    has its own /api/backblaze/* routes) so it can never open a stray DuckDB file."""
    # repo_kind() defaults to "kopia" for an id that does not exist at all, so this guard alone let
    # an unknown repo through to code that assumed it was real.
    if repo not in REPOS:
        raise HTTPException(status_code=400, detail=f"unknown repo {repo!r}")
    if repo_kind(repo) != "kopia":
        raise HTTPException(status_code=400, detail=f"repo {repo!r} is not a kopia repo (use /api/backblaze/*)")


@router.get("/repos")
def repos() -> dict:
    from ..core import auth

    settings = load_settings()
    kl = settings.get("kopialens", {})
    # The session cookie is SIGNED with the stored password hash, so the hash must never leave the
    # server - anyone holding it could mint valid sessions. The UI only needs yes/no.
    kl.pop("auth_password_hash", None)
    kl["auth_protected"] = auth.enabled()
    return {"repos": [{"id": key, "name": value["name"], "kind": value.get("kind", "kopia")}
                      for key, value in REPOS.items()], "settings": _scrub_secrets(settings)}


@router.get("/dashboard")
def dashboard(repo: str = "1", mode: str = "all", refresh: bool = False) -> dict:
    _require_kopia(repo)
    try:
        return table_rows(repo, refresh=refresh, mode=mode)
    except RepoOffline:
        raise                       # let the dedicated 503 handler explain it
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
    except RepoOffline:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/index/build")
def post_index_build(repo: str = "1", source: str | None = None, full: bool = False) -> dict:
    """Manual index: the SAME worker-subprocess pass the nightly scheduler runs, so a
    hand-triggered index costs this process no memory either.

    ``source`` is accepted and ignored. It used to select an in-process single-source rebuild;
    the builder now carries unchanged sources forward by reference and rebuilds only what actually
    changed, so a whole-repo run already IS the targeted one. Publishing is also worker-only now,
    so there is no in-process path left to take."""
    _require_kopia(repo)
    try:
        # Same completion callback the scheduler uses - otherwise a successful MANUAL index would
        # leave the freshness strip showing the old success time.
        from ..main import mark_nightly_result  # lazy: app.main imports this router
        return start_index_pass_async(repo, full=full, on_done=mark_nightly_result)
    except RepoOffline:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/index/freshness")
def index_freshness() -> dict:
    """How current every repo's data is - one call for the whole header strip, so the UI does not
    need a per-repo round trip just to say "indexed 3h ago"."""
    from ..main import nightly_error, nightly_success_ts, nightly_ts  # lazy: app.main imports this router
    from ..core.sources import last_listing_error

    out = []
    for key, cfg in REPOS.items():
        st = nightly_state(key)
        stored = nightly_error(key)
        row = {
            "repo": key, "name": cfg.get("name", key), "kind": cfg.get("kind", "kopia"),
            # last SUCCESS, not last attempt - a crashed run must never read as "indexed just now"
            "last_index": nightly_success_ts(key) or None,
            "last_attempt": nightly_ts(key) or None,
            "running": bool(st.get("running")),
            "started": st.get("started"), "elapsed": st.get("elapsed"),
            # in-memory state first, then the persisted reason (survives a restart), then a
            # listing failure that has not reached a pass yet
            "error": st.get("error") or stored.get("reason") or (last_listing_error(key) or None),
            "error_at": stored.get("at"),
        }
        if cfg.get("kind") == "backblaze":
            #: the badge shows REBUILD age - how old the data actually is.
            # Verification age ("checked Xm ago") rides the row tooltip; a detected
            # change waiting on the min-gap is flagged via `pending`.
            try:
                from ..backblaze import indexer as bz_indexer
                info = bz_indexer.freshness()
                row["last_index"] = info["last_reindex_ts"]
                row["last_check_ts"] = info["last_check_ts"]
                row["pending"] = info["pending"]
            except Exception:  # noqa: BLE001 - the strip degrades to the nightly timestamps
                pass
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
