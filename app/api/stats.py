"""Repo-level stats: content stats, repository status, cache info, maintenance info."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from ..core.kopia import kopia

router = APIRouter(tags=["stats"])


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
    """All read-only repo-level info for the Stats tab (each block degrades to text on error)."""
    try:
        return {
            "repo": repo,
            "repository_status": _json(repo, ["repository", "status", "--json"]) or _text(repo, ["repository", "status"]),
            "content_stats": _text(repo, ["content", "stats"]),
            "cache_info": _text(repo, ["cache", "info"]),
            "maintenance": _json(repo, ["maintenance", "info", "--json"]) or _text(repo, ["maintenance", "info"]),
            "blob_stats": _text(repo, ["blob", "stats"]),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
