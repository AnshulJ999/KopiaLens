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
import uuid

from ..core import paths as pathrules
from ..core.applog import get_logger
from . import sidecache
from .queries import _folder_name_sql, _rollup_join
from .store import read_cursor

log = get_logger("churnview")

_CACHE: dict[tuple, dict] = {}
_CACHE_MAX = 12
_LOCK = threading.Lock()
_BUILD_LOCKS: dict[tuple, threading.Lock] = {}

WINDOWS = (1, 3, 7, 30, 90, 180, 365)
_MAX_LIMIT = 2000  # rows retained per view in the cache; requests slice this down

# Bumped whenever the qualifying rule changes semantics, so on-disk rollups built by the old
# rule cannot be served as current (the index stamp alone does not change on a code fix).
_CACHE_VERSION = 4

# A file's extension, from its own name column (never by cutting the path).
_EXT = ("CASE WHEN len(string_split(t.name, '.')) > 1 "
        "THEN lower(list_extract(string_split(t.name, '.'), -1)) ELSE '' END")

# The qualifying set: (path, content) pairs that genuinely entered the repo inside the window.
#
# A file row's stamp is NOT its first appearance: the delta walk emits each distinct
# directory-object version stamped with the NEWEST snapshot where it was current, so a quiet
# directory re-stamps its files with every nightly full walk. Reading min(start_time) naively
# once read churn 5.5x high on repo 1; a versions>1 guard fixed the untouched half, but every
# rewrite older than the window still re-qualified nightly (measured 2026-09-08: repo 1's 24h
# view fell from 48,299 file versions to ~9.3k under the rule below).
#
# What the index knows exactly: one path holds one content per snapshot, so a path's content
# groups ordered by max(start_time) are its content history, and each group's max stamp is the
# LAST snapshot holding it. A group therefore first appeared in the first snapshot after the
# previous group's last stamp - nothing can hide between consecutive snapshots. The oldest
# group dates from paths.first_seen (the oldest-first backfill).
#
# A pair qualifies when:
#   - the path is new (first_seen inside the window) - everything on it is new; or
#   - first_seen IS NULL (backfill not yet run) - legacy conservative fallback; or
#   - the group has a predecessor on the same path and NO snapshot exists between that
#     predecessor's last stamp and the cutoff, i.e. this content entered inside the window.
# An old path whose surviving groups have no predecessor is NOT new data (its earlier history
# expired from retention; counting the nightly re-stamp would re-report old files forever).
# Tests: test_churn_sql.py pins each branch against an in-memory DuckDB.
_QUALIFY_CTE = """
WITH pc AS (
  SELECT f.path_id, f.cid_id, min(sn.start_time) AS first_ts, max(sn.start_time) AS last_ts,
         max(f.size) AS size
  FROM filev f
  JOIN snap sn ON sn.snap_id = f.snap_id
  GROUP BY f.path_id, f.cid_id
), hist AS (
  SELECT path_id, cid_id, first_ts, last_ts, size,
         row_number() OVER (PARTITION BY path_id ORDER BY last_ts, cid_id) AS rn,
         lag(last_ts) OVER (PARTITION BY path_id ORDER BY last_ts, cid_id) AS prev_last
  FROM pc
), newc AS (
  SELECT s.source, p.full_path, p.dir_id, p.src_id, p.name, hist.cid_id, COALESCE(hist.size, 0) AS size,
         COALESCE(cs.packed, ip.packed, 0) AS packed
  FROM hist
  LEFT JOIN pathx p ON p.path_id = hist.path_id
  JOIN src s ON s.src_id = p.src_id
  LEFT JOIN csize cs ON cs.cid_id = hist.cid_id
  LEFT JOIN iphys ip ON ip.cid_id = hist.cid_id
  WHERE p.first_seen >= ?
     OR (p.first_seen IS NULL AND hist.first_ts >= ?)
     OR (hist.rn > 1 AND NOT EXISTS (
          SELECT 1 FROM snap s2
          WHERE s2.src_id = p.src_id
            AND s2.start_time > hist.prev_last
            AND s2.start_time < ?
        ))
)
"""


