"""Everything-style search endpoints."""

from __future__ import annotations

import csv
import io
from functools import cmp_to_key

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from pydantic import BaseModel

from config import REPOS, repo_kind
from .errors import PASS_THROUGH
from ..core import paths as pathrules
from ..index import foldercache
from ..index.physical import file_detail, frees_for_files, storage_breakdown, stored_all_versions
from ..index.queries import (_RANK_SORTS, _SEARCH_SORT, EXPORT_HEADER, group_by_folder, landing,
                             search_export, search_paths, versions)

router = APIRouter(tags=["search"])


def _saved_folder_values(repo: str, rows: list[dict], filtered: bool) -> dict[str, dict]:
    """Last night's whole-folder values for unfiltered, whole-folder rows. A filtered list or a
    loose-files row describes other files, so it fills on screen instead ()."""
    if filtered:
        return {}
    return foldercache.saved_values(repo, [r["folder"] for r in rows if not r.get("loose")])


@router.get("/search/group-by-folder")
def search_group_by_folder(
    repo: str, depth: int = 3, q: str = "", source: str | None = None, ext: str | None = None,
    ext_exclude: str | None = None, deleted_only: bool = False, min_gb: float = 0, min_files: int = 0,
    sort: str = "physical", direction: str = "desc", limit: int = 500, min_depth: int = 0,
    loose: bool = False,
) -> dict:
    """Bloat finder: whole backed-up folders at `depth` (plus loose-files rows when asked), ranked by
    physical. Each row carries the saved exact values when it has them."""
    try:
        out = group_by_folder(
            repo, depth=depth, query=q, source=source, ext=ext, ext_exclude=ext_exclude,
            deleted_only=deleted_only, min_size=int(min_gb * 1_000_000_000), min_files=min_files,
            sort=sort, direction=direction, limit=limit, min_depth=min_depth, loose=loose,
        )
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    saved = _saved_folder_values(repo, out["rows"], bool(q or source or ext or ext_exclude or deleted_only))
    for r in out["rows"]:
        s = saved.get(r["folder"]) or {}
        r["physical"] = s.get("physical_newest")
        r["physical_all"] = s.get("physical")
        r["freeable"] = s.get("freeable")
        r["folder_versions"] = s.get("versions")
    return out


class FolderVersionsRequest(BaseModel):
    repo: str
    folders: list[str]


@router.post("/search/folder-versions")
def search_folder_versions(body: FolderVersionsRequest) -> dict:
    """How many distinct states each folder on screen has had (): one tree walk per source
    for the whole page (capped at 100 folders)."""
    from ..index.holding import folder_states
    folders = list(dict.fromkeys(f for f in body.folders if f))[:100]
    try:
        states = folder_states(body.repo, folders)
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"versions": {f: st["versions"] for f, st in states.items()}}


class FolderValuesRequest(BaseModel):
    repo: str
    folder: str
    loose: bool = False
    q: str = ""
    source: str | None = None
    ext: str | None = None
    ext_exclude: str | None = None
    deleted_only: bool = False


@router.post("/search/folder-values")
def search_folder_values(body: FolderValuesRequest) -> dict:
    """One Search folder row's exact values: Physical (newest versions, each piece once), Physical
    (all) and Reclaimable - over the files the row stands for: the whole folder, only its loose
    files, and with a filter on only the matching ones. Unfiltered whole folders use the saved
    nightly result when there is one."""
    filters = {"query": body.q, "source": body.source, "ext": body.ext, "ext_exclude": body.ext_exclude,
               "deleted_only": body.deleted_only}
    filtered = any(filters.values())
    try:
        if not filtered and not body.loose:
            d = foldercache.compute(body.repo, body.folder)
            if d.get("physical_newest") is None:       # a stale entry saved before it existed
                d = foldercache.compute(body.repo, body.folder, allow_stale=False)
        else:
            from ..index.physical import folder_detail
            d = folder_detail(body.repo, None, body.folder, filters=filters if filtered else None,
                              loose=body.loose)
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"folder": body.folder, "loose": body.loose, "physical": d.get("physical_newest"),
            "physical_all": d.get("physical"), "freeable": d.get("freeable"),
            "files": d.get("files"), "logical": d.get("logical")}


def _bounds(over_gb: float, largest: bool, min_gb: float, max_gb: float) -> tuple[int, int]:
    min_size = int(min_gb * 1_000_000_000) if min_gb else (int(over_gb * 1_000_000_000) if over_gb else 0)
    if largest and not min_size:
        min_size = 1
    max_size = int(max_gb * 1_000_000_000) if max_gb else 0
    return min_size, max_size


