"""Snapshot and source helpers, ported from ``kopiatoolkit.py``.

``sources_index()`` is the hot path - nearly every endpoint depends on it. It is served
from the persistent kopia server (repo kept open = fast + fresh) and RAM-cached: a
lastSnapshot fingerprint detects new snapshots instantly, and a max-age refresh catches
deletions. Falls back to a subprocess ``snapshot list --json`` if the server is down.
"""

from __future__ import annotations

import collections
import hashlib
import json
import threading
import time

from .applog import get_logger
from .kopia import kopia
from .server import SERVERS

log = get_logger("sources")

_CACHE: dict[str, dict] = {}
_LOCK = threading.Lock()
_POLL_INTERVAL = 3.0   # min seconds between cheap /sources freshness polls
_MAX_AGE = 120.0       # force a full rebuild at least this often (catches deletions)


def all_snapshots(repo: str) -> list[dict]:
    """Subprocess fallback: full-repo snapshot list in CLI (``--json``) shape."""
    out = kopia(repo, ["snapshot", "list", "--json"]).stdout
    return json.loads(out) if out.strip() else []


def _key_of(src: dict) -> str:
    return f"{src.get('userName')}@{src.get('host')}:{src.get('path')}"


def _fingerprint(sources: list[dict]) -> str:
    digest = hashlib.md5()
    for s in sorted(sources, key=lambda x: _key_of(x.get("source", {}))):
        last = (s.get("lastSnapshot") or {}).get("id", "")
        digest.update(f"{_key_of(s.get('source', {}))}|{last}".encode())
    return digest.hexdigest()


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
        by_source[_key_of(snap["source"])].append(snap)
    for key in by_source:
        by_source[key].sort(key=lambda item: item.get("endTime", ""))
    return dict(by_source)


def sources_index(repo: str, force: bool = False) -> dict[str, list[dict]]:
    now = time.time()
    with _LOCK:
        entry = _CACHE.get(repo)
        if entry and not force and (now - entry["polled"] < _POLL_INTERVAL) and (now - entry["built"] < _MAX_AGE):
            return entry["srcs"]
        try:
            sources = SERVERS.list_sources(repo)
            fp = _fingerprint(sources)
            if entry and not force and fp == entry["fp"] and (now - entry["built"] < _MAX_AGE):
                entry["polled"] = now
                return entry["srcs"]
            srcs = _build_from_server(repo, sources)
            log.info("sources_index repo=%s rebuilt from server: %d sources", repo, len(srcs))
        except Exception as exc:  # noqa: BLE001 - server unavailable -> subprocess fallback
            log.warning("sources_index repo=%s server failed (%s); using subprocess", repo, exc)
            srcs = _build_from_subprocess(repo)
            fp = ""
        _CACHE[repo] = {"srcs": srcs, "fp": fp, "built": now, "polled": now}
        return srcs


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