def _qualify_params(cutoff: str) -> list:
    """Bind values for _QUALIFY_CTE, in the order its placeholders appear. No repo param: a
    generation only ever holds its own repo's rows."""
    return [cutoff, cutoff, cutoff]


# The qualifying set is MATERIALIZED once per call and both outputs are derived from it. Embedding
# _QUALIFY_CTE in each statement made the expensive grouping run twice per window (measured 10.4s
# per window against 5.4s for the old, wrong query - most of the remaining gap).
_MATERIALIZE_SQL = f"CREATE OR REPLACE TEMPORARY TABLE {{tmp}} AS {_QUALIFY_CTE} SELECT * FROM newc"


def _base_sql(tmp: str, depth: int, folder: str) -> str:
    return f"""
SELECT t.source, {folder} AS folder, {_EXT} AS ext,
       sum(t.packed) AS bytes, count(*) AS versions, count(DISTINCT t.full_path) AS files,
       sum(t.size) AS logical
FROM {tmp} t
{_rollup_join('t', depth, 'f')}
GROUP BY t.source, folder, ext
"""


def _current(repo: str, depth: int) -> dict[str, dict]:
    """{view: {key: (logical, physical_latest)}} over the latest snapshot, for the size columns
    next to the churn figures. Same folder/ext expressions as the rollup, so the keys line up.
    ~1 s on repo 1."""
    out: dict[str, dict] = {}
    cur = read_cursor(repo)
    try:
        sqls = {
            "source": ("SELECT s.source, sum(t.size), sum(t.physical) FROM pathx t "
                       "JOIN src s ON s.src_id = t.src_id WHERE t.in_latest GROUP BY 1"),
            "folder": (f"SELECT {_folder_name_sql(cur, 'f')}, sum(t.size), sum(t.physical) FROM pathx t "
                       f"{_rollup_join('t', depth, 'f')} WHERE t.in_latest GROUP BY 1"),
            "ext": f"SELECT {_EXT}, sum(t.size), sum(t.physical) FROM pathx t WHERE t.in_latest GROUP BY 1",
        }
        for view, sql in sqls.items():
            out[view] = {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in cur.execute(sql).fetchall()}
    finally:
        cur.close()
    return out


# Deduped total = true repo growth, each content counted once however many paths hold it. Reads the
# SAME qualifying set as the attributed figure, so the two can never disagree (they used to be
# independent queries over independently-derived sets).
def _deduped_sql(tmp: str) -> str:
    return f"SELECT COALESCE(sum(packed), 0) FROM (SELECT DISTINCT cid_id, packed FROM {tmp})"


def _source_path(source_key: str) -> str:
    """A source key's root path, no trailing separator, under the source's own case rule."""
    sep = pathrules.sep(source_key)
    return pathrules.ident(source_key, pathrules.source_root(source_key).rstrip(sep))


def nested_map(source_keys) -> dict[str, str]:
    """``{source: the OTHER source that contains it}`` - a source whose path sits inside another
    source's path is snapshotted twice, so its churn also shows up in the parent's row. Surfacing
    that stops the ranking from reading as double the real growth."""
    paths = {k: _source_path(k) for k in source_keys}
    out: dict[str, str] = {}
    for key, path in paths.items():
        best = None
        sep = pathrules.sep(key)
        for other, opath in paths.items():
            if other == key or opath == path or pathrules.sep(other) != sep:
                continue
            if path.startswith(opath + sep) and (best is None or len(opath) > len(paths[best])):
                best = other
        if best:
            out[key] = best
    return out


def _index_hash(repo: str) -> str:
    """Cheap stamp that changes whenever the index gained snapshots - used to expire the cache."""
    cur = read_cursor(repo)
    try:
        row = cur.execute("SELECT count(*), max(indexed_at) FROM isnaps").fetchone()
    finally:
        cur.close()
    return f"{row[0]}:{row[1]}" if row else "0"


