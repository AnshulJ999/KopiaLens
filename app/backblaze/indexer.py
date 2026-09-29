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

from . import bbdb, parsers, paths, pq, queries, rules
from .safety import open_ro
from ..core.paths import prefix_bounds

log = logging.getLogger("kopialens.bz.indexer")  # under the app's logger so in-process calls reach the log files

try:
    from config import HERE as _KL_ROOT, SELF_EXE as _SELF_EXE  # _KL_ROOT: the worker subprocess cwd
except Exception:  # standalone / tests
    _KL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _SELF_EXE = sys.executable

_BATCH = 50_000
_CHURN_TAIL_BYTES = 30 * 1024 * 1024  # read ~last 30 MB of bzfileids for "recent additions"
_ROLLUP_MIN = 20_000  # depth-1 folders with >= this many files also get a depth-2 rollup (the slow ones)

# Lightweight progress/state for the /api/backblaze/status endpoint (no kopia-style start_build).
_STATE: dict = {"building": False, "last_run": 0.0, "last_summary": None, "last_error": None}


# ---- worker progress file () ------------------------------------------------------
def _progress_path() -> str:
    import config
    return os.path.join(config.DATA_DIR, "index_progress_repobz.json")


def _write_progress(stage: str) -> None:
    """Same protocol as the kopia worker's progress file (app/index/worker.py): the tray guard's
    `indexing` list and the orphan reaper glob data/index_progress_repo*.json, and this filename
    matches that glob. Atomic write; a failure never affects the refresh itself."""
    try:
        # Plain on purpose (no paths in it): the keyless tray reads it too.
        payload = {"repo": "bz", "stage": stage, "pid": os.getpid(), "updated": time.time()}
        import config
        os.makedirs(config.DATA_DIR, exist_ok=True)
        tmp = _progress_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, _progress_path())
    except Exception:  # noqa: BLE001 - progress reporting must never affect the real work
        pass


def _clear_progress() -> None:
    try:
        os.remove(_progress_path())
    except Exception:  # noqa: BLE001
        pass
# Atomic overlap guard for refresh_via_worker: a plain _STATE["building"] check-then-set is a race
# between two near-simultaneous triggers (double-clicked refresh POST, or POST + the bg cycle) -
# both could pass the check before either sets it, launching two worker subprocesses that then
# contend for the SQLite write lock. try_acquire on this Lock is atomic.
_WORKER_LOCK = threading.Lock()


_LEFTOVER_CHECK: list = [0.0, False]


def leftover_worker_running() -> bool:
    """A Backblaze worker started by a previous app process is still running (checked every 5 s)."""
    if time.time() - _LEFTOVER_CHECK[0] < 5:
        return _LEFTOVER_CHECK[1]
    alive = False
    try:
        with open(_progress_path(), encoding="utf-8") as fh:
            pid = int((json.load(fh) or {}).get("pid") or 0)
        if pid and pid != os.getpid():
            from ..core.nightly import our_worker
            alive = our_worker(pid) is not None
    except Exception:  # noqa: BLE001 - no progress file, or the process cannot be inspected
        alive = False
    _LEFTOVER_CHECK[:] = [time.time(), alive]
    return alive


def state() -> dict:
    out = dict(_STATE)
    if not out.get("building") and leftover_worker_running():
        out["building"] = True
    return out


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
    _, root_hi = prefix_bounds(root_lc)       # lo + U+FFFF misses emoji names
    con.execute("DELETE FROM folder_rollup WHERE parent_lc >= ? AND parent_lc < ?", [root_lc, root_hi])
    if rows:
        con.executemany("INSERT INTO folder_rollup VALUES(?,?,?,?,?,?,?,?,?)", rows)


def _walk_drive(vol, engine, emit) -> tuple[int, int, int, dict, dict]:
    """Parse + classify one drive's filelist, dedupe by path, emit rows in batches of _BATCH
    (tuple order = pq.SCHEMA column order), and accumulate the shallow folder rollups.
    Returns (total, backed, excluded, roll1, roll2) - the store-specific sink (SQLite insert or
    Parquet writer) is the caller's `emit`, and the caller persists the rollups.

    Dedupes by path: a Backblaze per-volume filelist can list each entry TWICE (the committed
    .dat sometimes contains the whole body duplicated - e.g. E: had 1.19M lines / 595k unique).
    Backblaze's own bzstat count dedupes, so we must too, else backed counts come out ~2x.
    Deduping before classify also halves the rule work."""
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
            emit(batch)
            batch.clear()
    if batch:
        emit(batch)
    return total, backed, excluded, roll1, roll2


