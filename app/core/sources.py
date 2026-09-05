"""Snapshot and source helpers, ported from ``kopiatoolkit.py``.

``sources_index()`` is the hot path - nearly every endpoint depends on it. It is
RAM-cached and served instantly; when the cache goes stale (TTL) it is served stale and
refreshed in a background thread (so navigation never blocks). The listing backend is
chosen by the ``listing_backend`` setting: ``subprocess`` (default - a plain
``snapshot list --json``, zero background activity) or ``server`` (a persistent kopia
server). Subprocess falls through automatically if the server path errors.
"""

from __future__ import annotations

import collections
import json
import threading
import time

from ..settings import load_settings
from .applog import get_logger
from .kopia import kopia
from .server import SERVERS

log = get_logger("sources")

_CACHE: dict[str, dict] = {}
_LOCK = threading.Lock()
_REFRESHING: set[str] = set()
_LAST_ERROR: dict[str, str] = {}
_TTL = 45.0  # serve cache up to this age; past it, serve stale + refresh in background


class ListingFailed(RuntimeError):
    """kopia could not list this repo's snapshots. Deliberately NOT an empty list: everything
    downstream reads "no sources" as "every source was removed" and cleans up accordingly."""


def all_snapshots(repo: str) -> list[dict]:
    """Full-repo snapshot list in CLI (``--json``) shape (includes retentionReason + pins).

    Raises ListingFailed when kopia itself failed. An unreachable repo (drive asleep, storage
    timeout, repo locked) prints nothing and exits non-zero, which is indistinguishable from a
    genuinely empty repository unless the return code is checked - and the index builder deletes
    the rows of every source missing from this list."""
    res = kopia(repo, ["snapshot", "list", "--json"])
    if res.returncode != 0:
        raise ListingFailed(
            f"repo {repo}: kopia snapshot list failed (exit {res.returncode}): "
            f"{(res.stderr or '').strip()[:300] or 'no error output'}"
        )
    out = res.stdout
    return json.loads(out) if out.strip() else []


def last_listing_error(repo: str) -> str:
    """The most recent listing failure for this repo, cleared on the next success."""
    with _LOCK:
        return _LAST_ERROR.get(repo, "")


def _key_of(src: dict) -> str:
    return f"{src.get('userName')}@{src.get('host')}:{src.get('path')}"


def _build_from_server(repo: str, sources: list[dict]) -> dict[str, list[dict]]:
    by_source: dict[str, list[dict]] = {}
    for s in sources:
        key = _key_of(s.get("source", {}))
        snaps = SERVERS.list_snapshots(repo, key)
        snaps.sort(key=lambda item: item.get("endTime", ""))
        by_source[key] = snaps
    return by_source


def _build_from_subprocess(repo: str) -> dict[str, list[dict]]:
    by_source: dict[str, list[dict]] = collections.defaultdict(list)
    for snap in all_snapshots(repo):
        snap["retention"] = snap.get("retentionReason", []) or []  # normalize to the shape endpoints expect
        by_source[_key_of(snap["source"])].append(snap)
    for key in by_source:
        by_source[key].sort(key=lambda item: item.get("endTime", ""))
    return dict(by_source)


def _build_now(repo: str) -> dict:
    """Build the listing, or raise. A failure NEVER caches an empty result and never replaces a
    previously good one - the old entry keeps being served (stale but true) until a real listing
    succeeds."""
    backend = load_settings().get("listing_backend", "subprocess")
    meta: dict[str, dict] = {}
    try:
        try:
            if backend == "server":
                sources = SERVERS.list_sources(repo)
                srcs = _build_from_server(repo, sources)
                meta = {_key_of(s.get("source", {})): s for s in sources}
            else:
                srcs = _build_from_subprocess(repo)
        except ListingFailed:
            raise                      # kopia itself failed - the fallback would only fail again
        except Exception as exc:       # noqa: BLE001 - a server-backend error falls back to subprocess
            log.warning("sources_index repo=%s backend=%s failed (%s); using subprocess", repo, backend, exc)
            srcs = _build_from_subprocess(repo)
            meta = {}
    except Exception as exc:  # noqa: BLE001 - record it, keep the old entry, let the caller decide
        with _LOCK:
            _LAST_ERROR[repo] = str(exc)[:500]
            _REFRESHING.discard(repo)
        log.error("sources_index repo=%s listing FAILED, keeping the previous listing: %s", repo, exc)
        raise

    entry = {"srcs": srcs, "meta": meta, "built": time.time()}
    with _LOCK:
        _CACHE[repo] = entry
        _LAST_ERROR.pop(repo, None)
        _REFRESHING.discard(repo)
    log.info("sources_index repo=%s built (%s): %d sources", repo, backend, len(srcs))
    return entry


