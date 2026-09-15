"""Excluded browser endpoints — local-disk coverage gaps vs both kopia repos."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from config import REPOS, repo_kind

from ..core import disklookup
from ..core.kopia import _repo_args
from ..index.excluded import drive_stats, folder_listing
from ..settings import load_settings

router = APIRouter(tags=["excluded"])


@router.get("/excluded/drives")
def drives() -> dict:
    """Local fixed drives, each with REAL total/backed/excluded size + file count (Everything vs
    index), so a drive row shows its excluded total - not its used-disk size. + es.exe liveness."""
    st = disklookup.status()
    return {"drives": drive_stats() if st["available"] else [], "everything_ok": st["available"],
            "disk_lookup": st}


@router.get("/disk-lookup")
def disk_lookup_status(refresh: bool = False) -> dict:
    """Which disk lookup provider answers (Everything, folder scan, or off) and why."""
    return disklookup.status(refresh=refresh)


@router.get("/excluded/include-cmds")
def include_cmds(path: str = "") -> dict:
    """Commands to ADD this path to Kopia (one per kopia repo). KopiaLens stays read-only on
    config; you copy/run. Adding it as a source means the global policy + schedule then apply."""
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    cmds = []
    for repo, cfg in REPOS.items():
        if repo_kind(repo) != "kopia":
            continue  # a non-kopia repo has no args: the bare command would target the DEFAULT repo
        parts = ["kopia"] + _repo_args(repo) + ["snapshot", "create", path]
        cmds.append({"repo": repo, "name": cfg["name"],
                     "command": " ".join(f'"{a}"' if (" " in a or not a) else a for a in parts)})
    return {"path": path, "commands": cmds}


@router.get("/excluded/list")
def list_folder(path: str = "") -> dict:
    """Direct children of `path` with backup status (backed / partial / excluded / new) + the
    'excluded under here' byte total per folder. Empty path -> caller should show the drive list."""
    if not path:
        raise HTTPException(status_code=400, detail="path is required (use /excluded/drives for roots)")
    try:
        tol = float(load_settings().get("kopialens", {}).get("excluded_tolerance", 0.05) or 0.05)
        return folder_listing(path, tolerance=tol)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
