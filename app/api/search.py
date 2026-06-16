"""Everything-style search endpoints."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..index.physical import file_detail
from ..index.queries import search_export, search_paths, versions

router = APIRouter(tags=["search"])

_EXPORT_COLS = ["source", "full_path", "name", "ext", "size", "physical", "versions",
                "first_seen", "last_seen", "in_latest"]


def _bounds(over_gb: float, largest: bool, min_gb: float, max_gb: float) -> tuple[int, int]:
    min_size = int(min_gb * 1_000_000_000) if min_gb else (int(over_gb * 1_000_000_000) if over_gb else 0)
    if largest and not min_size:
        min_size = 1
    max_size = int(max_gb * 1_000_000_000) if max_gb else 0
    return min_size, max_size


@router.get("/search")
def search(
    repo: str = "1",
    q: str = "",
    deleted_only: bool = False,
    largest: bool = False,
    over_gb: float = 0,
    source: str | None = None,
    ext: str | None = None,
    sort: str = "size",
    dir: str = "desc",
    limit: int = 500,
    offset: int = 0,
    confirm_exists: bool = False,
    min_gb: float = 0,
    max_gb: float = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    min_versions: int = 0,
    shared_min: int = 0,
) -> dict:
    min_size, max_size = _bounds(over_gb, largest, min_gb, max_gb)
    try:
        res = search_paths(
            repo, query=q, deleted_only=deleted_only, min_size=min_size,
            source=source or None, ext=ext or None, sort=sort, direction=dir,
            limit=limit, offset=offset, confirm_exists=confirm_exists,
            max_size=max_size, date_from=date_from or None, date_to=date_to or None,
            min_versions=min_versions, shared_min=shared_min,
        )
        return {"results": res["rows"], "total": res["total"], "offset": res["offset"], "limit": res["limit"]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/search/export")
def export(
    repo: str = "1", q: str = "", deleted_only: bool = False, largest: bool = False,
    over_gb: float = 0, source: str | None = None, ext: str | None = None,
    sort: str = "size", dir: str = "desc",
    min_gb: float = 0, max_gb: float = 0, date_from: str | None = None, date_to: str | None = None,
    min_versions: int = 0, shared_min: int = 0,
) -> StreamingResponse:
    min_size, max_size = _bounds(over_gb, largest, min_gb, max_gb)
    cur = search_export(repo, query=q, deleted_only=deleted_only, min_size=min_size,
                        source=source or None, ext=ext or None, sort=sort, direction=dir,
                        max_size=max_size, date_from=date_from or None, date_to=date_to or None,
                        min_versions=min_versions, shared_min=shared_min)

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_EXPORT_COLS)
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for row in batch:
                writer.writerow(row)
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        cur.close()

    return StreamingResponse(
        rows(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="kopialens_search_repo{repo}.csv"'},
    )


@router.get("/search/versions")
def file_versions(repo: str = "1", source: str | None = None, full_path: str | None = None) -> dict:
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    return {"versions": versions(repo, source, full_path)}


@router.get("/search/detail")
def detail(repo: str = "1", source: str | None = None, full_path: str | None = None) -> dict:
    """Accurate per-file footprint: logical/physical/freeable + dedup chunks + shared paths."""
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    try:
        return file_detail(repo, source, full_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