@router.get("/search")
def search(
    repo: str,
    q: str = "",
    deleted_only: bool = False,
    largest: bool = False,
    over_gb: float = 0,
    source: str | None = None,
    ext: str | None = None,
    ext_exclude: str | None = None,
    sort: str = "size",
    dir: str = "desc",
    limit: int = 500,
    offset: int = 0,
    confirm_exists: bool = False,
    min_gb: float = 0,
    max_gb: float = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    first_from: str | None = None,
    min_versions: int = 0,
    shared_min: int = 0,
    dedupe: bool = False,
    dedupe_by: str = "path",
) -> dict:
    min_size, max_size = _bounds(over_gb, largest, min_gb, max_gb)
    # The default landing view (no query/filters, size-desc, first page) is served from a cached,
    # background-revalidated result so opening the Search tab is instant.
    is_landing = (not q and not deleted_only and not largest and not over_gb and not source and not ext
                  and not ext_exclude and not min_gb and not max_gb and not date_from and not date_to
                  and not first_from and not min_versions and not shared_min and not dedupe
                  and not confirm_exists and offset == 0 and sort in _SEARCH_SORT and dir in ("asc", "desc"))
    try:
        if is_landing:
            res = landing(repo, limit=limit, sort=sort, direction=dir)
        else:
            res = search_paths(
                repo, query=q, deleted_only=deleted_only, min_size=min_size,
                source=source or None, ext=ext or None, ext_exclude=ext_exclude or None,
                sort=sort, direction=dir, limit=limit, offset=offset, confirm_exists=confirm_exists,
                max_size=max_size, date_from=date_from or None, date_to=date_to or None,
                first_from=first_from or None,
                min_versions=min_versions, shared_min=shared_min, dedupe=dedupe, dedupe_by=dedupe_by,
            )
        return {"results": res["rows"], "total": res["total"], "offset": res["offset"],
                "limit": res["limit"], "ranked": res.get("ranked")}
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _row_cmp(field: str, desc: bool):
    """Comparator for result rows on one sort column: empty values last, then path as a tiebreak."""
    def cmp(a: dict, b: dict) -> int:
        va, vb = a.get(field), b.get(field)
        if va is None or vb is None:
            if va is None and vb is None:
                return 0
            return 1 if va is None else -1
        if va != vb:
            r = -1 if va < vb else 1
            return -r if desc else r
        pa, pb = a.get("full_path") or "", b.get("full_path") or ""
        return (pa > pb) - (pa < pb)
    return cmp


# Columns folder rows have, in file-row field names; any other sort lists folders first.
_FOLDER_SORT = {"size": "size", "physical": "physical", "full_path": "full_path", "name": "name",
                "last_seen": "last_seen", "first_seen": "first_seen", "source": "source",
                "stored": "stored_all_versions", "frees": "frees_all_versions",
                "versions": "versions"}


def _ranked_cmp(field: str, desc: bool, by_path: bool):
    """Comparator matching the SQL order of a ranked sort: rows with a value first by value, the
    rest by last seen (newest first), then source and path."""
    def cmp(a: dict, b: dict) -> int:
        va, vb = a.get(field), b.get(field)
        if va is not None and vb is not None and va != vb:
            r = -1 if va < vb else 1
            return -r if desc else r
        if (va is None) != (vb is None):
            return 1 if va is None else -1
        la, lb = a.get("last_seen") or "", b.get("last_seen") or ""
        if la != lb:
            return -1 if la > lb else 1
        ka = (a.get("full_path") or "",) if by_path else (a.get("source") or "", a.get("full_path") or "")
        kb = (b.get("full_path") or "",) if by_path else (b.get("source") or "", b.get("full_path") or "")
        return (ka > kb) - (ka < kb)
    return cmp


