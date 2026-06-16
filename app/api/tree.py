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


def _dedupe_children(children: list[dict]) -> list[dict]:
    """Collapse file children sharing the same content (content_id) into one - content-aware view.
    Folders and content-less rows pass through unchanged."""
    out, seen = [], set()
    for c in children:
        cid = c.get("content_id")
        if c.get("type") == "file" and cid:
            if cid in seen:
                continue
            seen.add(cid)
        out.append(c)
    return out


@router.get("/tree")
def tree(
    repo: str = "1",
    source: str | None = None,
    snapshot_id: str | None = None,
    prefix: str = "",
    mode: str = "full",
    dedupe: bool = False,
) -> dict:
    """mode=full (complete point-in-time tree, default) | changed (only what changed) |
    reclaimable (files freed if this snapshot is deleted) | all (every snapshot, source-wide).
    ``dedupe`` collapses same-content files into one row (content-aware)."""
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    try:
        if mode == "all":
            children = tree_children_paths(repo, source, prefix)
        elif mode == "reclaimable":
            if not snapshot_id:
                raise HTTPException(status_code=400, detail="snapshot_id is required")
            from ..index.snapanalysis import reclaimable_files  # accurate exclusive-content list (flat)
            rfs = reclaimable_files(repo, source, snapshot_id, limit=500)
            children = [{"path": f["full_path"], "name": f["full_path"].split("\\")[-1].split("/")[-1],
                         "size": f["size"], "physical": f["size"], "files": 1, "modified": "", "type": "file"}
                        for f in rfs]
        else:
            if not snapshot_id:
                raise HTTPException(status_code=400, detail="snapshot_id is required")
            if mode == "changed":
                children = tree_children(repo, source, snapshot_id, prefix)
            else:
                root = _root_obj(repo, source, snapshot_id)
                if not root:
                    raise HTTPException(status_code=404, detail="snapshot root not found")
                children = tree_children_live(repo, source, root, prefix)
        if dedupe:
            children = _dedupe_children(children)
        return {"children": children, "mode": mode}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