def _reindex_drive(con, vol, engine) -> tuple[int, int, int]:
    """Replace one drive's `files` rows in the SQLite store from its filelist. Returns
    (total, backed, excluded) counts."""
    con.execute("DELETE FROM files WHERE drive=?", [vol.letter])

    def emit(batch):
        con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", batch)

    total, backed, excluded, roll1, roll2 = _walk_drive(vol, engine, emit)
    _write_rollup(con, vol.letter.lower() + ":\\", roll1, roll2)
    return total, backed, excluded


def _reindex_drive_pq(vol, engine, staged: str) -> tuple[int, int, int, dict, dict]:
    """Build one drive's rows into a staged Parquet file (); the SQLite `files` table is
    untouched. Returns (total, backed, excluded, roll1, roll2) - the caller writes the rollups
    to the slim SQLite and publishes the staged file into the new generation."""
    writer = pq.open_writer(staged)
    try:
        def emit(batch):
            writer.write_table(pq.table_from_rows(batch))

        total, backed, excluded, roll1, roll2 = _walk_drive(vol, engine, emit)
    finally:
        writer.close()
    return total, backed, excluded, roll1, roll2


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
    agg: dict[tuple, list] = {}
    with queries._files_cursor() as fc:
        cur = fc.execute("SELECT drive, full_path, size FROM files WHERE excluded=0")
        while True:
            batch = cur.fetchmany(200_000)
            if not batch:
                break
            for drive, full_path, size in batch:
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


def detect_changes() -> dict:
    """Cheap freshness detect for the scheduler's light pass (). Stat every filelist; hash the
    body only where the (mtime,size) watermark moved; skip stubs. NEVER touches the files tables -
    the only writes are the ingest watermark update on a digest match (mirroring refresh()'s own
    no-reindex bookkeeping, so consecutive detects are pure stat) and the kv flags the due check
    reads. A real change sets kv "reindex_pending"; refresh() clears it on success.

    Callers must not run this while a refresh is in flight (the worker owns the same SQLite file)."""
    if not paths.exists():
        return {"ok": False, "reason": f"bzdata not found at {paths.bzdata_root()}"}
    t0 = time.time()
    con = bbdb.connect()
    engine = rules.load_engine()
    rules_hash = rules.engine_digest(engine)
    prev_rules_hash = bbdb.kv_get(con, "rules_digest")
    # Same bootstrap rule as refresh(): no digest on record yet is NOT a rules change
    rules_changed = prev_rules_hash is not None and prev_rules_hash != rules_hash
    changed = False
    drives: dict[str, dict] = {}
    for vol in paths.volumes():
        src = f"filelist:{vol.letter}"
        try:
            st = os.stat(vol.filelist)
        except OSError:
            continue
        entry = {"watermark_moved": False, "changed": False}
        drives[vol.letter] = entry
        if parsers.is_stub_filelist(vol.filelist):
            continue  # aborted scan: flag nothing, watermark nothing (mirrors refresh())
        prev = bbdb.ingest_get(con, src)  # (mtime, size, digest) or None
        have_rows = (pq.has_drive(vol.letter) if queries._store() == "parquet" else
                     con.execute("SELECT 1 FROM files WHERE drive=? LIMIT 1", [vol.letter]).fetchone())
        if prev is not None and have_rows and prev[0] == st.st_mtime and prev[1] == st.st_size:
            continue  # untouched since the last check - pure stat
        entry["watermark_moved"] = True
        try:
            digest = _filelist_digest(vol.filelist)
        except OSError as exc:
            # Cannot verify -> flag nothing. A transient read error must not trigger a doomed
            # reindex; the next detect retries the hash and a warning that repeats is the signal.
            log.warning("BZ %s: filelist unreadable during detect (%s) - skipping", vol.letter, exc)
            continue
        if prev is not None and have_rows and digest and digest == prev[2]:
            bbdb.ingest_set(con, src, st.st_mtime, st.st_size, digest)  # header-only rewrite
        else:
            entry["changed"] = True
            changed = True  # watermark left alone: refresh() re-derives it when it reindexes
    if rules_changed:
        changed = True
    if changed:
        bbdb.kv_set(con, "reindex_pending", "1")
    bbdb.kv_set(con, "last_check_ts", str(time.time()))
    con.commit()
    return {"ok": True, "changed": changed, "rules_changed": rules_changed,
            "drives": drives, "elapsed": round(time.time() - t0, 1)}


