"""Backblaze (read-only) API - the BZ analog of the kopia routes.

Every endpoint is a pure read over the SQLite index in app/backblaze/queries.py. This router is
entirely separate from the kopia/DuckDB routes (prefix /api/backblaze) so adding Backblaze never
touches kopia code. The only cross-engine spots are the two Excluded endpoints, which decorate the
BZ-native coverage with a Kopia cross-coverage column (the unified "who backs this?" view).
"""

from __future__ import annotations

import collections
import csv
import io
import threading
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..backblaze import indexer, paths
from ..backblaze import queries as bzq

router = APIRouter(prefix="/api/backblaze", tags=["backblaze"])

# Short TTL cache for the Excluded views: each drill does a folder scan + 2 kopia DuckDB queries
# (the slow part), so repeated navigation within the window is instant. LRU-capped (mirrors
# index/excluded._LIST_CACHE): the keys are user-driven folder paths, so the cache used to grow
# unbounded across a long session drilling many folders.
_EXCL_CACHE: collections.OrderedDict[str, tuple] = collections.OrderedDict()
_EXCL_TTL = 120
_EXCL_CACHE_MAX = 200
_EXCL_LOCK = threading.Lock()  # sync FastAPI handlers run in a threadpool - the get/put
# check->move_to_end->popitem sequence can interleave across requests otherwise


def _excl_cache_get(key: str):
    with _EXCL_LOCK:
        hit = _EXCL_CACHE.get(key)
        if hit is not None:
            _EXCL_CACHE.move_to_end(key)
        return hit


def _excl_cache_put(key: str, val: tuple) -> None:
    with _EXCL_LOCK:
        _EXCL_CACHE[key] = val
        _EXCL_CACHE.move_to_end(key)
        while len(_EXCL_CACHE) > _EXCL_CACHE_MAX:
            _EXCL_CACHE.popitem(last=False)


def _require_data() -> None:
    if not paths.exists():
        # 404, not 5xx: an install without Backblaze data is a normal state, not a server fault.
        raise HTTPException(status_code=404, detail=f"Can't reach the Backblaze data folder {paths.bzdata_root()}. "
                                                    "Check that it's there, or set it in Settings > Backblaze.")


def apply_folder_setting(settings: dict | None = None) -> str:
    """Apply the Backblaze folder setting now (no restart): point the reader at it, drop cached
    views, and queue a rebuild when the folder changed. The rebuild itself notices another
    computer's data and sets the old index aside."""
    old = paths.bzdata_root()
    new = paths.apply_configured_root(settings)
    if new != old:                    # detection, churn rows and drive filters belong to the old folder
        paths.forget_platform(old)
        paths.forget_platform(new)
        bzq._CHURN_CACHE.clear()
    with _EXCL_LOCK:
        _EXCL_CACHE.clear()
    try:
        from ..index.excluded import forget_listings
        forget_listings()
    except Exception:  # noqa: BLE001 - only a cache
        pass
    if new != old and paths.exists():
        from ..core import reindexq, repos
        if repos.exists(repos.BACKBLAZE_ID):
            reindexq.request(repos.BACKBLAZE_ID, wait=0, reason="first")
            reindexq.run_now(repos.BACKBLAZE_ID)   # a user-changed folder starts now, not on the next tick
            try:   # the status/UI keeps waiting for the rebuild even while a kopia pass holds the slot
                from ..backblaze import bbdb
                con = bbdb.connect()
                bbdb.kv_set(con, "folder_change_pending", "1")
                con.commit()
            except Exception as exc:  # noqa: BLE001 - the rebuild itself is already queued
                indexer.log.warning("could not mark the Backblaze folder change pending: %s", exc)
    return new


def _cross_check() -> bool:
    """The Kopia cross-check compares with THIS computer's backups: off for another computer's data."""
    return paths.is_this_computer()


def _bz_settings() -> dict:
    try:
        from ..settings import load_settings
        return load_settings().get("backblaze", {})
    except Exception:  # noqa: BLE001
        return {}


def _collapse_prefixes() -> list:
    return _bz_settings().get("collapse_prefixes", []) or []


def _archive_path() -> str:
    return _bz_settings().get("eventlog_archive_path", "")


