"""Dashboard and index-management endpoints."""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException, Query

from config import REPOS, is_demo, kopia_repos, repo_kind

from ..core.metrics import table_rows
from ..core.nightly import start_index_pass_async, state as nightly_state, worker_progress
from ..core.sources import sources_index
from .errors import PASS_THROUGH
from ..index.builder import index_status_with_summary
from ..settings import load_settings, mutate_settings

router = APIRouter(tags=["dashboard"])

# /api/repos serves the whole settings file to the UI (the Settings modal reads it). Nothing in it
# may ever carry a credential; it is scrubbed recursively so a secret added under a future block
# cannot leak by accident. Safe to strip: saveSettings() merges a
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


def _ordered(rows: list[dict], order: list) -> list[dict]:
    """The display order the user chose in Settings; repos it does not name keep their place after
    the named ones. Display only - the record store (and the nightly queue) keep their own order."""
    rank = {str(rid): i for i, rid in enumerate(order)}
    return sorted(rows, key=lambda r: rank.get(r["id"], len(rank)))


@router.get("/repos")
def repos() -> dict:
    from ..core import auth, dataupgrade, kopiabin
    from ..core import repos as repo_store

    settings = load_settings()
    kl = settings.get("kopialens", {})
    # The session cookie is SIGNED with the stored password hash, so the hash must never leave the
    # server - anyone holding it could mint valid sessions. The UI only needs yes/no.
    kl.pop("auth_password_hash", None)
    kl["auth_protected"] = auth.enabled()
    from ..index.store import auto_limits
    kl["memory_auto"] = auto_limits()[0]  # what "Automatic" means on this PC
    return {
        # Every record, with the fields the repo UI needs: short label, connection source,
        # "counts as a backup", and the cached storage facts (no kopia call on a polled route).
        "repos": _ordered([repo_store.detail(rec) for rec in repo_store.all_records()],
                          kl.get("repo_order") or []),
        # Where the kopia program is (resolved, or not found). The version is read once at
        # startup, so this route never spawns anything.
        "kopia": kopiabin.info(with_version=False),
        # The data folder's format version: pending migration steps, or a folder from a NEWER
        # KopiaLens (which this app refuses to touch).
        "data": dataupgrade.status(),
        "settings": _scrub_secrets(settings),
    }


@router.get("/dashboard")
def dashboard(repo: str, refresh: bool = False) -> dict:
    _require_kopia(repo)
    try:
        return table_rows(repo, refresh=refresh)
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/sources")
def sources(repo: str, force: bool = False) -> dict:
    _require_kopia(repo)
    srcs = sources_index(repo, force=force)
    return {"sources": sorted(srcs)}


@router.get("/index/status")
def get_index_status(repo: str) -> dict:
    _require_kopia(repo)
    from ..core import repos as repo_store
    if repo_store.via_server(repo):
        return {"repo": repo, "via_server": True, "never_indexed": False, "building": False}
    try:
        return index_status_with_summary(repo)
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/index/backups")
def index_backups() -> dict:
    """Settings > Indexing: kept older versions per repo (space each one holds) and the copy
    folder's last result."""
    from ..index import versions
    return {"keep": versions.keep_count(), "dir": versions.backup_dir(),
            "repos": [versions.report(r) for r in kopia_repos() if not is_demo(r)]}


@router.post("/index/backups/copy")
def index_backups_copy() -> dict:
    """Copy every repo's index to the copy folder now, in the background."""
    from ..index import versions
    if not versions.backup_dir():
        raise HTTPException(status_code=400, detail="Choose a folder first.")

    def run() -> None:
        for r in kopia_repos():
            if not is_demo(r):
                versions.sync_backup(r)
    threading.Thread(target=run, name="index-copy", daemon=True).start()
    return {"started": True}


@router.get("/index/notices")
def index_notices() -> dict:
    """An index that was damaged and switched to a backup copy (the page banner)."""
    from ..index import versions
    return {"notices": versions.notices()}


@router.post("/index/notices/{notice_id}/dismiss")
def index_notice_dismiss(notice_id: str) -> dict:
    from ..index import versions
    versions.dismiss(notice_id)
    return {"ok": True}