@router.get("/search/mixed")
def search_mixed(
    repo: str, depth: int = 3, folder_min_gb: float = 0, folder_min_files: int = 0, min_depth: int = 0,
    q: str = "", deleted_only: bool = False, largest: bool = False, over_gb: float = 0,
    source: str | None = None, ext: str | None = None, ext_exclude: str | None = None,
    sort: str = "size", dir: str = "desc", limit: int = 500, offset: int = 0,
    min_gb: float = 0, max_gb: float = 0, date_from: str | None = None, date_to: str | None = None,
    first_from: str | None = None, min_versions: int = 0, shared_min: int = 0,
    dedupe: bool = False, dedupe_by: str = "path", loose: bool = False,
) -> dict:
    """Files and folders in one list. Files page exactly as /search; the folder rollups (top 500)
    are placed into the page their sort position falls in, so every folder appears once."""
    min_size, max_size = _bounds(over_gb, largest, min_gb, max_gb)
    kw = dict(query=q, deleted_only=deleted_only, min_size=min_size, source=source or None,
              ext=ext or None, ext_exclude=ext_exclude or None, sort=sort, direction=dir,
              max_size=max_size, date_from=date_from or None, date_to=date_to or None,
              first_from=first_from or None, min_versions=min_versions, shared_min=shared_min,
              dedupe=dedupe, dedupe_by=dedupe_by)
    desc = dir != "asc"
    try:
        page = search_paths(repo, limit=limit, offset=offset, **kw)
        groups = group_by_folder(
            repo, depth=depth, query=q, source=source or None, ext=ext or None,
            ext_exclude=ext_exclude or None, deleted_only=deleted_only,
            min_size=int(folder_min_gb * 1_000_000_000), min_files=folder_min_files,
            sort="logical" if sort == "size" else "physical", direction=dir, limit=500, min_depth=min_depth,
            loose=loose,
        )["rows"]
        field = _FOLDER_SORT.get(sort)
        prev = (search_paths(repo, limit=1, offset=offset - 1, **kw)["rows"] or [None])[0] if offset and field else None
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    files = page["rows"]
    saved = _saved_folder_values(repo, groups, bool(q or source or ext or ext_exclude or deleted_only))
    folders = []
    for g in groups:
        s = saved.get(g["folder"]) or {}
        folders.append({"kind": "folder", "full_path": g["folder"], "name": pathrules.basename(g["folder"]),
                        "loose": bool(g.get("loose")),
                        "size": g["logical"], "physical": s.get("physical_newest"), "files": g["files"],
                        "deleted_files": g["deleted_files"], "last_seen": g["last_seen"],
                        "first_seen": g["first_seen"], "sources": g["sources"], "source": g["source"],
                        "stored_all_versions": s.get("physical"), "frees_all_versions": s.get("freeable"),
                        "versions": s.get("versions")})
    ranked = page.get("ranked") or {}
    if sort in ("stored", "frees") and not ranked.get("available"):
        field = "physical"     # no nightly list yet: files fell back to Physical, folders follow
    if field is None:
        merged = (folders if offset == 0 else []) + files
    else:
        cmp = (_ranked_cmp(field, desc, dedupe and dedupe_by != "content") if sort in ("stored", "frees")
               and field != "physical" else _row_cmp(field, desc))
        last = files[-1] if files else None
        final = offset + len(files) >= page["total"]
        pick = [f for f in folders
                if (prev is None or cmp(f, prev) >= 0) and (final or last is None or cmp(f, last) < 0)]
        merged = sorted(pick + files, key=cmp_to_key(cmp))
    return {"results": merged, "total": page["total"], "folders": len(folders),
            "offset": page["offset"], "limit": page["limit"]}


_ALL_REPOS_DEPTH = 20_000


@router.get("/search/all-repos")
def search_all_repos(
    q: str = "", deleted_only: bool = False, largest: bool = False, over_gb: float = 0,
    ext: str | None = None, ext_exclude: str | None = None, sort: str = "size", dir: str = "desc",
    limit: int = 500, offset: int = 0, min_gb: float = 0, max_gb: float = 0,
    date_from: str | None = None, date_to: str | None = None, first_from: str | None = None,
    min_versions: int = 0, shared_min: int = 0, dedupe: bool = False, dedupe_by: str = "path",
) -> dict:
    """One list across every kopia repository, sorted together. Each repo returns its top
    offset+limit rows, so paging stops at a fixed depth; a repo whose index can't be read is
    skipped and named."""
    if offset + limit > _ALL_REPOS_DEPTH:
        raise HTTPException(status_code=400, detail=f"Across all repositories, results stop at {_ALL_REPOS_DEPTH:,}. Narrow the search to see more.")
    if sort in _RANK_SORTS or sort not in _SEARCH_SORT:
        sort = "physical"
    min_size, max_size = _bounds(over_gb, largest, min_gb, max_gb)
    rows: list[dict] = []
    total = 0
    searched: list[str] = []
    skipped: list[dict] = []
    for rid in REPOS:
        if repo_kind(rid) != "kopia":
            continue
        try:
            res = search_paths(
                rid, query=q, deleted_only=deleted_only, min_size=min_size, ext=ext or None,
                ext_exclude=ext_exclude or None, sort=sort, direction=dir, limit=offset + limit, offset=0,
                max_size=max_size, date_from=date_from or None, date_to=date_to or None,
                first_from=first_from or None, min_versions=min_versions, shared_min=shared_min,
                dedupe=dedupe, dedupe_by=dedupe_by,
            )
        except PASS_THROUGH as exc:
            skipped.append({"repo": rid, "reason": type(exc).__name__})
            continue
        except Exception as exc:  # noqa: BLE001 - one broken index must not hide the others
            skipped.append({"repo": rid, "reason": str(exc)})
            continue
        searched.append(rid)
        total += res["total"]
        for r in res["rows"]:
            r["repo"] = rid
        rows.extend(res["rows"])
    rows.sort(key=cmp_to_key(_row_cmp(_SEARCH_SORT[sort], dir != "asc")))
    return {"results": rows[offset:offset + limit], "total": total, "offset": offset, "limit": limit,
            "sort": sort, "searched": searched, "skipped": skipped, "max_depth": _ALL_REPOS_DEPTH}