def _disk_name(repo: str, days: int, depth: int) -> str:
    return f"churn_repo{repo}_{days}d_d{depth}"


def _disk_get(repo: str, days: int, depth: int, stamp: str) -> dict | None:
    """The rollup is computed by the nightly WORKER, which then exits - so it has to land on disk
    or the app never sees it. Costs the app nothing to read, and means opening the tab on the
    default window is free rather than a ~5s, ~700MB aggregation."""
    blob = sidecache.read(_disk_name(repo, days, depth))
    if blob is None:
        return None
    return blob if blob.get("v") == _CACHE_VERSION and blob.get("stamp") == stamp else None


def _disk_put(repo: str, days: int, depth: int, payload: dict) -> None:
    sidecache.write(_disk_name(repo, days, depth), payload)


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
    # Unique table name per call: concurrent builds for different windows must never collide on it.
    tmp = f"_churn_newc_{uuid.uuid4().hex[:12]}"
    cur = read_cursor(repo)
    try:
        cur.execute(_MATERIALIZE_SQL.format(tmp=tmp), _qualify_params(cutoff))
        rows = [
            {"source": r[0], "folder": r[1], "ext": r[2],
             "bytes": int(r[3] or 0), "versions": int(r[4] or 0), "files": int(r[5] or 0),
             "logical": int(r[6] or 0)}
            for r in cur.execute(
                _base_sql(tmp, depth, _folder_name_sql(cur, 'f')),
            ).fetchall()
        ]
        deduped = int(cur.execute(_deduped_sql(tmp)).fetchone()[0] or 0)
    finally:
        try:
            cur.execute(f"DROP TABLE IF EXISTS {tmp}")
        except Exception:  # noqa: BLE001 - a temp-table cleanup must never fail the request
            pass
        cur.close()
    return rows, deduped


