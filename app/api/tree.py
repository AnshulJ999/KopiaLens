"""TreeSize-style browser endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..index.queries import tree_children

router = APIRouter(tags=["tree"])


@router.get("/tree")
def tree(repo: str = "1", source: str | None = None, snapshot_id: str | None = None, prefix: str = "") -> dict:
    if not source or not snapshot_id:
        raise HTTPException(status_code=400, detail="source and snapshot_id are required")
    try:
        return {"children": tree_children(repo, source, snapshot_id, prefix)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