def _refresh_in_background(repo: str) -> None:
    """Background refresh: a failure must not kill the thread with the refreshing flag still set,
    or this repo would never refresh again for the life of the process."""
    try:
        _build_now(repo)
    except Exception:  # noqa: BLE001 - already logged by _build_now; the stale entry stands
        pass
    finally:
        with _LOCK:
            _REFRESHING.discard(repo)


def _ensure(repo: str, force: bool = False) -> dict:
    now = time.time()
    with _LOCK:
        entry = _CACHE.get(repo)
        fresh = bool(entry) and (now - entry["built"] < _TTL)
    if entry is None:
        return _build_now(repo)  # nothing to fall back on: a failure here has to surface
    if force:
        try:
            return _build_now(repo)
        except Exception:  # noqa: BLE001 - a forced refresh that fails still has a last good answer
            return entry
    if fresh:
        return entry
    with _LOCK:  # stale: serve stale now, refresh in the background
        if repo not in _REFRESHING:
            _REFRESHING.add(repo)
            threading.Thread(target=_refresh_in_background, args=(repo,), daemon=True).start()
    return entry


def sources_index(repo: str, force: bool = False) -> dict[str, list[dict]]:
    return _ensure(repo, force)["srcs"]


def sources_meta(repo: str, force: bool = False) -> dict[str, dict]:
    """Per-source server metadata (server backend only): nextSnapshotTime/status/lastSnapshot."""
    return _ensure(repo, force)["meta"]


def invalidate(repo: str | None = None) -> None:
    with _LOCK:
        if repo is None:
            _CACHE.clear()
        else:
            _CACHE.pop(repo, None)


def is_laptop(key: str) -> bool:
    return "laptop" in key.lower()


def split_sources(srcs: dict[str, list[dict]], mode: str) -> tuple[list[str], list[str]]:
    keys = sorted(srcs)
    non_laptop = [key for key in keys if not is_laptop(key)]
    if mode == "ignore":
        return non_laptop, non_laptop
    if mode == "hide":
        return keys, non_laptop
    return keys, keys


def logical_stats(snaps: list[dict]) -> dict:
    sizes = [snap.get("stats", {}).get("totalSize", 0) for snap in snaps]
    dates = [snap.get("startTime", "")[:10] for snap in snaps if snap.get("startTime")]
    largest_index = max(range(len(sizes)), key=lambda i: sizes[i]) if sizes else 0
    return {
        "count": len(snaps),
        "first": dates[0] if dates else "?",
        "last": dates[-1] if dates else "?",
        "first_time": snaps[0].get("startTime", "") if snaps else "",
        "last_time": (snaps[-1].get("endTime") or snaps[-1].get("startTime") or "") if snaps else "",
        "latest": sizes[-1] if sizes else 0,
        "largest": sizes[largest_index] if sizes else 0,
        "largest_date": snaps[largest_index].get("startTime", "")[:10] if snaps else "?",
        "avg": sum(sizes) // len(sizes) if sizes else 0,
        "sum": sum(sizes),
    }


def roots_of(snaps: list[dict], recent: int | None = None) -> list[str]:
    use = snaps[-recent:] if recent else snaps
    return [snap["rootEntry"]["obj"] for snap in use if snap.get("rootEntry", {}).get("obj")]


def resolve_source(repo: str, source: str | None = None, index: int | str | None = None) -> tuple[str, dict[str, list[dict]]]:
    srcs = sources_index(repo)
    keys = sorted(srcs)
    if index is not None:
        return keys[int(index)], srcs
    if source:
        if source in srcs:
            return source, srcs
        # Tolerate a trailing-separator discrepancy (a lost/extra backslash in transit) BEFORE
        # falling back to substring - an exact source key is a substring of every deeper source
        # (e.g. "...:C:\" is inside 49 "...:C:\..." keys), which made the fallback misfire.
        def _norm(s: str) -> str:
            return s.rstrip("\\/").lower()
        nsrc = _norm(source)
        norm_hits = [key for key in keys if _norm(key) == nsrc]
        if len(norm_hits) == 1:
            return norm_hits[0], srcs
        hits = [key for key in keys if source.lower() in key.lower()]
        if len(hits) == 1:
            return hits[0], srcs
        raise ValueError(f"'{source}' matched {len(hits)} sources; be exact or use source_index")
    raise ValueError("need source or source_index")


def select_snap(snaps: list[dict], snap: str | None = None) -> dict | None:
    if not snaps:
        return None
    if snap is None or str(snap) in ("last", "latest"):
        return snaps[-1]
    value = str(snap)
    if value in ("first", "baseline"):
        return snaps[0]
    for item in snaps:
        if item["id"] == value:
            return item
    if value.isdigit() and 0 <= int(value) < len(snaps):
        return snaps[int(value)]
    candidates = [item for item in snaps if item.get("startTime", "").startswith(value)]
    return candidates[-1] if candidates else None