# ---- index status / manual refresh -----------------------------------------------------
@router.get("/status")
def status() -> dict:
    st = indexer.state()
    out = {"building": bool(st.get("building")), "available": paths.exists(),
           "last_run": st.get("last_run"), "last_error": st.get("last_error")}
    try:
        out.update(bzq.status())
    except Exception as exc:  # noqa: BLE001 - report, don't 500 (lets the UI show "not built yet")
        out["error"] = str(exc)
    out.update(indexer.freshness())  #: pending / last_check_ts / last_reindex_ts
    try:
        from ..core import reindexq
        out["rebuild_queued"] = any(p.get("repo") == "bz" for p in reindexq.pending())
    except Exception:  # noqa: BLE001 - a queue read must not break status
        out["rebuild_queued"] = False
    return out


@router.post("/refresh")
def refresh(force: bool = False) -> dict:
    """Non-blocking: starts the refresh in a background thread (which itself hands off to the
    subprocess worker, see indexer.refresh_via_worker) and returns immediately. The frontend polls
    GET /status (`building`) and reloads the active tab once it clears."""
    _require_data()
    if indexer.state().get("building"):
        return {"started": False, "reason": "already running"}
    threading.Thread(target=indexer.refresh_via_worker, kwargs={"force": force}, daemon=True).start()
    return {"started": True}


# ---- Dashboard / overview / stats ------------------------------------------------------
@router.get("/overview")
def overview() -> dict:
    _require_data()
    return bzq.overview()


@router.get("/stats")
def stats() -> dict:
    _require_data()
    return bzq.stats()


@router.get("/churn")
def churn(top: int = 200, depth: int = 2, collapse: bool = True) -> dict:
    _require_data()
    return bzq.churn(top=top, depth=depth, collapse=_collapse_prefixes() if collapse else None)


# ---- Search (FTS5) ---------------------------------------------------------------------
@router.get("/search")
def search(q: str = Query("", description="substring over the full path"),
           drive: str | None = None, ext: str | None = None, ext_exclude: str | None = None,
           status: str = "all", min_size: int = 0, sort: str = "size", direction: str = "desc",
           limit: int = 500, offset: int = 0) -> dict:
    _require_data()
    return bzq.search(query=q, drive=drive, ext=ext, ext_exclude=ext_exclude, status=status,
                      min_size=min_size, sort=sort, direction=direction, limit=limit, offset=offset)


@router.get("/path-status")
def path_status(path: str) -> dict:
    """Whether Backblaze backs up this exact path: {"found", "excluded", "rule", "size"}."""
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    _require_data()
    want = path.lower()
    rows = bzq.search(query=path, limit=50)["rows"]
    hit = next((r for r in rows if str(r.get("full_path") or "").lower() == want), None)
    if not hit:
        return {"found": False}
    return {"found": True, "excluded": bool(hit.get("excluded")), "rule": hit.get("excl_rule"),
            "size": hit.get("size")}


def _bz_export_name(filename: str, q: str) -> str:
    from ..core import exportname
    return exportname.client_name(filename) or exportname.export_name("search", q, "BZ")


@router.get("/export")
def export(q: str = "", drive: str | None = None, ext: str | None = None, ext_exclude: str | None = None,
           status: str = "all", min_size: int = 0, sort: str = "size", direction: str = "desc",
           filename: str = "") -> StreamingResponse:
    _require_data()
    cur = bzq.export_iter(query=q, drive=drive, ext=ext, ext_exclude=ext_exclude, status=status,
                          min_size=min_size, sort=sort, direction=direction)

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["drive", "full_path", "name", "ext", "size", "excluded", "excl_rule"])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for r in cur:
            w.writerow([r["drive"], r["full_path"], r["name"], r["ext"], r["size"],
                        r["excluded"], r["excl_rule"]])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{_bz_export_name(filename, q)}"'})


@router.get("/group-by-folder")
def group_by_folder(drive: str | None = None, depth: int = 2, status: str = "backed",
                    min_files: int = 1, top: int = 300, collapse: bool = True) -> dict:
    _require_data()
    return {"depth": depth, "rows": bzq.group_by_folder(
        drive=drive, depth=depth, status=status, min_files=min_files, top=top,
        collapse=_collapse_prefixes() if collapse else None)}


# ---- Tree (per-drive folder browser; also used by the BZ Excluded drill) ----------------
@router.get("/folder")
def folder(prefix: str) -> dict:
    _require_data()
    return {"prefix": prefix, "children": bzq.folder_children(prefix)}


# ---- Exclusions (4 layers + impact + naming-gap + generate) ----------------------------
@router.get("/exclusions")
def exclusions() -> dict:
    _require_data()
    return bzq.exclusions()


@router.post("/exclusions/generate")
def exclusions_generate(payload: dict) -> dict:
    return bzq.generate_exclusion(payload.get("kind", "folder"), payload.get("value", ""))