def freshness() -> dict:
    """The kv freshness triple (): when the data was last verified (detect or refresh), when
    it was last actually rebuilt, and whether a detected change is still waiting. Reads the bz
    SQLite kv table, so it survives restarts; a missing/unbuilt DB reads as never."""
    out = {"last_check_ts": None, "last_reindex_ts": None, "pending": False}
    try:
        con = bbdb.connect()
        for key in ("last_check_ts", "last_reindex_ts"):
            try:
                out[key] = float(bbdb.kv_get(con, key) or 0.0) or None
            except (TypeError, ValueError):
                pass
        out["pending"] = bbdb.kv_get(con, "reindex_pending") == "1"
    except Exception:  # noqa: BLE001 - a freshness read must never break its caller
        pass
    return out


def refresh(force: bool = False) -> dict:
    """Incremental refresh of the whole BZ index. Returns a per-drive summary."""
    if not paths.exists():
        return {"ok": False, "reason": f"bzdata not found at {paths.bzdata_root()}"}
    from ..index import crypto
    if pq.target_state() and not crypto.unlocked():
        # The BZ store is encrypted and this process holds no key: nothing may index in the clear.
        return {"ok": False, "skipped": True,
                "reason": "the Backblaze store is encrypted and locked - unlock KopiaLens"}
    t0 = time.time()
    _STATE["building"] = True
    _write_progress("starting")
    con = bbdb.connect()
    engine = rules.load_engine()
    # Reclassify trigger: the exclusion rules the engine parsed (all four layers), hashed. The
    # filelist is a raw volume scan that includes files the rules exclude, so a rule change does
    # NOT change the filelist body - without this hash, "Index BZ" would skip every drive and the
    # Excluded/coverage views would keep the old classification until Backblaze's next scan cycle.
    # A digest change is why a reindex fires, never which rows get written.
    rules_hash = rules.engine_digest(engine)
    prev_rules_hash = bbdb.kv_get(con, "rules_digest")
    rules_changed = prev_rules_hash is not None and prev_rules_hash != rules_hash
    changed: list[str] = []
    drives: dict[str, dict] = {}
    try:
      with _LOCKED(con):
        parquet = queries._store() == "parquet"
        pq_lock = None
        pq_gen = 0
        pq_moves: list[tuple[str, str]] = []
        pq_rels: dict[str, str] = {}
        reclaim_ran = False
        try:
            if parquet:
                # One WriterLock for the whole write phase (staging + publish). The manifest is
                # read while HOLDING it - mirror of index/publish.replace_artifacts.
                pq_lock = pq.WriterLock()
                pq_lock.__enter__()
                manifest = pq.read_manifest()
                pq_gen = int((manifest or {}).get("generation") or 0) + 1
                pq_rels = dict((manifest or {}).get("drives", {}))
            for vol in paths.volumes():
                src = f"filelist:{vol.letter}"
                try:
                    st = os.stat(vol.filelist)
                except OSError:
                    continue
                prev = bbdb.ingest_get(con, src)  # (mtime, size, digest) or None
                have_rows = (pq.has_drive(vol.letter) if parquet else
                             con.execute("SELECT 1 FROM files WHERE drive=? LIMIT 1", [vol.letter]).fetchone())
                reindex = True
                trigger = "file list"
                digest = None
                # A stub filelist is an aborted scan (header only, the recurring F:-signature).
                # Reindexing would DELETE the drive's good rows and read back nothing. Keep the last
                # good index instead (the scan_stub event records the signature); checked FIRST, so
                # neither the filelist body nor a rules change can force a reindex against a
                # half-written list.
                if parsers.is_stub_filelist(vol.filelist):
                    if have_rows:
                        log.warning("BZ %s: filelist is a stub (aborted scan) - keeping the existing index", vol.letter)
                    reindex = False
                else:
                    body_same = False
                    if not force and prev is not None and have_rows:
                        if prev[0] == st.st_mtime and prev[1] == st.st_size:
                            body_same = True  # quick pre-check: stat unchanged, no hash needed
                        else:
                            # mtime/size changed (Backblaze rewrites the scan-start header on every pass) -
                            # hash the body (skipping that header line) to check whether the CONTENT
                            # actually changed, cheap insurance against a full reindex on every cycle.
                            try:
                                digest = _filelist_digest(vol.filelist)
                            except OSError:
                                digest = None
                            if digest and digest == prev[2]:
                                body_same = True
                                bbdb.ingest_set(con, src, st.st_mtime, st.st_size, digest)
                                log.debug("BZ %s: filelist body unchanged (digest match), skipping reindex", vol.letter)
                    if body_same:
                        reindex = rules_changed  # unchanged body still reclassifies when the rules moved
                        if reindex:
                            trigger = "rules changed"
                # bootstrap: a drive with no Parquet file (freshly switched store) rebuilds
                # regardless of the digest watermarks - the watermarks describe the OTHER store.
                if parquet and vol.letter not in pq_rels and not parsers.is_stub_filelist(vol.filelist):
                    reindex = True
                    trigger = "missing in parquet store"
                if reindex:
                    if parquet:
                        _write_progress(f"reindexing {vol.letter}")
                        staged = pq.staged_path(vol.letter, pq_gen)
                        total, backed, excluded, roll1, roll2 = _reindex_drive_pq(vol, engine, staged)
                        if pq.ensure_staged_state(staged):   # Everything level
                            _write_progress(f"encrypting {vol.letter}")
                        _write_rollup(con, vol.letter.lower() + ":\\", roll1, roll2)
                        rel = pq.pool_rel(vol.letter, pq_gen)
                        pq_moves.append((staged, rel))
                        pq_rels[vol.letter] = rel
                    else:
                        total, backed, excluded = _reindex_drive(con, vol, engine)
                    _write_progress(f"indexing {vol.letter}")
                    if digest is None:
                        try:
                            digest = _filelist_digest(vol.filelist)
                        except OSError:
                            digest = ""
                    bbdb.ingest_set(con, src, st.st_mtime, st.st_size, digest or "")
                    drives[vol.letter] = {"total": total, "backed": backed, "excluded": excluded, "reindexed": True}
                    changed.append(vol.letter)
                    log.info("BZ reindex %s (%s): %d files (%d backed / %d excluded)",
                             vol.letter, trigger, total, backed, excluded)
                else:
                    if parquet:
                        t, b, e = pq.drive_counts(vol.letter)
                        drives[vol.letter] = {"total": t, "backed": b, "excluded": e, "reindexed": False}
                    else:
                        row = con.execute(
                            "SELECT count(*), sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END), "
                            "sum(CASE WHEN excluded=1 THEN 1 ELSE 0 END) FROM files WHERE drive=?",
                            [vol.letter]).fetchone()
                        drives[vol.letter] = {"total": row[0] or 0, "backed": row[1] or 0,
                                              "excluded": row[2] or 0, "reindexed": False}
            if changed or force:
                _write_progress("publishing")
                if parquet:
                    if pq_rels:
                        pq.publish(pq_gen, pq_rels, pq_moves)
                        # the parquet store is now authoritative: hand back the
                        # ~3.4 GB the stale legacy files rows occupy inside the slim SQLite.
                        reclaim_ran = bool(_reclaim_legacy_files(con))
                else:
                    bbdb.rebuild_fts(con)
            if rules_hash != prev_rules_hash:
                bbdb.kv_set(con, "rules_digest", rules_hash)
            _refresh_queue(con)
            _refresh_events(con)
            _refresh_backup_days(con)
            _refresh_stats_kv(con)
            _refresh_folder_snapshots(con, _history_days())
            if force or not bbdb.kv_get(con, "fileids_total"):  # populate once, then only on a forced rebuild
                _refresh_ids_count(con)
            _refresh_cache_aggregates(con)  # kv-cache status/overview/default-churn for instant open
            # handshake with the scheduler's detect: a completed refresh has absorbed whatever
            # the detect flagged, and (when it actually reindexed) becomes the new throttle anchor.
            # Same transaction as the data, so a crash cannot clear pending without the fresh rows.
            if changed or force:
                bbdb.kv_set(con, "last_reindex_ts", str(time.time()))
            bbdb.kv_set(con, "last_check_ts", str(time.time()))
            bbdb.kv_set(con, "reindex_pending", "0")
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
            # a pending legacy VACUUM retries on every refresh until it succeeds
            # (skipped when the reclamation already attempted it in this run).
            if parquet and not reclaim_ran and bbdb.kv_get(con, "legacy_vacuum_pending") == "1":
                _vacuum_legacy(con)
        finally:
            if pq_lock is not None:
                pq_lock.__exit__(None, None, None)
      summary = {"ok": True, "changed": changed, "drives": drives, "elapsed": round(time.time() - t0, 1)}
      _STATE["last_summary"] = summary
      _STATE["last_run"] = time.time()
      return summary
    finally:
        _clear_progress()
        _STATE["building"] = False


