"""Backblaze (read-only) API - the BZ analog of the kopia routes.

Every endpoint is a pure read over the SQLite index in app/backblaze/queries.py. This router is
entirely separate from the kopia/DuckDB routes (prefix /api/backblaze) so adding Backblaze never
touches kopia code. The only cross-engine spots are the two Excluded endpoints, which decorate the
BZ-native coverage with a Kopia cross-coverage column (the unified "who backs this?" view).
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..backblaze import indexer, paths
from ..backblaze import queries as bzq

router = APIRouter(prefix="/api/backblaze", tags=["backblaze"])


def _require_data() -> None:
    if not paths.exists():
        raise HTTPException(status_code=503, detail=f"Backblaze data not found at {paths.bzdata_root()}")


# ---- index status / manual refresh -----------------------------------------------------
@router.get("/status")
def status() -> dict:
    st = indexer.state()
    out = {"building": bool(st.get("building")), "available": paths.exists(),
           "last_run": st.get("last_run")}
    try:
        out.update(bzq.status())
    except Exception as exc:  # noqa: BLE001 - report, don't 500 (lets the UI show "not built yet")
        out["error"] = str(exc)
    return out


@router.post("/refresh")
def refresh(force: bool = False) -> dict:
    _require_data()
    return indexer.refresh(force=force)


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
def churn(top: int = 200) -> dict:
    _require_data()
    return bzq.churn(top=top)


# ---- Search (FTS5) ---------------------------------------------------------------------
@router.get("/search")
def search(q: str = Query("", description="substring over the full path"),
           drive: str | None = None, ext: str | None = None, status: str = "all",
           min_size: int = 0, sort: str = "size", direction: str = "desc",
           limit: int = 500, offset: int = 0) -> dict:
    _require_data()
    return bzq.search(query=q, drive=drive, ext=ext, status=status, min_size=min_size,
                      sort=sort, direction=direction, limit=limit, offset=offset)


@router.get("/export")
def export(q: str = "", drive: str | None = None, ext: str | None = None, status: str = "all",
           min_size: int = 0, sort: str = "size", direction: str = "desc") -> StreamingResponse:
    _require_data()
    cur = bzq.export_iter(query=q, drive=drive, ext=ext, status=status, min_size=min_size,
                          sort=sort, direction=direction)

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
                             headers={"Content-Disposition": "attachment; filename=backblaze_search.csv"})


@router.get("/group-by-folder")
def group_by_folder(drive: str | None = None, depth: int = 2, status: str = "backed",
                    min_files: int = 1, top: int = 300) -> dict:
    _require_data()
    return {"depth": depth, "rows": bzq.group_by_folder(drive=drive, depth=depth, status=status,
                                                        min_files=min_files, top=top)}


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
def queue(depth: int = 1) -> dict:
    _require_data()
    return bzq.queue(depth=depth)


@router.get("/sessions")
def sessions(limit: int = 200) -> dict:
    _require_data()
    return bzq.sessions(limit=limit)


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
    ov = bzq.overview()
    kd: dict = {}
    try:
        from ..index.excluded import kopia_drive_backed
        kd = kopia_drive_backed()
    except Exception:  # noqa: BLE001 - cross-coverage is optional
        kd = {}
    drives = []
    for r in ov["rows"]:
        letter = str(r["drive"]).upper()
        kf, kb = kd.get(letter, (0, 0))
        drives.append({
            "drive": letter, "path": f"{letter}:\\", "total_files": r["total"],
            "backed_files": r["backed_files"], "backed_bytes": r["backed_bytes"],
            "excluded_files": r["excluded_files"], "excluded_bytes": r["excluded_bytes"],
            "auth_files": r["auth_files"], "diff": r["diff"],
            "kopia_backed_files": kf, "kopia_backed_bytes": kb,
        })
    return {"drives": drives, "grand_files": ov["auth_grand_files"], "grand_bytes": ov["auth_grand_bytes"]}


@router.get("/excluded/list")
def excluded_list(prefix: str) -> dict:
    """BZ folder coverage under `prefix` (native backed/excluded per child) + Kopia cross-column."""
    _require_data()
    items = bzq.folder_children(prefix)
    kc: dict = {}
    try:
        from ..index.excluded import kopia_backed_children
        kc = kopia_backed_children(prefix)
    except Exception:  # noqa: BLE001 - cross-coverage is optional
        kc = {}
    for it in items:
        b = kc.get(it["name"].lower())
        it["kopia_backed_bytes"] = int(b["logical"]) if b else 0
        it["kopia_backed_files"] = int(b["files"]) if b else 0
        it["kopia_repos"] = sorted(b["repos"]) if b else []
    return {"prefix": prefix, "items": items, "count": len(items)}
