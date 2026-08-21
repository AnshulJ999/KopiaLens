"""Churn analysis: which sources / folders / file types added NEW backed-up data in a window.

"New data" = a (path, content) pair whose EARLIEST appearance in any snapshot falls inside the
window, priced at its packed (on-disk) size. That captures both genuinely-new files and the more
interesting case - the same path rewritten over and over (browser profiles, DB dumps), where one
file quietly contributes many versions.

One base rollup at (source, folder, ext) granularity feeds all three views, so the numbers can
never disagree between them. Cached per (repo, days, depth) and invalidated when the index moves.

NOTE on double counting: bytes are attributed PER PATH, so content deduplicated across two paths
is charged to both - that is what makes the ranking useful ("who is responsible"). The deduped
repo-wide figure is reported separately as ``total_bytes_deduped``.

Not to be confused with the dead ``app/core/churn.py`` (superseded long ago by snapanalysis).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time

from config import CACHEDIR

from ..core.applog import get_logger
from .schema import read_cursor

log = get_logger("churnview")

_SEP = chr(92)

_CACHE: dict[tuple, dict] = {}
_CACHE_MAX = 12
_LOCK = threading.Lock()
_BUILD_LOCKS: dict[tuple, threading.Lock] = {}

WINDOWS = (7, 30, 90, 180, 365)
_MAX_LIMIT = 2000  # rows retained per view in the cache; requests slice this down

# Last path segment, then its extension - built from string_split only, so the SQL text carries no
# backslash literal (the separator is always bound as a parameter).
_BASENAME = "list_extract(string_split(full_path, ?), -1)"
_EXT = (f"CASE WHEN len(string_split({_BASENAME}, '.')) > 1 "
        f"THEN lower(list_extract(string_split({_BASENAME}, '.'), -1)) ELSE '' END")
_FOLDER = ("array_to_string(list_slice(string_split(full_path, ?), 1, "
           "least(?, len(string_split(full_path, ?)) - 1)), ?)")

# The qualifying set: (path, content) pairs that genuinely entered the repo inside the window.
#
# 🚨 `min(files.start_time)` ALONE IS NOT "first appearance" - it was, and the Churn tab read 5.5x
# high because of it. `files` is a DELTA index: the newest snapshot is walked in full, while older
# snapshots contribute only what changed (the walk skips directories whose object is unchanged). So
# a file untouched for two years has exactly ONE row, stamped with the LATEST snapshot's time. Ask
# it when it first appeared and it answers "today", every day, forever. Measured on repo 1: churn
# claimed 1,349,520 new files in 30 days against 245,775 by `paths.first_seen`; three folders in the
# user's top ten (site-packages, PSP Games, GoogleDriveMirror) were 100% artifact.
#
# Two things are genuinely new, and they need different evidence:
#   * a path that did not exist before the window  -> `paths.first_seen`, which Pass A backfilled
#     oldest-first specifically to be the true first appearance. Authoritative, no inference.
#   * an existing path REWRITTEN inside the window -> its new content is new data.
#
# The discriminator for the second is how many versions the PATH has (`paths.versions`), not the
# (path, content) group's own row count. A rewritten file always has at least two, because changing
# a file changes its containing directory, which forces the indexer to walk and record it. A file
# rewritten in the very latest snapshot has only one row for its NEW content - so counting per group
# would throw away real churn - but its path still carries the older content's version. An untouched
# file has exactly one version, and that is the case being excluded here.
#
# This reasons about how the indexer builds `files` rather than reading a stored fact, so it is
# exact today and could drift if that walk ever changes. The last six tests in `test_churn_sql.py`
# pin the invariant so a future change fails loudly instead of silently going 5x wrong again. The durable
# alternative - stamping first-seen per (path, content) at index time - was costed at 400-800 MB per
# repo and deliberately deferred; see the sub-memory.
# `paths.versions` is the version count the INDEXER already computed for each path
# (count(DISTINCT snapshot_id) in builder._PATHS_SQL). Deriving it here again cost a second full
# GROUP BY over all ~18.7M `files` rows - measured at 42.6s against 5.4s before. Reading it off the
# `paths` join that this query already performs removes that scan outright.
_QUALIFY_CTE = """
WITH pc AS (
  SELECT source, full_path, content_id, min(start_time) AS first_ts
  FROM files WHERE repo = ? GROUP BY source, full_path, content_id
), newc AS (
  SELECT pc.source, pc.full_path, pc.content_id,
         COALESCE(cs.packed, ip.packed, 0) AS packed
  FROM pc
  LEFT JOIN paths p ON p.repo = ? AND p.source = pc.source AND p.full_path = pc.full_path
  LEFT JOIN content_sizes cs ON cs.repo = ? AND cs.cid = pc.content_id
  LEFT JOIN indirect_phys  ip ON ip.repo = ? AND ip.oid = pc.content_id
  WHERE pc.first_ts >= ?
    AND (p.first_seen >= ? OR p.first_seen IS NULL OR COALESCE(p.versions, 2) > 1)
)
"""


def _qualify_params(repo: str, cutoff: str) -> list:
    """Bind values for _QUALIFY_CTE, in the order its placeholders appear."""
    return [repo, repo, repo, repo, cutoff, cutoff]


_BASE_SQL = f"""{_QUALIFY_CTE}
SELECT source, {_FOLDER} AS folder, {_EXT} AS ext,
       sum(packed) AS bytes, count(*) AS versions, count(DISTINCT full_path) AS files