def _db_size_bytes():
    """Best-effort on-disk size of the slim SQLite (None for an in-memory test DB)."""
    try:
        return os.path.getsize(bbdb.db_path())
    except Exception:  # noqa: BLE001
        return None


def _vacuum_legacy(con) -> bool:
    """VACUUM the slim SQLite. VACUUM needs exclusive access and the app's WAL readers can hold
    it, so a failure ('database is locked') is EXPECTED under load: flag kv legacy_vacuum_pending
    and return False - the housekeeping zone of every later parquet refresh retries until it
    succeeds. Never raises."""
    try:
        con.execute("VACUUM")
    except Exception as exc:  # noqa: BLE001 - a locked VACUUM must not fail the refresh
        bbdb.kv_set(con, "legacy_vacuum_pending", "1")
        con.commit()
        log.warning("BZ legacy reclamation: VACUUM failed (%s) - flagged for retry on a later refresh", exc)
        return False
    if bbdb.kv_get(con, "legacy_vacuum_pending") == "1":
        bbdb.kv_set(con, "legacy_vacuum_pending", "0")
        con.commit()
    return True


def _reclaim_legacy_files(con) -> bool:
    """ After a successful Parquet publish, the legacy sqlite `files` table (+ its
    FTS5 index) is dead weight - ~3.4 GB of stale rows inside the SAME file that holds the small
    history/state tables (which must never be dropped). Drop the legacy structures, recreate them
    EMPTY (so switching back to the sqlite store rebuilds instead of erroring), and VACUUM to hand
    the space back. Returns True iff a VACUUM was attempted (so the caller does not immediately
    retry); a locked VACUUM retries on later refreshes via kv legacy_vacuum_pending."""
    have = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('files','files_fts')")]
    if "files" not in have:
        return False
    if con.execute("SELECT 1 FROM files LIMIT 1").fetchone() is None:
        return False  # already reclaimed (or never built) - nothing to hand back
    log.info("BZ legacy reclamation: dropping the stale sqlite files table (+FTS) - the parquet store is authoritative")
    con.commit()
    con.execute("DROP TABLE IF EXISTS files_fts")
    con.execute("DROP TABLE IF EXISTS files")
    bbdb._init_schema(con)  # recreate EMPTY files + indexes + FTS shell; the small tables are untouched
    con.commit()
    t0 = time.time()
    before = _db_size_bytes()
    if not _vacuum_legacy(con):
        return True
    after = _db_size_bytes()
    if before is not None and after is not None:
        log.info("BZ legacy reclamation: VACUUM %.1fs, db %.2f GB -> %.2f GB",
                 time.time() - t0, before / 1e9, after / 1e9)
    else:
        log.info("BZ legacy reclamation: VACUUM done in %.1fs", time.time() - t0)
    return True


