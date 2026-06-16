"""Repo-level stats: content stats, repository status, cache info, maintenance info."""

from __future__ import annotations

import json
import threading

from fastapi import APIRouter, HTTPException

from ..core.kopia import kopia

router = APIRouter(tags=["stats"])

_blob_lock = threading.Lock()  # serialize the slow blob-stats scan so it can never pile up
_blob_cache: dict[str, str] = {}


def _text(repo: str, args: list[str]) -> str:
    try:
        res = kopia(repo, args)
        return (res.stdout or res.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 - surface as text, never 500 the whole panel
        return f"(error running {' '.join(args)}: {exc})"


def _json(repo: str, args: list[str]):
    try:
        res = kopia(repo, args)
        return json.loads(res.stdout) if res.stdout.strip() else None
    except Exception:
        return None


@router.get("/stats")
def stats(repo: str = "1") -> dict:
    """Fast read-only repo-level info for the Stats tab. ``blob stats`` is intentionally EXCLUDED
    here (slow multi-TB scan) - it is loaded on demand via /stats/blob."""
    try:
        return {
            "repo": repo,
            "repository_status": _json(repo, ["repository", "status", "--json"]) or _text(repo, ["repository", "status"]),
            "content_stats": _text(repo, ["content", "stats"]),
            "cache_info": _text(repo, ["cache", "info"]),
            "maintenance": _json(repo, ["maintenance", "info", "--json"]) or _text(repo, ["maintenance", "info"]),
            "blob_stats": _blob_cache.get(repo),  # only present once explicitly loaded
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/stats/blob")
def blob_stats(repo: str = "1") -> dict:
    """On-demand `blob stats` (slow). Serialized + cached so it can never spawn duplicates."""
    if not _blob_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="blob stats already running")
    try:
        _blob_cache[repo] = _text(repo, ["blob", "stats"])
        return {"blob_stats": _blob_cache[repo]}
    finally:
        _blob_lock.release()