@router.get("/index/log")
def index_log(repo: str, after: int = 0, limit: int = 400) -> dict:
    """ Paged tail of this repo's worker log for the panel's log pane. ``after`` is the
    cursor the previous call returned; only lines after it come back, and the cursor advances by
    exactly what was returned so a burst pages through instead of being skipped."""
    from ..core.nightly import worker_log_lines
    lines, cursor = worker_log_lines(repo, after=after, limit=limit)
    return {"repo": repo, "lines": lines, "cursor": cursor}


@router.get("/index/pending")
def index_pending() -> dict:
    """Index updates queued after deletes, with when each will start."""
    from ..core import reindexq
    return {"pending": reindexq.pending(), "delay": reindexq.delay(), "now": time.time()}


@router.post("/index/pending/run")
def index_pending_run(repo: str) -> dict:
    """Start a queued index update now instead of waiting out the quiet period."""
    _require_kopia(repo)
    from ..core import reindexq
    return reindexq.run_now(repo)


@router.get("/index/runs")
def index_runs(limit: int = 100) -> dict:
    """ Per-run index history, ALL repos in one list (newest first) - the Logs tab's
    Indexing section. Records cover every outcome: the worker's stage timings, a timeout, or a
    launch failure (the app writes the record, so a killed run is still on the list)."""
    from ..core import runhistory
    return {"runs": runhistory.read_tail(limit=min(max(limit, 1), 500))}


@router.post("/index/build")
def post_index_build(repo: str, source: str | None = None, full: bool = False) -> dict:
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
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/index/freshness")
def index_freshness() -> dict:
    """How current every repo's data is - one call for the whole header strip, so the UI does not
    need a per-repo round trip just to say "indexed 3h ago"."""
    from ..main import nightly_error, nightly_success_ts, nightly_ts  # lazy: app.main imports this router
    from ..core import repos as repo_store
    from ..core.sources import last_listing_error

    out = []
    for key, cfg in REPOS.items():
        st = nightly_state(key)
        stored = nightly_error(key)
        row = {
            "repo": key, "name": cfg.get("name", key), "kind": cfg.get("kind", "kopia"),
            "via_server": repo_store.via_server(key),
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
    from ..core import pause
    return {"repos": out, "now": time.time(), "pause": pause.state()}


@router.post("/settings")
def post_settings(payload: dict) -> dict:
    kl = payload.pop("kopialens", None)
    bz = payload.pop("backblaze", None)
    if isinstance(kl, dict):
        from ..settings import OWN_ROUTE_SETTINGS
        own = [k for k in kl if k in OWN_ROUTE_SETTINGS]
        if own:
            where = OWN_ROUTE_SETTINGS[own[0]]
            raise HTTPException(status_code=400, detail=(
                f"{own[0]} isn't saved here. Set it in settings.json." if where == "settings.json"
                else f"{own[0]} isn't saved here. Change it in {where}."))

    def _apply(settings: dict) -> None:
        settings.update(payload)
        if isinstance(kl, dict):  # merge nested sub-dicts, don't clobber the whole block
            settings["kopialens"] = {**settings.get("kopialens", {}), **kl}
        if isinstance(bz, dict):
            settings["backblaze"] = {**settings.get("backblaze", {}), **bz}

    saved = mutate_settings(_apply)
    try:  # the debug log toggle applies immediately, no restart
        from ..core.applog import apply_logging_settings
        apply_logging_settings(saved)
    except Exception:  # noqa: BLE001 - a logging toggle must never fail a settings save
        pass
    return saved


@router.get("/settings/tables")
def get_table_prefs() -> dict:
    from ..core import uistate
    return uistate.tables()


@router.post("/settings/table")
def post_table_pref(payload: dict) -> dict:
    """Persist one table's Tabulator config (columns/sort) in ui_state.json."""
    from ..core import uistate
    table_id = payload.get("id")
    if not table_id:
        return {"ok": False}
    uistate.set_table(table_id, payload.get("config"))
    return {"ok": True}
