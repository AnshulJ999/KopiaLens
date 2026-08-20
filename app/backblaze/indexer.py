"""Build / incrementally refresh the Backblaze SQLite index from the live bzdata (read-only).

Per-drive incremental: a quick (mtime,size) pre-check skips genuinely-untouched drives; when those
differ (Backblaze rewrites the scan-start header on every pass, so they almost always do) a body
hash (skipping that header line) catches drives whose CONTENT didn't actually change. The small
XML/queue/event sources are cheap and refreshed each pass. Designed to be driven by KopiaLens'
existing background worker (kind-gated, via `refresh_via_worker`) and by the standalone bz_verify
harness (calls `refresh()` in-process).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time

from . import bbdb, parsers, paths, queries, rules
from .safety import open_ro

log = logging.getLogger("bz.indexer")

try:
    from config import HERE as _KL_ROOT  # KopiaLens root, needed as the worker subprocess cwd
except Exception:  # standalone / tests
    _KL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BATCH = 50_000
_CHURN_TAIL_BYTES = 30 * 1024 * 1024  # read ~last 30 MB of bzfileids for "recent additions"
_ROLLUP_MIN = 20_000  # depth-1 folders with >= this many files also get a depth-2 rollup (the slow ones)

# Lightweight progress/state for the /api/backblaze/status endpoint (no kopia-style start_build).
_STATE: dict = {"building": False, "last_run": 0.0, "last_summary": None, "last_error": None}
# Atomic overlap guard for refresh_via_worker: a plain _STATE["building"] check-then-set is a race
# between two near-simultaneous triggers (double-clicked refresh POST, or POST + the bg cycle) -
# both could pass the check before either sets it, launching two worker subprocesses that then
# contend for the SQLite write lock. try_acquire on this Lock is atomic.
_WORKER_LOCK = threading.Lock()


def state() -> dict:
    return dict(_STATE)


def _acc_roll(roll: dict, parent_lc: str, child: str, is_dir: bool, is_excl: bool, size: int, rule) -> None:
    """Accumulate one file into a parent->child rollup bucket (for folder_rollup)."""
    key = (parent_lc, child.lower())
    e = roll.get(key)
    if e is None:
        e = roll[key] = [child, 1 if is_dir else 0, 0, 0, 0, 0, None]  # name, is_dir, bf, bb, ef, eb, rule
    elif is_dir:
        e[1] = 1
    if is_excl:
        e[4] += 1; e[5] += size
        if e[6] is None:
            e[6] = rule
    else:
        e[2] += 1; e[3] += size


def _write_rollup(con, root_lc: str, roll1: dict, roll2: dict) -> None:
    """Persist depth-1 child aggregates (always) + depth-2 (only for big depth-1 folders) for a drive."""
    big = set()
    for (plc, clc), e in roll1.items():
        if (e[2] + e[4]) >= _ROLLUP_MIN:  # this depth-1 folder is large -> precompute its children too
            big.add(plc + clc + "\\")
    rows = [(plc, clc, e[0], e[1], e[2], e[3], e[4], e[5], e[6]) for (plc, clc), e in roll1.items()]
    rows += [(plc, clc, e[0], e[1], e[2], e[3], e[4], e[5], e[6]) for (plc, clc), e in roll2.items() if plc in big]
    con.execute("DELETE FROM folder_rollup WHERE parent_lc >= ? AND parent_lc < ?", [root_lc, root_lc + "￿"])
    if rows:
        con.executemany("INSERT INTO folder_rollup VALUES(?,?,?,?,?,?,?,?,?)", rows)


def _reindex_drive(con, vol, engine) -> tuple[int, int, int]:
    """Replace all rows for one drive from its filelist. Returns (total, backed, excluded) counts.

    Dedupes by path: a Backblaze per-volume filelist can list each entry TWICE (the committed
    .dat sometimes contains the whole body duplicated - e.g. E: had 1.19M lines / 595k unique).
    Backblaze's own bzstat count dedupes, so we must too, else backed counts come out ~2x.
    Deduping before classify also halves the rule work. Also accumulates the shallow folder_rollup
    (depth-1/2 child aggregates) so the Tree/Excluded views render the slow shallow levels instantly."""
    con.execute("DELETE FROM files WHERE drive=?", [vol.letter])
    total = backed = excluded = 0
    seen: set[str] = set()
    batch: list[tuple] = []
    roll1: dict = {}  # depth-1 children of the drive root
    roll2: dict = {}  # depth-2 children (filtered to big depth-1 folders in _write_rollup)
    root_lc = vol.letter.lower() + ":\\"
    for mtime_hex, size, full_path in parsers.iter_filelist(vol.filelist):
        plc = full_path.lower()
        if plc in seen:
            continue
        seen.add(plc)
        is_excl, rule = engine.classify(full_path)
        name = full_path.rsplit("\\", 1)[-1]
        base = name.lower()
        dot = base.rfind(".")
        ext = base[dot + 1:] if dot > 0 else ""
        batch.append((vol.letter, full_path, plc, name, ext, size, mtime_hex,
                      1 if is_excl else 0, rule))
        total += 1
        if is_excl:
            excluded += 1
        else:
            backed += 1
        comps = full_path.split("\\")
        if len(comps) >= 2:
            _acc_roll(roll1, root_lc, comps[1], len(comps) > 2, is_excl, size, rule)
            if len(comps) >= 3:
                _acc_roll(roll2, (comps[0] + "\\" + comps[1] + "\\").lower(), comps[2], len(comps) > 3, is_excl, size, rule)
        if len(batch) >= _BATCH:
            con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", batch)
    _write_rollup(con, root_lc, roll1, roll2)
    return total, backed, excluded


def import_eventlog_archive(eventlog_dir: str) -> dict:
    """One-time import of backup-session events from a copied (readable) rotated-eventlog folder into
    the permanent events_archive table. The live rotated .zips are ACL-locked; this folder is a copy.
    Idempotent (replaces the archive each run). Going forward the live .log keeps current sessions
    fresh, so this is only for the ~1 year of back-history."""
    if not os.path.isdir(eventlog_dir):
        return {"ok": False, "reason": f"not found: {eventlog_dir}"}
    con = bbdb.connect()
    evs = [(e["ts"], e["kind"], e["files"], e["bytes"], e["detail"], e["src"])
           for e in parsers.parse_eventlog(eventlog_dir)
           if e["kind"] in ("session_start", "session_end")]
    with _LOCKED(con):
        # Merge + dedupe (by ts+kind) instead of replace, so re-importing a refreshed copy months
        # later ACCUMULATES new history without losing the old or duplicating.
        existing = {(r[0], r[1]) for r in con.execute("SELECT ts, kind FROM events_archive")}
        new = [e for e in evs if (e[0], e[1]) not in existing]
        if new:
            con.executemany("INSERT INTO events_archive VALUES(?,?,?,?,?,?)", new)
        con.commit()
        total = con.execute("SELECT count(*) FROM events_archive").fetchone()[0]
    return {"ok": True, "imported": len(new), "total": total, "dir": eventlog_dir}


def _refresh_queue(con) -> int:
    con.execute("DELETE FROM queue")
    rows = parsers.parse_queue(paths.datacenter_dir())
    data = []
    for sz, p in rows:
        drive = p[0].upper() if len(p) > 1 and p[1] == ":" else ""
        data.append((drive, p, sz))
    if data:
        con.executemany("INSERT INTO queue VALUES(?,?,?)", data)
    return len(data)


def _refresh_backup_days(con) -> int:
    """Sum bz_done_*.dat into per-day backed-up totals - the honest answer to "how much did
    Backblaze actually upload", which the eventlog's END_BACKUP lines never were (see the note in
    parsers.parse_bz_done_day_totals).

    Watermarked by (mtime, size) in `ingest`: a rotated bz_done file never changes again, so after
    the first pass only the newest one is re-read. Returns the number of files re-parsed."""
    dcenter = paths.datacenter_dir()
    parsed = 0
    for fn, mtime, size in parsers.bz_done_files(dcenter):
        key = f"bzdone:{fn}"
        row = con.execute("SELECT mtime, size FROM ingest WHERE source=?", (key,)).fetchone()
        if row and abs((row[0] or 0) - mtime) < 1e-6 and (row[1] or 0) == size:
            continue  # unchanged since last time
        totals = parsers.parse_bz_done_day_totals(os.path.join(dcenter, fn))
        con.execute("DELETE FROM backup_days WHERE src=?", (fn,))
        if totals:
            con.executemany(
                "INSERT OR REPLACE INTO backup_days (day, src, files, bytes) VALUES(?,?,?,?)",
                [(day, fn, v["files"], v["bytes"]) for day, v in totals.items()],
            )
        con.execute("INSERT OR REPLACE INTO ingest (source, mtime, size, extra) VALUES(?,?,?,?)",
                    (key, mtime, size, None))
        parsed += 1
    if parsed:
        log.info("backup_days: re-parsed %d bz_done file(s)", parsed)
    return parsed


def _refresh_events(con) -> int:
    """Rebuild the small events timeline (sessions, upload-days, mass-deletion + stub signals)."""
    con.execute("DELETE FROM events")
    evs: list[tuple] = []
    for e in parsers.parse_eventlog(paths.eventlog_dir()):
        evs.append((e["ts"], e["kind"], "", e["files"], e["bytes"], e["detail"], e["src"]))
    for d in parsers.read_upload_success(paths.report("bzstat_upload_success.xml")):
        evs.append((d["date"], "upload_day", "", d["success"], d["fail"],  # bytes slot repurposed = fail count
                    f"success={d['success']} fail={d['fail']}", "upload_success"))
    # mass-deletion candidates: any RECENT bz_done > 100 MB
    now = time.time()
    for fn, mtime, size in parsers.bz_done_files(paths.datacenter_dir()):
        if size > 100 * 1_000_000:
            age_days = (now - mtime) / 86400
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
            evs.append((ts, "bz_done_big", "", 0, size,
                        f"{size//1_000_000} MB ({age_days:.0f}d ago)" +
                        (" ACTIVE" if age_days < 20 else " historical"), fn))
    # stub filelists (aborted scan signature)
    for vol in paths.volumes():
        if parsers.is_stub_filelist(vol.filelist):
            evs.append((time.strftime("%Y-%m-%d %H:%M:%S"), "scan_stub", vol.letter, 0, 0,
                        f"{vol.letter}: filelist is a stub (aborted scan)", os.path.basename(vol.filelist)))
    if evs:
        con.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?)", evs)
    return len(evs)


def _refresh_stats_kv(con) -> None:
    """Parse the small status XMLs into kv (JSON values) for the Dashboard/Stats/Health tabs."""
    grand, per = parsers.read_totalbackup(paths.report("bzstat_totalbackup.xml"))
    bbdb.kv_set(con, "totalbackup", json.dumps({"grand": grand, "per_guid": per}))
    bbdb.kv_set(con, "filestats", json.dumps(parsers.read_filestats(
        os.path.join(paths.filelists_dir(), "filestats.xml"))))
    bbdb.kv_set(con, "remaining", json.dumps(parsers.read_simple_xml(
        paths.report("bzstat_remainingbackup.xml"),
        {"remaining_files": r'remainingnumfilesforbackup="(\d+)"',
         "remaining_bytes": r'remainingnumbytesforbackup="(\d+)"'})))
    bbdb.kv_set(con, "last_completed", json.dumps(parsers.read_simple_xml(
        paths.report("bzstat_lastbackupcompleted.xml"),
        {"localdatetime": r'localdatetime="(\d+)"', "gmt_millis": r'gmt_millis="(\d+)"'})))
    ver = parsers.read_simple_xml(os.path.join(paths.bzdata_root(), "overviewstatus.xml"),
                                  {"status": r'(.+)'})
    bbdb.kv_set(con, "overview", json.dumps(ver))
    bbdb.kv_set(con, "zbytes_history", json.dumps(
        parsers.read_zbytes_history(paths.report("zbytes_history.xml"))[:120]))


def _refresh_churn(con) -> None:
    """Roll up the most-recent fileids tail by depth-2 folder = 'recently added' signal."""
    fp = paths.fileids_path()
    size = parsers.fileids_total_size(fp)
    if not size:
        return
    offset = max(0, size - _CHURN_TAIL_BYTES)
    counts: dict[str, int] = {}
    for path in parsers.iter_fileids_from(fp, offset):
        k = parsers.folder_key(path, 2)
        counts[k] = counts.get(k, 0) + 1
    con.execute("DELETE FROM churn")
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:200]
    if top:
        con.executemany("INSERT INTO churn VALUES(?,?)", top)


def _refresh_folder_snapshots(con, history_days: int = 90) -> None:
    """Write ONE per-folder (depth-2) backed rollup for today, if not already written today.
    This is the time-series foundation for churn-Delta + growth-over-time compare. Cheap (one
    grouped scan of `files`); pruned to `history_days`."""
    import time
    day = time.strftime("%Y-%m-%d")
    if con.execute("SELECT 1 FROM folder_snapshots WHERE day=? LIMIT 1", [day]).fetchone():
        return  # already captured today's rollup
    rows = con.execute(
        "SELECT drive, full_path, size FROM files WHERE excluded=0").fetchall()
    agg: dict[tuple, list] = {}
    for drive, full_path, size in rows:
        comps = full_path.split("\\")
        folder = "\\".join(comps[:3]) if len(comps) > 2 else full_path
        e = agg.get((drive, folder))
        if e:
            e[0] += 1; e[1] += size
        else:
            agg[(drive, folder)] = [1, size]
    if agg:
        con.executemany(
            "INSERT INTO folder_snapshots(day, drive, folder, files, bytes) VALUES(?,?,?,?,?)",
            [(day, d, f, v[0], v[1]) for (d, f), v in agg.items()])
    # prune history
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - history_days * 86400))
    con.execute("DELETE FROM folder_snapshots WHERE day < ?", [cutoff])


def _refresh_ids_count(con) -> None:
    """Count the cumulative bzfileids.dat lines (the ~4M total vs ~1.15M selected = dead/historical).
    A full 452 MB scan, so only run on a forced/first build and cache in kv."""
    fp = paths.fileids_path()
    if not os.path.exists(fp):
        return
    try:
        from .safety import open_ro
        n = 0
        with open_ro(fp, encoding="utf-8", errors="replace") as fh:
            for _ in fh:
                n += 1
        bbdb.kv_set(con, "fileids_total", json.dumps(n))
    except Exception as exc:  # noqa: BLE001 - never fail a refresh on the optional ids count
        log.warning("fileids count skipped: %s", exc)


def _history_days() -> int:
    try:
        from ..settings import load_settings
        return int(load_settings().get("backblaze", {}).get("history_days", 90))
    except Exception:
        return 90


def _collapse_prefixes() -> list:
    try:
        from ..settings import load_settings
        return load_settings().get("backblaze", {}).get("collapse_prefixes", []) or []
    except Exception:
        return []


def _filelist_digest(path: str) -> str:
    """blake2b over the filelist BODY, skipping the first line (the '# GmtMillisThisListWasStarted...'
    header, which Backblaze rewrites on every scan even when the file's actual entries didn't change).
    1 MB chunks so a ~200 MB filelist doesn't need to fit in memory at once."""
    h = hashlib.blake2b()
    with open_ro(path, mode="rb") as fh:
        fh.readline()  # skip header line
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _refresh_cache_aggregates(con) -> None:
    """Write kv-cached status/overview/default-churn snapshots. Between refreshes the `files` table
    never changes, so these aggregates stay exact - readers (status()/overview()/churn() for the
    default params) serve the cache instead of re-scanning ~4M rows on every BZ tab open."""
    now = time.time()
    stat = queries._status_live()
    stat["cached_at"] = now
    bbdb.kv_set(con, "cache_status", json.dumps(stat))
    ov = queries._overview_live()
    ov["cached_at"] = now
    bbdb.kv_set(con, "cache_overview", json.dumps(ov))
    # Must call the live build path directly, NOT queries.churn() - churn() for these exact params
    # (depth=2, top<=200, default collapse) PREFERS the kv row this function is about to overwrite,
    # so calling it here would just re-read-and-re-write the same stale snapshot forever (the kv
    # value froze at its first-ever computed result - Pass D1 fix).
    ch = queries._churn_build(200, 2, queries._default_collapse_pairs())
    ch["cached_at"] = now
    bbdb.kv_set(con, "cache_churn_default", json.dumps(ch))


