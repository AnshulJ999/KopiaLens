"""Repo-level stats: content stats, repository status, cache info, maintenance info."""

from __future__ import annotations

import json
import threading
import time

from fastapi import APIRouter, HTTPException

from ..core.applog import get_logger
from ..core.kopia import kopia

router = APIRouter(tags=["stats"])
log = get_logger("stats")

_blob_lock = threading.Lock()  # serialize the slow blob-stats scan so it can never pile up
_blob_cache: dict[str, str] = {}

# Repo stats change rarely but cost 4 kopia subprocess calls (~4.5s). Cache + serve-stale:
# return the cached blocks instantly and refresh in the background (also driven by the bg warm
# pass), so opening the Stats tab is never slow.
_STATS_TTL = 300.0
_stats_cache: dict[str, dict] = {}      # repo -> data
_stats_ts: dict[str, float] = {}        # repo -> last computed
_stats_refreshing: set[str] = set()
_stats_lock = threading.Lock()


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


def _compute_stats(repo: str) -> dict:
    data = {
        "repo": repo,
        "repository_status": _json(repo, ["repository", "status", "--json"]) or _text(repo, ["repository", "status"]),
        "content_stats": _text(repo, ["content", "stats"]),
        "cache_info": _text(repo, ["cache", "info"]),
        "maintenance": _json(repo, ["maintenance", "info", "--json"]) or _text(repo, ["maintenance", "info"]),
    }
    with _stats_lock:
        _stats_cache[repo] = data
        _stats_ts[repo] = time.time()
        _stats_refreshing.discard(repo)
    return data


def refresh_stats(repo: str) -> None:
    """Recompute + cache stats (called by the background warm pass). Single-flight."""
    with _stats_lock:
        if repo in _stats_refreshing:
            return
        _stats_refreshing.add(repo)
    try:
        _compute_stats(repo)
    except Exception as exc:  # noqa: BLE001
        log.warning("stats refresh repo=%s failed: %s", repo, exc)
        with _stats_lock:
            _stats_refreshing.discard(repo)


@router.get("/stats")
def stats(repo: str = "1") -> dict:
    """Fast read-only repo-level info for the Stats tab. Served from cache (serve-stale); a stale
    cache is refreshed in the background. ``blob stats`` is on-demand via /stats/blob."""
    with _stats_lock:
        cached = _stats_cache.get(repo)
        age = time.time() - _stats_ts.get(repo, 0)
    if cached is None:
        cached = _compute_stats(repo)  # first ever: must compute synchronously
    elif age > _STATS_TTL:             # stale: serve now, refresh in background
        threading.Thread(target=refresh_stats, args=(repo,), daemon=True).start()
    out = dict(cached)
    out["blob_stats"] = _blob_cache.get(repo)
    return out


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
