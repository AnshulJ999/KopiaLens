"""Everything-style search endpoints."""

from __future__ import annotations

import csv
import io
from functools import cmp_to_key

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from pydantic import BaseModel

from config import REPOS, repo_kind
from . import freeids
from .actions import FULL_LIST_CAP
from .errors import PASS_THROUGH, RequestCancelled
from ..core import exportname
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
    loose: bool = False, mode: str = "deepest",
) -> dict:
    """Bloat finder: whole backed-up folders at `depth` (plus loose-files rows when asked), ranked by
    physical. Each row carries the saved exact values when it has them."""
    try:
        out = group_by_folder(
            repo, depth=depth, query=q, source=source, ext=ext, ext_exclude=ext_exclude,
            deleted_only=deleted_only, min_size=int(min_gb * 1_000_000_000), min_files=min_files,
            sort=sort, direction=direction, limit=limit, min_depth=min_depth, loose=loose, mode=mode,
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
    from ..core.sources import sources_index
    from ..index.holding import folder_states
    folders = list(dict.fromkeys(f for f in body.folders if f))[:100]
    try:
        index = sources_index(body.repo)
        # Counted once per snapshot listing: a folder seen before answers from memory.
        sig = (body.repo, tuple(sorted((s, len(v), (v[-1].get("id") if v else None)) for s, v in index.items())))
        known = _FOLDER_VERSIONS.setdefault(sig, {})
        while len(_FOLDER_VERSIONS) > 4:
            _FOLDER_VERSIONS.pop(next(iter(_FOLDER_VERSIONS)))
        todo = [f for f in folders if f not in known]
        if todo:
            known.update({f: st["versions"] for f, st in folder_states(body.repo, todo, index=index).items()})
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"versions": {f: known[f] for f in folders}}


_FOLDER_VERSIONS: dict[tuple, dict[str, int]] = {}


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


def _no_ids(rows: list) -> list:
    """Search rows go out without the object id: nothing on screen uses it ()."""
    for r in rows:
        r.pop("content_id", None)
    return rows


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
        return {"results": _no_ids(res["rows"]), "total": res["total"], "offset": res["offset"],
                "limit": res["limit"], "ranked": res.get("ranked")}
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _sort_value(row: dict, field: str):
    """A row's value for the sort. A folder with no exact Physical is placed by its rollup's own
    Physical; so is a folder whose Stored or Frees can only be worked out on screen (filtered
    search, own files), so it lands near its place instead of after every file."""
    v = row.get(field)
    if v is None and (field == "physical" or (field in _STAND_IN and row.get("_stand_in"))):
        return row.get("_physical_rank")
    return v


def _row_cmp(field: str, desc: bool):
    """Comparator for result rows on one sort column: empty values last, then path as a tiebreak."""
    def cmp(a: dict, b: dict) -> int:
        va, vb = _sort_value(a, field), _sort_value(b, field)
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
# Saved only for whole, unfiltered folders; other rows are placed by their Physical meanwhile.
_STAND_IN = {"stored_all_versions", "frees_all_versions"}


def _ranked_cmp(field: str, desc: bool):
    """Comparator matching the leading keys of a search sort's SQL order: rows with a value first
    by value, ties and the rest by last seen (newest first). Rows equal on both compare equal: the
    SQL's further tiebreaks differ by mode, so folders are placed by these two keys only."""
    def cmp(a: dict, b: dict) -> int:
        va, vb = _sort_value(a, field), _sort_value(b, field)
        if va is not None and vb is not None and va != vb:
            r = -1 if va < vb else 1
            return -r if desc else r
        if (va is None) != (vb is None):
            return 1 if va is None else -1
        la, lb = a.get("last_seen") or "", b.get("last_seen") or ""
        if la != lb:
            return -1 if la > lb else 1
        return 0
    return cmp