def refresh(force: bool = False) -> dict:
    """Incremental refresh of the whole BZ index. Returns a per-drive summary."""
    if not paths.exists():
        return {"ok": False, "reason": f"bzdata not found at {paths.bzdata_root()}"}
    t0 = time.time()
    _STATE["building"] = True
    con = bbdb.connect()
    engine = rules.load_engine()
    changed: list[str] = []
    drives: dict[str, dict] = {}
    try:
      with _LOCKED(con):
        for vol in paths.volumes():
            src = f"filelist:{vol.letter}"
            try:
                st = os.stat(vol.filelist)
            except OSError:
                continue
            prev = bbdb.ingest_get(con, src)  # (mtime, size, digest) or None
            have_rows = con.execute("SELECT 1 FROM files WHERE drive=? LIMIT 1", [vol.letter]).fetchone()
            reindex = True
            digest = None
            if not force and prev is not None and have_rows:
                if prev[0] == st.st_mtime and prev[1] == st.st_size:
                    reindex = False  # quick pre-check: stat unchanged, no hash needed
                else:
                    # mtime/size changed (Backblaze rewrites the scan-start header on every pass) -
                    # hash the body (skipping that header line) to check whether the CONTENT
                    # actually changed, cheap insurance against a full reindex on every cycle.
                    try:
                        digest = _filelist_digest(vol.filelist)
                    except OSError:
                        digest = None
                    if digest and digest == prev[2]:
                        reindex = False
                        bbdb.ingest_set(con, src, st.st_mtime, st.st_size, digest)
                        log.info("BZ %s: filelist body unchanged (digest match), skipping reindex", vol.letter)
            if reindex:
                total, backed, excluded = _reindex_drive(con, vol, engine)
                if digest is None:
                    try:
                        digest = _filelist_digest(vol.filelist)
                    except OSError:
                        digest = ""
                bbdb.ingest_set(con, src, st.st_mtime, st.st_size, digest or "")
                drives[vol.letter] = {"total": total, "backed": backed, "excluded": excluded, "reindexed": True}
                changed.append(vol.letter)
                log.info("BZ reindex %s: %d files (%d backed / %d excluded)", vol.letter, total, backed, excluded)
            else:
                row = con.execute(
                    "SELECT count(*), sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END), "
                    "sum(CASE WHEN excluded=1 THEN 1 ELSE 0 END) FROM files WHERE drive=?",
                    [vol.letter]).fetchone()
                drives[vol.letter] = {"total": row[0] or 0, "backed": row[1] or 0,
                                      "excluded": row[2] or 0, "reindexed": False}
        if changed:
            bbdb.rebuild_fts(con)
        _refresh_queue(con)
        _refresh_events(con)
        _refresh_backup_days(con)
        _refresh_stats_kv(con)
        _refresh_folder_snapshots(con, _history_days())
        if force or not bbdb.kv_get(con, "fileids_total"):  # populate once, then only on a forced rebuild
            _refresh_ids_count(con)
        _refresh_cache_aggregates(con)  # kv-cache status/overview/default-churn for instant open
        con.commit()
        # Release pages freed by the DELETE+INSERT churn so the file stays small (cheap; only does
        # real work when auto_vacuum=INCREMENTAL is active and there are free pages to release).
        try:
            con.execute("PRAGMA incremental_vacuum")
            con.commit()
        except Exception as exc:  # noqa: BLE001 - never fail a refresh on housekeeping
            log.warning("incremental_vacuum skipped: %s", exc)
        # Truncate the WAL back to ~0. A full rebuild + FTS('rebuild') writes GBs into the WAL, and a
        # PASSIVE auto-checkpoint folds frames back into the DB but never SHRINKS the WAL file - so it
        # had grown to ~3 GB and stayed there. TRUNCATE reclaims it. (Best-effort: a concurrent reader
        # makes it a no-op rather than an error.)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # noqa: BLE001 - never fail a refresh on housekeeping
            log.warning("wal_checkpoint(TRUNCATE) skipped: %s", exc)
      summary = {"ok": True, "changed": changed, "drives": drives, "elapsed": round(time.time() - t0, 1)}
      _STATE["last_summary"] = summary
      _STATE["last_run"] = time.time()
      return summary
    finally:
        _STATE["building"] = False


