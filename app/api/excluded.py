"""Excluded browser endpoints — local-disk coverage gaps vs both kopia repos."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import REPOS, repo_kind

from ..core import disklookup
from ..core.kopia import _kopia_exe, _repo_args
from ..index.excluded import drive_stats, folder_listing, forget_listings
from ..settings import load_settings
from .errors import PASS_THROUGH

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
    """Which disk lookup provider answers (Everything, folder scan, or off) and why, plus the
    scans queued or running."""
    return disklookup.status(refresh=refresh)


class CountRequest(BaseModel):
    path: str


class StopRequest(BaseModel):
    id: int


@router.post("/disk-lookup/count")
def disk_count(body: CountRequest) -> dict:
    """Count (or recount) a folder, drive or place with the KopiaLens scanner, shown with Stop."""
    if disklookup.provider() != "scan":
        raise HTTPException(status_code=409, detail="The KopiaLens scanner is off.")
    if not body.path.strip():
        raise HTTPException(status_code=400, detail="path is required")
    forget_listings()
    return {"scan": disklookup.count(body.path.strip())}


@router.post("/disk-lookup/stop")
def disk_stop(body: StopRequest) -> dict:
    forget_listings()
    return {"stopped": disklookup.stop(body.id)}


@router.post("/disk-lookup/left")
def disk_left() -> dict:
    """The Excluded tab was left: stop the counts it started (explicit counts keep going)."""
    disklookup.stop_all_left()
    return {"ok": True}


@router.get("/disk-lookup/computers")
def disk_lookup_computers() -> dict:
    """Computers that appear in the backups, for Settings > Disk scanning's "On computer"."""
    from ..core.sources import sources_index
    from config import kopia_repos
    found: set[str] = set()
    for repo in kopia_repos():
        try:
            keys = sources_index(repo)
        except Exception:  # noqa: BLE001 - an offline repository just adds no names
            continue
        for key in keys:
            host = key.split(":", 1)[0].partition("@")[2]
            if host:
                found.add(host.lower())
    return {"computers": sorted(found)}


@router.get("/excluded/include-cmds")
def include_cmds(path: str = "") -> dict:
    """Commands to ADD this path to Kopia (one per kopia repo). KopiaLens stays read-only on
    config; you copy/run. Adding it as a source means the global policy + schedule then apply."""
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    cmds = []
    for repo, cfg in list(REPOS.items()):
        if repo_kind(repo) != "kopia":
            continue  # a non-kopia repo has no args: the bare command would target the DEFAULT repo
        parts = [_kopia_exe()] + _repo_args(repo) + ["snapshot", "create", path]
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
        return {**folder_listing(path, tolerance=tol), "scans": disklookup.scans()}
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