def refresh_transmit_side() -> dict:
    """ Light in-process refresh of the TRANSMIT-side data only: the upload-queue table
    (bz_todo_*.dat) and the stats-kv (remaining / totalbackup / last_completed / filestats /
    overview). Backblaze rewrites these every ~10 minutes, independent of the scan-side filelists
    the detect+throttle cycle watches - so they ride the 30-min light loop (main._bz_transmit_pass)
    instead of waiting for a reindex. refresh() keeps
    doing both (idempotent); queue + kv live in the slim SQLite in BOTH store modes, so this is
    identical under parquet and the legacy sqlite path. Well under a second."""
    if _STATE["building"]:
        return {"ok": False, "reason": "refresh in flight"}
    if not paths.exists():
        return {"ok": False, "reason": f"bzdata not found at {paths.bzdata_root()}"}
    t0 = time.time()
    try:
        con = bbdb.connect()
        with _LOCKED(con):
            queue_rows = _refresh_queue(con)
            _refresh_stats_kv(con)
            con.commit()
        return {"ok": True, "queue_rows": queue_rows, "elapsed": round(time.time() - t0, 2)}
    except Exception as exc:  # noqa: BLE001 - a failed transmit refresh must never break the light pass
        log.warning("BZ transmit-side refresh failed: %s", exc)
        return {"ok": False, "reason": str(exc)}


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
    if leftover_worker_running():
        _WORKER_LOCK.release()
        return {"ok": False, "reason": "already running"}
    _STATE["building"] = True
    try:
        args = [_SELF_EXE, "-m", "app.backblaze.worker"]
        if force:
            args.append("--force")
        from ..index import crypto
        payload = crypto.worker_payload() if crypto.enabled() else None
        env = {**os.environ}
        if payload is not None:
            env["KOPIALENS_KEY_STDIN"] = "1"
        try:
            res = subprocess.run(
                args, cwd=_KL_ROOT, capture_output=True, text=True, timeout=45 * 60,
                input=payload, env=env,
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
