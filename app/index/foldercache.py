"""Cached all-versions folder reclaim, pre-computed nightly for the folders worth looking at.

``physical.folder_detail`` is honest but not cheap - it unions the chunks of every version of every
path under a prefix. On a small folder that is a second; on a very large one it is minutes. The
panel must not make the user press a button for a number they always want, so:

  * the nightly worker pre-computes the biggest folders at the depths the UI browses, and
  * anything not pre-computed is computed on demand in the background and cached from then on.

Cached results are keyed to the index state, so they expire automatically when new snapshots land
rather than going quietly stale.
"""

from __future__ import annotations

import json
import os
import threading
import time

from config import CACHEDIR

from ..core.applog import get_logger
from .churnview import _index_hash
from .physical import folder_detail
from .queries import group_by_folder

log = get_logger("foldercache")

_SEP = chr(92)
_MAX_ENTRIES = 4000          # plenty for every folder the UI realistically browses
_PREWARM_TOP = 60            # biggest folders per depth, by physical
_PREWARM_DEPTHS = (2, 3, 4)
_PREWARM_BUDGET = 900.0      # seconds; see prewarm() - the pre-compute must not own the night

_LOCK = threading.Lock()
_MEM: dict[str, dict] = {}   # repo -> {key: payload}
_INFLIGHT: dict[tuple, threading.Lock] = {}


def _path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"folder_reclaim_repo{repo}.json")


def _key(prefix: str, source: str | None) -> str:
    return (prefix.rstrip(_SEP).lower() + "|" + (source or "*"))


def _load(repo: str) -> dict:
    with _LOCK:
        cached = _MEM.get(repo)
    if cached is not None:
        return cached
    try:
        with open(_path(repo), encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    with _LOCK:
        _MEM[repo] = data
    return data


def _save(repo: str) -> None:
    with _LOCK:
        data = dict(_MEM.get(repo) or {})
    # keep the newest entries when the store grows past the cap
    if len(data) > _MAX_ENTRIES:
        keep = sorted(data.items(), key=lambda kv: kv[1].get("computed_at", 0), reverse=True)[:_MAX_ENTRIES]
        data = dict(keep)
        with _LOCK:
            _MEM[repo] = data
    try:
        os.makedirs(CACHEDIR, exist_ok=True)
        tmp = _path(repo) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _path(repo))
    except Exception as exc:  # noqa: BLE001 - a cache write must never fail the caller
        log.warning("repo=%s folder cache write failed: %s", repo, exc)


def peek(repo: str, prefix: str, source: str | None = None) -> dict | None:
    """A valid cached result, or None. Never computes - lets the API answer instantly and decide
    for itself whether to kick a background computation."""
    entry = _load(repo).get(_key(prefix, source))
    if not entry:
        return None
    try:
        if entry.get("stamp") != _index_hash(repo):
            return None
    except Exception:  # noqa: BLE001 - if we cannot check freshness, do not serve a maybe-stale number
        return None
    return entry


def compute(repo: str, prefix: str, source: str | None = None) -> dict:
    """Compute (or return an already-valid cached) all-versions reclaim for one folder.
    Single-flight per folder: two panels opened on the same folder do the work once."""
    hit = peek(repo, prefix, source)
    if hit is not None:
        return {**hit, "cached": True}
    ck = (repo, _key(prefix, source))
    with _LOCK:
        lk = _INFLIGHT.setdefault(ck, threading.Lock())
    with lk:
        hit = peek(repo, prefix, source)          # someone may have finished while we waited
        if hit is not None:
            return {**hit, "cached": True}
        start = time.time()
        # Stamp taken BEFORE the work: if the index moves while this runs, the result belongs to
        # the OLD index and must expire, not be filed under the newer stamp as if it were fresh.
        stamp = _index_hash(repo)
        payload = folder_detail(repo, source, prefix)
        payload.update({"prefix": prefix, "source": source, "stamp": stamp,
                        "computed_at": time.time(), "elapsed": round(time.time() - start, 2)})
        data = _load(repo)
        with _LOCK:
            data[_key(prefix, source)] = payload
        _save(repo)
        if payload["elapsed"] > 5:
            log.info("repo=%s folder reclaim %s took %.1fs (%d objects)",
                     repo, prefix, payload["elapsed"], payload.get("objects", 0))
        return {**payload, "cached": False}


def _prewarm_budget() -> float:
    try:
        from ..settings import load_settings
        return max(0.0, float(load_settings().get("kopialens", {}).get("folder_prewarm_budget", _PREWARM_BUDGET)))
    except Exception:  # noqa: BLE001
        return _PREWARM_BUDGET


def prewarm(repo: str, top: int = _PREWARM_TOP, depths=_PREWARM_DEPTHS) -> None:
    """Nightly: compute the biggest folders at each browsing depth so the panel is instant for the
    ones actually worth opening.

    Hard wall-clock budget, because this is the one stage whose cost is unbounded by anything else:
    the biggest folders are also the slowest to compute (an all-versions chunk union over millions
    of rows), so an unbounded "top N per depth" could run for hours and own the whole night.
    Whatever it does not reach stays uncached and is computed on demand when the user opens it -
    a few seconds once, rather than an overnight job that never ends. Biggest-first ordering means
    the budget is always spent on the folders most worth having ready.
    """
    budget = _prewarm_budget()
    seen: set[str] = set()
    done = failed = 0
    start = time.time()
    stopped_early = False
    for depth in depths:
        if stopped_early:
            break
        try:
            rows = group_by_folder(repo, depth=depth, sort="physical", direction="desc", limit=top)["rows"]
        except Exception as exc:  # noqa: BLE001
            log.warning("repo=%s folder prewarm depth=%d listing failed: %s", repo, depth, exc)
            continue
        for row in rows:
            if budget and time.time() - start > budget:
                stopped_early = True
                break
            prefix = row.get("folder")
            if not prefix or prefix.lower() in seen:
                continue
            seen.add(prefix.lower())
            try:
                compute(repo, prefix)
                done += 1
            except Exception as exc:  # noqa: BLE001 - one bad folder must not end the pass
                failed += 1
                log.warning("repo=%s folder prewarm failed for %s: %s", repo, prefix, exc)
    log.info("repo=%s folder reclaim prewarm: %d computed, %d failed, %.0fs%s",
             repo, done, failed, time.time() - start,
             " (stopped at the time budget)" if stopped_early else "")