@router.get("/search/mixed")
def search_mixed(
    repo: str, depth: int = 3, folder_min_gb: float = 0, folder_min_files: int = 0, min_depth: int = 0,
    q: str = "", deleted_only: bool = False, largest: bool = False, over_gb: float = 0,
    source: str | None = None, ext: str | None = None, ext_exclude: str | None = None,
    sort: str = "size", dir: str = "desc", limit: int = 500, offset: int = 0,
    min_gb: float = 0, max_gb: float = 0, date_from: str | None = None, date_to: str | None = None,
    first_from: str | None = None, min_versions: int = 0, shared_min: int = 0,
    dedupe: bool = False, dedupe_by: str = "path", loose: bool = False, mode: str = "deepest",
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
    field = _FOLDER_SORT.get(sort)
    try:
        prev = None
        if offset and field:
            # One query for the page and the row before it (where this page's folders start).
            page = search_paths(repo, limit=limit + 1, offset=offset - 1, **kw)
            prev = page["rows"][0] if page["rows"] else None
            page.update(rows=page["rows"][1:], offset=offset, limit=limit)
        else:
            page = search_paths(repo, limit=limit, offset=offset, **kw)
        groups = group_by_folder(
            repo, depth=depth, query=q, source=source or None, ext=ext or None,
            ext_exclude=ext_exclude or None, deleted_only=deleted_only,
            min_size=int(folder_min_gb * 1_000_000_000), min_files=folder_min_files,
            sort="logical" if sort == "size" else "physical", direction=dir, limit=500, min_depth=min_depth,
            loose=loose, mode=mode,
        )["rows"]
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    files = page["rows"]
    filtered = bool(q or source or ext or ext_exclude or deleted_only)
    saved = _saved_folder_values(repo, groups, filtered)
    folders = []
    for g in groups:
        s = saved.get(g["folder"]) or {}
        folders.append({"kind": "folder", "full_path": g["folder"], "name": pathrules.basename(g["folder"]),
                        "loose": bool(g.get("loose")),
                        "size": g["logical"], "physical": s.get("physical_newest"), "files": g["files"],
                        "deleted_files": g["deleted_files"], "last_seen": g["last_seen"],
                        "first_seen": g["first_seen"], "sources": g["sources"], "source": g["source"],
                        "stored_all_versions": s.get("physical"), "frees_all_versions": s.get("freeable"),
                        "versions": s.get("versions"), "_physical_rank": g.get("phys"),
                        "_stand_in": filtered or bool(g.get("loose"))})
    ranked = page.get("ranked") or {}
    if sort in ("stored", "frees") and not ranked.get("available"):
        field = "physical"     # no nightly list yet: files fell back to Physical, folders follow
    if field is None:
        merged = (folders if offset == 0 else []) + files
    else:
        # Each page takes the folders in [row before it, its last row): the windows of consecutive
        # pages meet without overlapping, so every folder lands on exactly one page. The sort is
        # stable and files go first, so files keep the SQL order and a tied folder follows them.
        cmp = _ranked_cmp(field, desc)
        last = files[-1] if files else None
        final = offset + len(files) >= page["total"]
        pick = [f for f in folders
                if (prev is None or cmp(f, prev) >= 0) and (final or last is None or cmp(f, last) < 0)]
        merged = sorted(files + pick, key=cmp_to_key(cmp))
    for r in merged:
        r.pop("_physical_rank", None)
        r.pop("_stand_in", None)
    return {"results": _no_ids(merged), "total": page["total"], "folders": len(folders),
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
    for rid in list(REPOS):
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
        except RequestCancelled:
            raise
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
    return {"results": _no_ids(rows[offset:offset + limit]), "total": total, "offset": offset, "limit": limit,
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
    dedupe_by: str = "path", filename: str = "",
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
        headers={"Content-Disposition": f'attachment; filename="{exportname.client_name(filename) or exportname.export_name("search", q, exportname.repo_label(repo))}"'},
    )


@router.get("/search/versions")
def file_versions(repo: str, source: str | None = None, full_path: str | None = None) -> dict:
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    rows = versions(repo, source, full_path)
    contents = freeids.contents(rows)          # before Free strips the ids it counts
    return {"versions": freeids.versions(repo, rows), "contents": contents}


@router.get("/search/detail")
def detail(repo: str, source: str | None = None, full_path: str | None = None, others_cap: int = 500) -> dict:
    """Accurate per-file footprint: logical/physical/freeable + dedup chunks + shared paths
    (the first ``others_cap``; the panel's "Show all" asks for every one)."""
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    try:
        return freeids.detail(repo, file_detail(repo, source, full_path,
                                                others_cap=max(1, min(others_cap, FULL_LIST_CAP))))
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
        return freeids.breakdown(repo, storage_breakdown(repo, source, full_path))
    except PASS_THROUGH:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
