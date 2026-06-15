"""Everything-style search endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..index.queries import search_paths, versions

router = APIRouter(tags=["search"])


@router.get("/search")
def search(
    repo: str = "1",
    q: str = "",
    deleted_only: bool = False,
    largest: bool = False,
    over_gb: float = 0,
    source: str | None = None,
    ext: str | None = None,
    limit: int = 400,
    confirm_exists: bool = False,
) -> dict:
    min_size = int(over_gb * 1_000_000_000) if over_gb else 0
    if largest and not min_size:
        min_size = 1
    try:
        return {"results": search_paths(
            repo, query=q, deleted_only=deleted_only, min_size=min_size,
            source=source or None, ext=ext or None, limit=limit, confirm_exists=confirm_exists,
        )}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/search/versions")
def file_versions(repo: str = "1", source: str | None = None, full_path: str | None = None) -> dict:
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    return {"versions": versions(repo, source, full_path)}