@router.get("/naming-gap")
def naming_gap(q: str, depth: int = 2) -> dict:
    _require_data()
    return {"query": q, "rows": bzq.naming_gap(q, depth=depth)}


# ---- Queue / Sessions / Health / Dupes -------------------------------------------------
@router.get("/queue")
def queue(depth: int = 1, collapse: bool = True) -> dict:
    _require_data()
    return bzq.queue(depth=depth, collapse=_collapse_prefixes() if collapse else None)


@router.get("/sessions")
def sessions(limit: int = 400) -> dict:
    _require_data()
    return bzq.sessions(limit=limit)


@router.post("/import-archive")
def import_archive(payload: dict | None = None) -> dict:
    """One-time import of the copied (readable) rotated eventlog .zips -> permanent events_archive.
    Path from the request or the configured eventlog_archive_path."""
    path = (payload or {}).get("path") or _archive_path()
    if not path:
        raise HTTPException(status_code=400, detail="no archive path configured")
    return indexer.import_eventlog_archive(path)


@router.get("/health")
def health() -> dict:
    _require_data()
    return bzq.health()


@router.get("/dupes")
def dupes(min_size: int = 50_000_000, top: int = 200) -> dict:
    _require_data()
    return {"rows": bzq.dupes(min_size=min_size, top=top)}


# ---- BZ-native Excluded views (+ Kopia cross-coverage column) --------------------------
@router.get("/excluded/drives")
def excluded_drives() -> dict:
    """Per-drive BZ backed/excluded coverage (native, exact) + a Kopia cross-coverage column."""
    _require_data()
    hit = _excl_cache_get("*drives*")
    if hit and time.time() - hit[0] < _EXCL_TTL:
        return hit[1]
    ov = bzq.overview()
    kd: dict = {}
    cross = _cross_check()
    if cross:
        try:
            from ..index.excluded import kopia_drive_backed
            kd = kopia_drive_backed()
        except Exception:  # noqa: BLE001 - cross-coverage is optional
            kd = {}
    drives = []
    for r in ov["rows"]:
        key = str(r["drive"])
        kf, kb = kd.get(key, (0, 0))
        drives.append({
            "drive": key, "label": r["label"], "path": r["root"], "total_files": r["total"],
            "backed_files": r["backed_files"], "backed_bytes": r["backed_bytes"],
            "excluded_files": r["excluded_files"], "excluded_bytes": r["excluded_bytes"],
            "auth_files": r["auth_files"], "diff": r["diff"],
            "kopia_backed_files": kf, "kopia_backed_bytes": kb,
        })
    result = {"drives": drives, "grand_files": ov["auth_grand_files"], "grand_bytes": ov["auth_grand_bytes"],
              "cross_check": cross}
    _excl_cache_put("*drives*", (time.time(), result))
    return result


@router.get("/excluded/list")
def excluded_list(prefix: str) -> dict:
    """BZ-native folder coverage under `prefix` (backed/excluded per child). Fast - one indexed scan.
    The Kopia cross-coverage column is fetched separately (/excluded/kopia-coverage) so navigation is
    never blocked on the slower DuckDB queries (lazy fill in the frontend)."""
    _require_data()
    key = prefix.lower()
    hit = _excl_cache_get(key)
    if hit and time.time() - hit[0] < _EXCL_TTL:
        return hit[1]
    items = bzq.folder_children(prefix)
    result = {"prefix": prefix, "items": items, "count": len(items)}
    _excl_cache_put(key, (time.time(), result))
    return result


@router.get("/excluded/kopia-coverage")
def excluded_kopia_coverage(prefix: str) -> dict:
    """Kopia cross-coverage per immediate child under `prefix` (the slow DuckDB part of the Excluded
    view, loaded lazily). Returns {seg_lc: {bytes, files, repos}}."""
    ckey = "kc:" + prefix.lower()
    hit = _excl_cache_get(ckey)
    if hit and time.time() - hit[0] < _EXCL_TTL:
        return hit[1]
    cov: dict = {}
    try:
        if _cross_check():
            from ..index.excluded import kopia_backed_children
            for seg, b in kopia_backed_children(prefix).items():
                cov[seg] = {"bytes": int(b["logical"]), "files": int(b["files"]), "repos": sorted(b["repos"])}
    except Exception:  # noqa: BLE001 - cross-coverage is optional
        cov = {}
    result = {"prefix": prefix, "coverage": cov}
    _excl_cache_put(ckey, (time.time(), result))
    return result
