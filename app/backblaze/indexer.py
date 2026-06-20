"""Build / incrementally refresh the Backblaze SQLite index from the live bzdata (read-only).

Per-drive incremental: a drive's filelist is re-parsed only when its (mtime,size) changed since
the last ingest - Backblaze regenerates filelists wholesale per scan, so per-drive rebuild is the
natural unit and churn stays tiny. The small XML/queue/event sources are cheap and refreshed each
pass. Designed to be driven by KopiaLens' existing background worker (kind-gated) later, and by the
standalone bz_verify harness now.
"""

from __future__ import annotations

import json
import logging
import os
import time

from . import bbdb, parsers, paths, rules

log = logging.getLogger("bz.indexer")

_BATCH = 50_000
_CHURN_TAIL_BYTES = 30 * 1024 * 1024  # read ~last 30 MB of bzfileids for "recent additions"


def _reindex_drive(con, vol, engine) -> tuple[int, int, int]:
    """Replace all rows for one drive from its filelist. Returns (total, backed, excluded) counts.

    Dedupes by path: a Backblaze per-volume filelist can list each entry TWICE (the committed
    .dat sometimes contains the whole body duplicated - e.g. E: had 1.19M lines / 595k unique).
    Backblaze's own bzstat count dedupes, so we must too, else backed counts come out ~2x.
    Deduping before classify also halves the rule work."""
    con.execute("DELETE FROM files WHERE drive=?", [vol.letter])
    total = backed = excluded = 0
    seen: set[str] = set()
    batch: list[tuple] = []
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
        if len(batch) >= _BATCH:
            con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", batch)
    return total, backed, excluded


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


def _refresh_events(con) -> int:
    """Rebuild the small events timeline (sessions, upload-days, mass-deletion + stub signals)."""
    con.execute("DELETE FROM events")
    evs: list[tuple] = []
    for e in parsers.parse_eventlog(paths.eventlog_dir()):
        evs.append((e["ts"], e["kind"], "", e["files"], e["bytes"], e["detail"], e["src"]))
    for d in parsers.read_upload_success(paths.report("bzstat_upload_success.xml")):
        evs.append((d["date"], "upload_day", "", d["success"], 0,
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


def refresh(force: bool = False) -> dict:
    """Incremental refresh of the whole BZ index. Returns a per-drive summary."""
    if not paths.exists():
        return {"ok": False, "reason": f"bzdata not found at {paths.bzdata_root()}"}
    t0 = time.time()
    con = bbdb.connect()
    engine = rules.load_engine()
    changed: list[str] = []
    drives: dict[str, dict] = {}
    with _LOCKED(con):
        for vol in paths.volumes():
            src = f"filelist:{vol.letter}"
            try:
                st = os.stat(vol.filelist)
            except OSError:
                continue
            prev = bbdb.ingest_get(con, src)
            have_rows = con.execute("SELECT 1 FROM files WHERE drive=? LIMIT 1", [vol.letter]).fetchone()
            if force or prev is None or not have_rows or prev[0] != st.st_mtime or prev[1] != st.st_size:
                total, backed, excluded = _reindex_drive(con, vol, engine)
                bbdb.ingest_set(con, src, st.st_mtime, st.st_size)
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
        _refresh_stats_kv(con)
        _refresh_churn(con)
        con.commit()
        # Release pages freed by the DELETE+INSERT churn so the file stays small (cheap; only does
        # real work when auto_vacuum=INCREMENTAL is active and there are free pages to release).
        try:
            con.execute("PRAGMA incremental_vacuum")
            con.commit()
        except Exception as exc:  # noqa: BLE001 - never fail a refresh on housekeeping
            log.warning("incremental_vacuum skipped: %s", exc)
    return {"ok": True, "changed": changed, "drives": drives, "elapsed": round(time.time() - t0, 1)}


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
