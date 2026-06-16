"""TreeSize-style browser endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.sources import sources_index
from ..index.queries import tree_children, tree_children_live, tree_children_paths

router = APIRouter(tags=["tree"])


def _root_obj(repo: str, source: str, snapshot_id: str) -> str | None:
    for snap in sources_index(repo).get(source, []):
        if snap.get("id") == snapshot_id:
            return snap.get("rootEntry", {}).get("obj")
    return None


@router.get("/tree")
def tree(
    repo: str = "1",
    source: str | None = None,
    snapshot_id: str | None = None,
    prefix: str = "",
    mode: str = "full",
) -> dict:
    """mode=full (complete point-in-time tree, default) | changed (only what changed in this
    snapshot) | all (every snapshot, deduped - source-wide, no snapshot needed)."""
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    try:
        if mode == "all":
            return {"children": tree_children_paths(repo, source, prefix), "mode": mode}
        if not snapshot_id:
            raise HTTPException(status_code=400, detail="snapshot_id is required")
        if mode == "changed":
            return {"children": tree_children(repo, source, snapshot_id, prefix), "mode": mode}
        root = _root_obj(repo, source, snapshot_id)
        if not root:
            raise HTTPException(status_code=404, detail="snapshot root not found")
        return {"children": tree_children_live(repo, source, root, prefix), "mode": "full"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