def refresh_via_worker(force: bool = False) -> dict:
    """Run refresh() in a separate subprocess so the app's own request-handling threads never share
    the GIL / disk contention with the ~4 min reindex (reads stay responsive throughout). The worker
    is a plain `python -m app.backblaze.worker` invocation; SQLite WAL lets it write concurrently
    with the app's own read connections. Guards against overlap via _WORKER_LOCK (atomic try-acquire,
    unlike a plain _STATE["building"] check-then-set - two near-simultaneous callers could otherwise
    both pass the check and launch two worker subprocesses contending for the SQLite write lock);
    always releases and clears `building` when the subprocess finishes, success or failure."""
    if not _WORKER_LOCK.acquire(blocking=False):
        return {"ok": False, "reason": "already running"}
    _STATE["building"] = True
    try:
        args = [sys.executable, "-m", "app.backblaze.worker"]
        if force:
            args.append("--force")
        try:
            res = subprocess.run(
                args, cwd=_KL_ROOT, capture_output=True, text=True, timeout=45 * 60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash under pythonw
            )
        except subprocess.TimeoutExpired:
            log.error("BZ worker timed out after 45 min")
            _STATE["last_error"] = "timeout after 45 min"
            return {"ok": False, "reason": "timeout"}
        if res.returncode != 0:
            log.error("BZ worker failed (rc=%d): %s", res.returncode, (res.stderr or "")[-2000:])
            _STATE["last_error"] = f"worker exit {res.returncode}"
            return {"ok": False, "reason": f"worker exit {res.returncode}", "stderr": (res.stderr or "")[-2000:]}
        try:
            summary = json.loads(res.stdout.strip())
        except Exception as exc:  # noqa: BLE001 - malformed worker output should never crash the caller
            log.error("BZ worker produced unparsable output: %s", exc)
            _STATE["last_error"] = f"bad worker output: {exc}"
            return {"ok": False, "reason": f"bad worker output: {exc}"}
        _STATE["last_summary"] = summary
        _STATE["last_run"] = time.time()
        _STATE["last_error"] = None
        return summary
    finally:
        _STATE["building"] = False
        _WORKER_LOCK.release()


class _LOCKED:
    """Hold the bbdb lock for the whole write batch (single-writer)."""

    def __init__(self, con):
        self._con = con

    def __enter__(self):
        bbdb._LOCK.acquire()
        return self._con

    def __exit__(self, *exc):
        bbdb._LOCK.release()
        return False
