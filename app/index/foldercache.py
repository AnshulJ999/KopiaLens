"""Cached all-versions folder reclaim, pre-computed nightly for the folders worth looking at.

``physical.folder_detail`` is honest but not cheap - it unions the chunks of every version of every
path under a prefix. On a small folder that is a second; on a very large one it is minutes. The
panel must not make the user press a button for a number they always want, so:

  * the nightly worker pre-computes the biggest folders at the depths the UI browses, and
  * anything not pre-computed is computed on demand in the background and cached from then on.

Cached results are stamped per folder with the snapshot-set hashes of the sources covering that
folder, so they expire exactly when the data behind them changed rather than whenever anything
anywhere in the repo moved.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from config import CACHEDIR

from ..core.applog import get_logger
from .physical import folder_detail
from .queries import _ESC, group_by_folder, like_literal
from .store import read_cursor

log = get_logger("foldercache")

_SEP = chr(92)
_MAX_ENTRIES = 4000          # plenty for every folder the UI realistically browses
_PREWARM_TOP = 40            # biggest folders per depth, by physical
# Depths are DERIVED from what the user actually browses (see _prewarm_depths), not hardcoded.
# Hardcoding was wrong twice: depth 2 computed containers nobody would ever delete (a drive-level
# Users folder measured 165s on its own), while depths 5-7 missed the folders the Churn tab and the
# bloat finder actually display. Deriving them keeps prewarm and UI in step by construction.
_PREWARM_DEPTHS = (3, 4, 5)   # fallback only
_PREWARM_BUDGET = 900.0      # seconds; see prewarm() - the pre-compute must not own the night
_PREWARM_MAX_FILES = 400_000  # skip folders too big to be a delete target (and slowest to compute)
_FULL_REFRESH_DAYS = 3.0     # recompute even when unchanged, bounding freeable/shared drift


def _snap_hashes(repo: str) -> dict[str, str]:
    """Per-source hash of the indexed snapshot-id set (isnaps). ~7k rows - one query."""
    cur = read_cursor(repo)
    try:
        rows = cur.execute(
            "SELECT source, snapshot_id FROM isnaps ORDER BY source, snapshot_id").fetchall()
    finally:
        cur.close()
    digests: dict[str, "hashlib._Hash"] = {}
    for source, sid in rows:
        d = digests.setdefault(source, hashlib.md5())
        d.update((sid or "").encode())
    return {k: d.hexdigest() for k, d in digests.items()}


def folder_stamp(repo: str, prefix: str, source: str | None = None) -> str:
    """Freshness stamp for ONE folder: a hash over only the sources holding anything under the
    prefix, each hashed with its snapshot-id set. A folder's numbers change only when one of
    those sources' index content changes - the old repo-wide ``_index_hash`` stamp made every
    cached folder stale whenever any source anywhere changed, so the nightly recomputed all of
    them. Matching mirrors folder_detail: case-insensitive, the exact folder or under it."""
    base = prefix.rstrip(_SEP).lower()
    cur = read_cursor(repo)
    try:
        sql = ("SELECT DISTINCT s.source FROM dir d JOIN src s ON s.src_id = d.src_id "
               "WHERE lower(d.dir_path) = ? OR lower(d.dir_path) LIKE ?" + _ESC)
        params: list = [base, like_literal(base) + _SEP + "%"]
        if source:
            sql += " AND s.source = ?"
            params.append(source)
        covers = [r[0] for r in cur.execute(sql, params).fetchall()]
    finally:
        cur.close()
    if not covers:
        return "0"
    hashes = _snap_hashes(repo)
    digest = hashlib.md5()
    for s in sorted(covers):
        digest.update(s.encode())
        digest.update(hashes.get(s, "").encode())
    return digest.hexdigest()

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
    # Per-writer temp name. A single fixed `.tmp` was shared by every background refresh thread, so
    # two folders refreshing at once interleaved their writes into the same file and whichever
    # finished last renamed the mess into place. Not dangerous - a corrupt cache reads as empty -
    # but it throws away exactly the expensive work the cache exists to avoid.
    tmp = f"{_path(repo)}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(CACHEDIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _path(repo))
    except Exception as exc:  # noqa: BLE001 - a cache write must never fail the caller
        log.warning("repo=%s folder cache write failed: %s", repo, exc)
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001 - best effort; a stray temp file is harmless
            pass


def peek(repo: str, prefix: str, source: str | None = None, allow_stale: bool = False) -> dict | None:
    """A cached result, or None. Never computes.

    ``allow_stale`` returns an entry computed against an older index, tagged ``stale``. The caller
    decides what to do with it - the API serves it immediately and refreshes behind it."""
    entry = _load(repo).get(_key(prefix, source))
    if not entry:
        return None
    try:
        fresh = entry.get("stamp") == folder_stamp(repo, prefix, source)
    except Exception:  # noqa: BLE001 - cannot verify freshness; treat as stale rather than fresh
        fresh = False
    if fresh:
        return {**entry, "stale": False}
    return {**entry, "stale": True} if allow_stale else None


def compute(repo: str, prefix: str, source: str | None = None, allow_stale: bool = True) -> dict:
    """All-versions reclaim for one folder.

    Fresh cache -> instant. Stale cache -> serve it immediately, tagged, and refresh in the
    background: a number from last night is far more useful than a three-minute wait for one a few
    hours newer. Nothing cached at all -> compute now (the caller fetches this asynchronously, so
    the panel shows "computing" rather than blocking).
    """
    hit = peek(repo, prefix, source, allow_stale=allow_stale)
    if hit is not None and not hit.get("stale"):
        return {**hit, "cached": True}
    if hit is not None:
        _kick_refresh(repo, prefix, source)
        return {**hit, "cached": True}
    return _compute_now(repo, prefix, source)


def _kick_refresh(repo: str, prefix: str, source: str | None) -> None:
    """Recompute in the background. Non-blocking single-flight - if one is already running for this
    folder we simply do not start another."""
    ck = (repo, _key(prefix, source))
    with _LOCK:
        lk = _INFLIGHT.setdefault(ck, threading.Lock())
    if lk.locked():
        return
    def _go() -> None:
        try:
            _compute_now(repo, prefix, source)
        except Exception as exc:  # noqa: BLE001 - a background refresh must never surface
            log.warning("repo=%s background folder refresh failed for %s: %s", repo, prefix, exc)
    threading.Thread(target=_go, daemon=True).start()


def _compute_now(repo: str, prefix: str, source: str | None = None) -> dict:
    """The actual computation. Single-flight per folder: two panels on the same folder do it once."""
    ck = (repo, _key(prefix, source))
    with _LOCK:
        lk = _INFLIGHT.setdefault(ck, threading.Lock())
    with lk:
        hit = peek(repo, prefix, source)          # someone may have finished while we waited
        if hit is not None and not hit.get("stale"):
            return {**hit, "cached": True}
        start = time.time()
        # Stamp taken BEFORE the work: if the index moves while this runs, the result belongs to
        # the OLD index and must expire, not be filed under the newer stamp as if it were fresh.
        stamp = folder_stamp(repo, prefix, source)
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
        return {**payload, "cached": False, "stale": False}


def _prewarm_budget() -> float:
    """Wall-clock seconds this stage may spend. **0 means DISABLED, never unlimited.**

    It used to be read as falsy and so removed the limit entirely - the one setting whose accidental
    value turned off the guard against a stage that can otherwise run for hours. Unlimited is not
    reachable by any value; the largest budget is just a large number."""
    try:
        from ..settings import load_settings
        return max(0.0, float(load_settings().get("kopialens", {}).get("folder_prewarm_budget", _PREWARM_BUDGET)))
    except Exception:  # noqa: BLE001
        return _PREWARM_BUDGET


def _prewarm_depths() -> tuple:
    """The depths the UI actually shows folders at - exactly those, nothing extra.

    The Churn tab rolls up at ``churn_depth`` and the bloat finder at ``group_depth``; those are the
    two places a folder gets clicked from. Reading them means the prewarm follows the user instead
    of me guessing, and it stays correct if they change either setting later.

    A third, one-deeper level used to be included and was dropped: the rollup caps at the path's own
    length, so depth N+1 returns substantially the same folders as depth N and spends a third of the
    budget recomputing them."""
    try:
        from ..settings import load_settings
        kl = load_settings().get("kopialens", {})
        churn = int(kl.get("churn_depth", 4))
        group = int(kl.get("group_depth", 3))
    except Exception:  # noqa: BLE001
        return _PREWARM_DEPTHS
    depths = sorted({d for d in (group, churn) if 2 < d <= 20})
    return tuple(depths) or _PREWARM_DEPTHS


def prewarm(repo: str, top: int = _PREWARM_TOP, depths=None) -> None:
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
    if budget <= 0:
        log.info("repo=%s folder reclaim prewarm is disabled (budget 0)", repo)
        return
    depths = depths or _prewarm_depths()
    seen: set[str] = set()
    done = failed = skipped = skipped_unchanged = 0
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
            if time.time() - start > budget:   # budget 0 already returned above
                stopped_early = True
                break
            prefix = row.get("folder")
            if not prefix or prefix.lower() in seen:
                continue
            seen.add(prefix.lower())
            # Enormous folders are both the slowest to compute and the least likely to be a delete
            # target - somebody deleting a folder picks a specific one, not a 2-million-file root.
            if int(row.get("files") or 0) > _PREWARM_MAX_FILES:
                skipped += 1
                continue
            # Skip-when-unchanged: a folder whose covering sources' snapshot sets did not change
            # re-verifies its numbers against the same index - pure waste (
            # unchanged folders may serve slightly stale freeable/shared; opening the
            # panel refreshes in the background, and _FULL_REFRESH_DAYS bounds the drift).
            entry = _load(repo).get(_key(prefix, None))
            if entry:
                try:
                    unchanged = entry.get("stamp") == folder_stamp(repo, prefix)
                except Exception:  # noqa: BLE001 - cannot verify -> treat as changed
                    unchanged = False
                age_days = (time.time() - entry.get("computed_at", 0)) / 86400.0
                if unchanged and age_days < _FULL_REFRESH_DAYS:
                    skipped_unchanged += 1
                    continue
            try:
                # allow_stale=False is LOAD-BEARING, not a tidy-up. A stale entry hands the work
                # to a daemon thread and returns immediately - that would race through the whole
                # list in milliseconds (never touching the budget), leave ~100 threads querying
                # DuckDB while the worker calls close_all(), and cache nothing at all, because
                # daemon threads die when the process exits.
                compute(repo, prefix, allow_stale=False)
                done += 1
            except Exception as exc:  # noqa: BLE001 - one bad folder must not end the pass
                failed += 1
                log.warning("repo=%s folder prewarm failed for %s: %s", repo, prefix, exc)
    log.debug("repo=%s prewarm depths=%s", repo, depths)
    log.info("repo=%s folder reclaim prewarm: %d computed, %d skipped (unchanged), "
             "%d skipped (too large), %d failed, %.0fs%s",
             repo, done, skipped_unchanged, skipped, failed, time.time() - start,
             " (stopped at the time budget)" if stopped_early else "")