FROM newc
GROUP BY source, folder, ext
"""

# Deduped total = true repo growth, each content counted once however many paths hold it. Reads the
# SAME qualifying set as the attributed figure, so the two can never disagree (they used to be
# independent queries over independently-derived sets).
_DEDUPED_SQL = f"""{_QUALIFY_CTE}
SELECT COALESCE(sum(packed), 0) FROM (SELECT DISTINCT content_id, packed FROM newc)
"""


def _source_path(source_key: str) -> str:
    """Strip the ``user@host:`` prefix off a source key, lowercased, no trailing separator."""
    return source_key.split(":", 1)[-1].rstrip(_SEP).lower()


def nested_map(source_keys) -> dict[str, str]:
    """``{source: the OTHER source that contains it}`` - a source whose path sits inside another
    source's path is snapshotted twice, so its churn also shows up in the parent's row. Surfacing
    that stops the ranking from reading as double the real growth."""
    paths = {k: _source_path(k) for k in source_keys}
    out: dict[str, str] = {}
    for key, path in paths.items():
        best = None
        for other, opath in paths.items():
            if other == key or not opath or opath == path:
                continue
            if path.startswith(opath + _SEP) and (best is None or len(opath) > len(paths[best])):
                best = other
        if best:
            out[key] = best
    return out


def _index_hash(repo: str) -> str:
    """Cheap stamp that changes whenever the index gained snapshots - used to expire the cache."""
    cur = read_cursor(repo)
    try:
        row = cur.execute(
            "SELECT count(*), max(indexed_at) FROM indexed_snapshots WHERE repo=?", [repo]
        ).fetchone()
    finally:
        cur.close()
    return f"{row[0]}:{row[1]}" if row else "0"


def _disk_path(repo: str, days: int, depth: int) -> str:
    return os.path.join(CACHEDIR, f"churn_repo{repo}_{days}d_d{depth}.json")


def _disk_get(repo: str, days: int, depth: int, stamp: str) -> dict | None:
    """The rollup is computed by the nightly WORKER, which then exits - so it has to land on disk
    or the app never sees it. Costs the app nothing to read, and means opening the tab on the
    default window is free rather than a ~5s, ~700MB aggregation."""
    try:
        with open(_disk_path(repo, days, depth), encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:
        return None
    return blob if blob.get("stamp") == stamp else None


def _disk_put(repo: str, days: int, depth: int, payload: dict) -> None:
    path = _disk_path(repo, days, depth)
    try:
        os.makedirs(CACHEDIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - a cache write must never fail the request
        log.warning("churn cache write failed: %s", exc)


def _cache_get(ck: tuple, stamp: str) -> dict | None:
    with _LOCK:
        hit = _CACHE.get(ck)
    if hit is not None and hit.get("stamp") == stamp:
        return hit
    disk = _disk_get(ck[0], ck[1], ck[2], stamp)
    if disk is not None:
        with _LOCK:
            _CACHE[ck] = disk
    return disk


def _cache_put(ck: tuple, payload: dict) -> None:
    with _LOCK:
        _CACHE[ck] = payload
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
    _disk_put(ck[0], ck[1], ck[2], payload)


def _slice(payload: dict, limit: int) -> dict:
    """Return the payload with each view cut to `limit`. Kept separate from the cache so one
    request's row count can never constrain another's."""
    out = dict(payload)
    for key in ("by_source", "by_folder", "by_ext"):
        rows = payload.get(key) or []
        out[key] = rows[:limit]
    return out


def _build_lock(ck: tuple) -> threading.Lock:
    with _LOCK:
        return _BUILD_LOCKS.setdefault(ck, threading.Lock())


def _compute(repo: str, depth: int, cutoff: str) -> tuple[list[dict], int]:
    cur = read_cursor(repo)
    try:
        rows = [
            {"source": r[0], "folder": r[1], "ext": r[2],
             "bytes": int(r[3] or 0), "versions": int(r[4] or 0), "files": int(r[5] or 0)}
            for r in cur.execute(
                _BASE_SQL,
                _qualify_params(repo, cutoff) + [_SEP, int(depth), _SEP, _SEP, _SEP, _SEP],
            ).fetchall()
        ]
        deduped = int(cur.execute(
            _DEDUPED_SQL, _qualify_params(repo, cutoff)).fetchone()[0] or 0)
    finally:
        cur.close()
    return rows, deduped


def _roll(rows: list[dict], key: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        k = r[key]
        e = out.get(k)
        if e is None:
            out[k] = {key: k, "bytes": r["bytes"], "versions": r["versions"], "files": r["files"]}
        else:
            e["bytes"] += r["bytes"]
            e["versions"] += r["versions"]
            e["files"] += r["files"]
    return out


def build_views(rows: list[dict], limit: int, all_sources=None) -> dict:
    """Roll the (source, folder, ext) base rows up into the three tab views. Pure - unit tested.

    ``all_sources`` must be the repo's COMPLETE source list, not just the ones that churned: a
    source is only correctly flagged as nested if its parent is in the set being compared, and a
    parent with zero churn in this window would otherwise be missing."""
    by_source = _roll(rows, "source")
    by_folder = _roll(rows, "folder")
    by_ext = _roll(rows, "ext")

    # Per source: its single biggest-churning folder ("C: is churning, mostly Chrome").
    top: dict[str, tuple[str, int]] = {}
    for r in rows:
        cur_top = top.get(r["source"])
        if cur_top is None or r["bytes"] > cur_top[1]:
            top[r["source"]] = (r["folder"], r["bytes"])
    nested = nested_map(set(all_sources or ()) | set(by_source))
    for key, row in by_source.items():
        folder, fbytes = top.get(key, ("", 0))
        row["top_folder"] = folder
        row["top_folder_bytes"] = fbytes
        row["nested_in"] = nested.get(key)

    # Per folder: which sources back it up (a folder inside two sources is snapshotted twice).
    fsrc: dict[str, set] = {}
    for r in rows:
        fsrc.setdefault(r["folder"], set()).add(r["source"])
    for key, row in by_folder.items():
        srcs = sorted(fsrc.get(key, ()))
        row["sources"] = srcs
        row["source_count"] = len(srcs)

    def top_rows(d: dict) -> list[dict]:
        return sorted(d.values(), key=lambda x: -x["bytes"])[:limit]

    return {"by_source": top_rows(by_source), "by_folder": top_rows(by_folder),
            "by_ext": top_rows(by_ext)}


def churn_rollup(repo: str, days: int = 30, depth: int = 4, limit: int = 400,
                 refresh: bool = False) -> dict:
    """The churn tab's single data source. Heavy (a grouped scan of the whole ``files`` table),
    so it is cached per (repo, days, depth) until the index changes."""
    days = max(1, int(days))
    depth = max(1, min(20, int(depth)))
    limit = max(1, min(2000, int(limit)))
    stamp = _index_hash(repo)
    ck = (repo, days, depth)
    if not refresh:
        hit = _cache_get(ck, stamp)
        if hit is not None:
            return {**_slice(hit, limit), "cached": True}
    with _build_lock(ck):
        if not refresh:  # another caller may have built it while we waited on the lock
            hit = _cache_get(ck, stamp)
            if hit is not None:
                return {**_slice(hit, limit), "cached": True}
        start = time.time()
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows, deduped = _compute(repo, depth, cutoff)
        try:
            from ..core.sources import sources_index
            all_sources = list(sources_index(repo))
        except Exception:  # noqa: BLE001 - nesting is a hint, never a hard requirement
            all_sources = []
        payload = {
            "repo": repo, "days": days, "depth": depth, "cutoff": cutoff, "stamp": stamp,
            "computed_at": time.time(), "elapsed": time.time() - start,
            "total_bytes": sum(r["bytes"] for r in rows),
            "total_bytes_deduped": deduped,
            "total_files": sum(r["files"] for r in rows),
            "total_versions": sum(r["versions"] for r in rows),
            "group_count": len(rows),
            # Cache the FULL rollup, never a limited slice: `limit` is not part of the cache key,
            # so storing a truncated view would serve 10 rows to a later caller asking for 400.
            **build_views(rows, _MAX_LIMIT, all_sources=all_sources),
        }
        _cache_put(ck, payload)
        log.info("churn repo=%s days=%d depth=%d: %d groups, %.1fs",
                 repo, days, depth, len(rows), payload["elapsed"])
        return {**_slice(payload, limit), "cached": False}


def prewarm(repo: str, depth: int = 4, windows=WINDOWS) -> None:
    """Nightly: build EVERY standard window and persist it, so switching windows in the tab the
    next day costs nothing. Each is ~5s, and doing it here means the ~700MB aggregation happens in
    the worker process (which then exits) rather than in the long-lived app."""
    for days in windows:
        try:
            churn_rollup(repo, days=days, depth=depth, refresh=True)
        except Exception as exc:  # noqa: BLE001 - never fail the nightly cycle on a warm pass
            log.error("churn prewarm repo=%s days=%d failed: %s", repo, days, exc)
