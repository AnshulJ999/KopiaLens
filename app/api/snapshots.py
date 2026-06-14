"""Snapshot browser endpoints."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from ..core.churn import churn_rows
from ..core.kopia import kopia
from ..core.sources import resolve_source

router = APIRouter(tags=["snapshots"])

_RET_RE = re.compile(r"\b(?:latest|hourly|daily|weekly|monthly|annual)-\d+")


def _retention_reasons(repo: str, key: str, all_ids: set[str]) -> dict[str, list[str]]:
    """Per-snapshot retention reasons under the current policy (KopiaUI-style pills)."""
    out: dict[str, list[str]] = {}
    try:
        text = kopia(repo, ["snapshot", "list", key, "--manifest-id"]).stdout
        for line in text.splitlines():
            reasons = _RET_RE.findall(line)
            if not (reasons or "pins:" in line):
                continue
            ids = [t for t in re.findall(r"[0-9a-f]{16,40}", line) if t in all_ids]
            if ids:
                out[ids[0]] = sorted(set(reasons))
    except Exception:
        pass
    return out


@router.get("/snapshots")
def snapshots(repo: str = "1", source: str | None = None, source_index: int | None = None) -> dict:
    try:
        key, srcs = resolve_source(repo, source=source, index=source_index)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    all_ids = {snap["id"] for snap in srcs[key]}
    reasons = _retention_reasons(repo, key, all_ids)
    rows = []
    for snap in srcs[key]:
        stats = snap.get("stats", {})
        rows.append(
            {
                "id": snap["id"],
                "source": key,
                "start_time": snap.get("startTime", ""),
                "end_time": snap.get("endTime", ""),
                "logical": stats.get("totalSize", 0),
                "files": snap.get("rootEntry", {}).get("summ", {}).get("files", 0),
                "dirs": snap.get("rootEntry", {}).get("summ", {}).get("dirs", 0),
                "pins": snap.get("pins"),
                "retention": reasons.get(snap["id"], []),
                "root": snap.get("rootEntry", {}).get("obj"),
            }
        )
    return {"source": key, "snapshots": rows}


@router.get("/snapshots/churn")
def snapshot_churn(repo: str = "1", source: str | None = None, recent: int = 20) -> dict:
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    try:
        return churn_rows(repo, source, recent=recent)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