def _roll(rows: list[dict], key: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        k = r[key]
        e = out.get(k)
        if e is None:
            out[k] = {key: k, "bytes": r["bytes"], "versions": r["versions"], "files": r["files"],
                      "logical": r.get("logical", 0)}
        else:
            e["bytes"] += r["bytes"]
            e["versions"] += r["versions"]
            e["files"] += r["files"]
            e["logical"] += r.get("logical", 0)
    return out


def build_views(rows: list[dict], limit: int, all_sources=None, current=None) -> dict:
    """Roll the (source, folder, ext) base rows up into the three tab views. Pure - unit tested.

    ``all_sources`` must be the repo's COMPLETE source list, not just the ones that churned: a
    source is only correctly flagged as nested if its parent is in the set being compared, and a
    parent with zero churn in this window would otherwise be missing. ``current`` (from
    ``_current``) adds what each row holds now: ``size_now`` and ``physical_latest``."""
    by_source = _roll(rows, "source")
    by_folder = _roll(rows, "folder")
    by_ext = _roll(rows, "ext")
    for view, table in (("source", by_source), ("folder", by_folder), ("ext", by_ext)):
        now = (current or {}).get(view)
        if now is None:
            continue
        for k, row in table.items():
            row["size_now"], row["physical_latest"] = now.get(k, (0, 0))

    # Per source: its single biggest-churning folder ("C: is churning, mostly Chrome"). Ties break
    # lexicographically: DuckDB's GROUP BY emission order varies between runs, and a first-seen
    # tie here flapped the parity fingerprint (top_folder differs while rows and sums stay equal).
    top: dict[str, tuple[str, int]] = {}
    for r in rows:
        cur_top = top.get(r["source"])
        if cur_top is None or r["bytes"] > cur_top[1] or (
                r["bytes"] == cur_top[1] and r["folder"] < cur_top[0]):
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
            current = _current(repo, depth)
        except Exception as exc:  # noqa: BLE001 - the size columns are extra; churn must still load
            log.warning("churn repo=%s: current sizes failed: %s", repo, exc)
            current = None
        try:
            from ..core.sources import sources_index
            all_sources = list(sources_index(repo))
        except Exception:  # noqa: BLE001 - nesting is a hint, never a hard requirement
            all_sources = []
        payload = {
            "repo": repo, "days": days, "depth": depth, "cutoff": cutoff, "stamp": stamp,
            "v": _CACHE_VERSION,
            "computed_at": time.time(), "elapsed": time.time() - start,
            "total_bytes": sum(r["bytes"] for r in rows),
            "total_bytes_deduped": deduped,
            "total_files": sum(r["files"] for r in rows),
            "total_versions": sum(r["versions"] for r in rows),
            "group_count": len(rows),
            # Cache the FULL rollup, never a limited slice: `limit` is not part of the cache key,
            # so storing a truncated view would serve 10 rows to a later caller asking for 400.
            **build_views(rows, _MAX_LIMIT, all_sources=all_sources, current=current),
        }
        _cache_put(ck, payload)
        log.info("churn repo=%s days=%d depth=%d: %d groups, %.1fs",
                 repo, days, depth, len(rows), payload["elapsed"])
        return {**_slice(payload, limit), "cached": False}


def cached_folder_window(repo: str, folder: str, days: int = 30, depth: int = 4) -> dict | None:
    """New data under one folder (its whole subtree) from an already-built rollup. None when the
    rollup is not cached or the folder is deeper than the rollup's ``depth``. Never computes."""
    want, sep = pathrules.folder_key(folder)
    if sep is None or len(want.split(sep)) > depth:   # a stored folder's depth = its part count
        return None
    hit = _cache_get((repo, days, depth), _index_hash(repo))
    if hit is None:
        return None
    fold = sep == pathrules.WIN_SEP
    want = want.lower() if fold else want
    total = {"bytes": 0, "files": 0, "versions": 0, "days": days}
    found = False
    for row in hit.get("by_folder") or []:
        key, ksep = pathrules.folder_key(str(row.get("folder", "")))
        if ksep != sep:
            continue
        key = key.lower() if fold else key
        if key == want or key.startswith(want + sep):
            found = True
            for k in ("bytes", "files", "versions"):
                total[k] += row.get(k) or 0
    return total if found else None


_PANEL_DEPTH = 4          # cached_folder_window's depth: the folder panel's 30-day row


def ui_depth() -> int:
    """The depth the Churn tab rolls up at: the user's ``churn_depth``, the same number the API
    defaults to. Read here so the prewarm follows the setting instead of assuming 4."""
    try:
        from ..settings import load_settings
        return max(1, int((load_settings().get("kopialens", {}) or {}).get("churn_depth", _PANEL_DEPTH)))
    except Exception:  # noqa: BLE001 - an unreadable setting must not skip the prewarm
        return _PANEL_DEPTH


def prewarm(repo: str, depth: int | None = None, windows=WINDOWS) -> None:
    """Nightly: build EVERY standard window at the depth the UI reads, plus the folder panel's row.

 The tab rolls up at ``churn_depth`` while ``cached_folder_window`` reads depth 4, so one
    hardcoded depth left the other recomputing from scratch on its first open - the nightly built 4
    deep against a tab that read 8. Each pass is ~5s and happens in the worker process (which then
    exits) rather than in the long-lived app."""
    if depth is None:
        depth = ui_depth()
    for days in windows:
        try:
            churn_rollup(repo, days=days, depth=depth, refresh=True)
        except Exception as exc:  # noqa: BLE001 - never fail the nightly cycle on a warm pass
            log.error("churn prewarm repo=%s days=%d depth=%d failed: %s", repo, days, depth, exc)
    if depth != _PANEL_DEPTH:
        try:
            churn_rollup(repo, days=30, depth=_PANEL_DEPTH, refresh=True)
        except Exception as exc:  # noqa: BLE001 - the panel row is a bonus, not the pass
            log.error("churn prewarm repo=%s days=30 depth=%d failed: %s", repo, _PANEL_DEPTH, exc)