class VersionRow(BaseModel):
    source: str
    full_path: str


class VersionsRequest(BaseModel):
    repo: str
    rows: list[VersionRow]


@router.post("/search/versions")
def search_versions(payload: VersionsRequest) -> dict:
    """All-versions sizes for the rows on screen: Stored (every version this path has held, shared
    chunks once) and Frees (what removing this file, every version, gives back - the file panel's
    Reclaimable). Never calls kopia; a value whose piece lists are not indexed yet comes back null."""
    rows = payload.rows[:200]
    handles = [(r.source, r.full_path) for r in rows]
    try:
        stored = stored_all_versions(payload.repo, handles)
        frees = frees_for_files(payload.repo, handles)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"rows": [{"source": r.source, "full_path": r.full_path,
                      "stored": stored.get((r.source, r.full_path)),
                      "frees": frees.get((r.source, r.full_path))} for r in rows]}


def _csv_rows(cur, registered=()):
    """Stream the export, always closing the read cursor.

    try/finally rather than a close() after the loop: a cancelled download (the client goes away,
    or the user hits stop) abandons this generator part-way, so the final line never runs and the
    DuckDB cursor stayed open until the generator happened to be garbage collected."""
    try:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(EXPORT_HEADER)
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for row in batch:
                writer.writerow(row)
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    finally:
        for name in registered:
            try:
                cur.unregister(name)
            except Exception:  # noqa: BLE001 - already gone
                pass
        try:
            cur.close()
        except Exception:  # noqa: BLE001 - already closed / connection gone
            pass


@router.get("/search/export")
def export(
    repo: str, q: str = "", deleted_only: bool = False, largest: bool = False,
    over_gb: float = 0, source: str | None = None, ext: str | None = None,
    ext_exclude: str | None = None, sort: str = "size", dir: str = "desc",
    min_gb: float = 0, max_gb: float = 0, date_from: str | None = None, date_to: str | None = None,
    first_from: str | None = None, min_versions: int = 0, shared_min: int = 0, dedupe: bool = False,
    dedupe_by: str = "path",
) -> StreamingResponse:
    min_size, max_size = _bounds(over_gb, largest, min_gb, max_gb)
    cur, registered = search_export(repo, query=q, deleted_only=deleted_only, min_size=min_size,
                        source=source or None, ext=ext or None, ext_exclude=ext_exclude or None,
                        sort=sort, direction=dir,
                        max_size=max_size, date_from=date_from or None, date_to=date_to or None,
                        first_from=first_from or None,
                        min_versions=min_versions, shared_min=shared_min, dedupe=dedupe, dedupe_by=dedupe_by)

    return StreamingResponse(
        _csv_rows(cur, registered), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="kopialens_search_repo{repo}.csv"'},
    )


@router.get("/search/versions")
def file_versions(repo: str, source: str | None = None, full_path: str | None = None) -> dict:
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    return {"versions": versions(repo, source, full_path)}


@router.get("/search/detail")
def detail(repo: str, source: str | None = None, full_path: str | None = None) -> dict:
    """Accurate per-file footprint: logical/physical/freeable + dedup chunks + shared paths."""
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    try:
        return file_detail(repo, source, full_path)
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/search/breakdown")
def breakdown(repo: str, source: str | None = None, full_path: str | None = None) -> dict:
    """Where a file's storage goes if it is deleted: freed bytes, identical copies, and the other
    files sharing its data."""
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    try:
        return storage_breakdown(repo, source, full_path)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
