"""New snapshot of any folder (KopiaUI's "New Snapshot") and the facts for deleting a source.

/newsnap/check: is the path on this computer, which source it becomes, and the policy kopia will
apply to it. The estimate and the snapshot itself run as jobs (snapshot_estimate, snapshot_create).
/newsnap/source: snapshot and pin counts plus whether the source has its own policy.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

import config

from ..core import policyresolve
from ..core.sources import sources_index
from .policies import _defined, client_options

router = APIRouter(tags=["newsnapshot"])


def _need_kopia(repo: str) -> None:
    if repo not in config.REPOS or config.repo_kind(repo) != "kopia" or config.is_demo(repo):
        raise HTTPException(status_code=404, detail=f"unknown repository: {repo}")


def _policy_summary(repo: str, source: str) -> dict:
    defined = _defined(repo)
    eff, _df = policyresolve.resolve(defined, source)
    chain = policyresolve.hierarchy(defined, source)
    nearest = next((k for k, _p in chain if k in defined), policyresolve.GLOBAL)
    sch = eff.get("scheduling") or {}
    ret = eff.get("retention") or {}
    keep = [(n, ret.get(f"keep{n}")) for n in ("Latest", "Hourly", "Daily", "Weekly", "Monthly", "Annual")]
    return {
        "own": source in defined,
        "from": "global" if nearest == policyresolve.GLOBAL else nearest,
        "interval": sch.get("intervalSeconds") or 0,
        "times": [policyresolve.tod_string(t) for t in (sch.get("timeOfDay") or [])],
        "cron": sch.get("cron") or [],
        "manual": bool(sch.get("manual")),
        "retention": {n.lower(): v for n, v in keep if v},
        "rules": len((eff.get("files") or {}).get("ignore") or []),
    }


@router.get("/newsnap/check")
def check(repo: str, path: str) -> dict:
    """Where a new snapshot of ``path`` would go. Paths are this computer's: KopiaLens can only
    back up what the machine it runs on can read."""
    _need_kopia(repo)
    raw = (path or "").strip().strip('"')
    if not raw:
        raise HTTPException(status_code=400, detail="Enter a folder path.")
    if not os.path.isabs(raw):
        return {"ok": False, "error": "Enter a full path."}
    norm = os.path.abspath(raw)
    client = client_options(repo)
    if not client["host"]:
        return {"ok": False, "error": "Couldn't read this computer's connection to the repository."}
    if not os.path.exists(norm):
        return {"ok": False, "path": norm, "error": "That path doesn't exist on this computer."}
    source = policyresolve.make_key(client["user"], client["host"], norm)
    snaps = sources_index(repo).get(source) or []
    return {"ok": True, "path": norm, "kind": "dir" if os.path.isdir(norm) else "file",
            "source": source, "identity": f"{client['user']}@{client['host']}",
            "snapshots": len(snaps), "policy": _policy_summary(repo, source)}


@router.get("/newsnap/source")
def source_info(repo: str, source: str) -> dict:
    _need_kopia(repo)
    snaps = sources_index(repo).get(source)
    if snaps is None:
        raise HTTPException(status_code=404, detail="That source has no snapshots in this repository.")
    return {"source": source, "snapshots": len(snaps),
            "pinned": sum(1 for s in snaps if s.get("pins")),
            "has_policy": source in _defined(repo)}
