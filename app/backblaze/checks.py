"""Backblaze problems read from Backblaze's own files, for Health and diagnostics.

Every check reads evidence Backblaze writes itself and never guesses from KopiaLens's own counts.
A notice is {"id", "level", "text"}: "warning" only for a confirmed problem, "info" for a sign worth
knowing about. Each check stays quiet when its evidence is absent.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import time

from . import bbdb, paths
from .safety import open_ro

RETRANSMIT_REPORT = "bzlist_bzdone_retransmit.txt"
SELFHEAL_MARK = "SelfHealBzDoneFiles - "
SELFHEAL_OK = "healthy checkup"
# The self-heal replaced a local upload record with the server's older copy.
REPAIR_MARK = b"_DownloadOne_bzdone"
REPAIR_DONE = b"placed bz_done file"
VERIFY_FAILED = b"<verify_failed "
SHRINK_MIN_BYTES = 1_000_000_000    # Backblaze trims a few lines in routine cleanups; a rollback loses far more
RECENT_DAYS = 60                    # older evidence leaves Health
KEEP = 20

# `20261117011215 bz_done_20261111_1.bzff wrong_size_bz_comb_88103479` (time in UTC)
_REPORT_LINE = re.compile(r"^(\d{14})\s+(\S+)\s+(wrong_size\S*)")


def _cutoff() -> float:
    return time.time() - RECENT_DAYS * 86400


# ---- A: Backblaze's own retransmit report --------------------------------------------------
def retransmit_events() -> list[dict]:
    """`bzreports/bzlist_bzdone_retransmit.txt`: one `wrong_size` line each time Backblaze replaced
    its upload record with an older copy from its server and queued those files again. Recent lines
    only; a line in another shape counts with the file's own date."""
    path = paths.report(RETRANSMIT_REPORT)
    out = []
    try:
        updated = os.path.getmtime(path)
        with open_ro(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "wrong_size" not in line:
                    continue
                m = _REPORT_LINE.match(line.strip())
                at = updated
                if m:
                    try:
                        at = calendar.timegm(time.strptime(m.group(1), "%Y%m%d%H%M%S"))
                    except ValueError:
                        pass
                out.append({"at": at, "file": m.group(2) if m else "", "reason": m.group(3) if m else ""})
    except OSError:
        return []
    return [e for e in out if e["at"] >= _cutoff()]


# ---- B: an upload record (bz_done) got smaller ---------------------------------------------
def note_shrink(con, fn: str, old_size: int, new_size: int, lost_bytes: int, lost_files: int) -> None:
    """Called by the indexer when a bz_done file it read before is now smaller."""
    rows = _kv_list(con, "bz_done_shrinks")
    rows.append({"file": fn, "at": time.time(), "old_size": old_size, "new_size": new_size,
                 "lost_bytes": lost_bytes, "lost_files": lost_files})
    bbdb.kv_set(con, "bz_done_shrinks", json.dumps(rows[-KEEP:]))


def recent_shrinks(con) -> list[dict]:
    return [r for r in _kv_list(con, "bz_done_shrinks")
            if r.get("at", 0) >= _cutoff() and r.get("lost_bytes", 0) >= SHRINK_MIN_BYTES]


# ---- C: self-heal results and repairs in the transmit log ----------------------------------
def refresh_selfheal(con) -> int:
    """Keep the self-heal result lines, the record repairs and the failed checks from Backblaze's
    transmit logs (newest KEEP each). Each log is read from where the last pass stopped; a log that
    got shorter was started again and is read whole."""
    folder = paths.transmit_log_dir()
    try:
        names = sorted(n for n in os.listdir(folder) if n.startswith("bztransmit") and n.endswith(".log"))
    except OSError:
        return 0
    found = {"selfheal_results": [], "bzdone_repairs": [], "verify_failed": []}
    for name in names:
        path = os.path.join(folder, name)
        key = f"bztransmit:{name}"
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        row = con.execute("SELECT size FROM ingest WHERE source=?", (key,)).fetchone()
        start = row[0] if row and row[0] and row[0] <= size else 0
        if start == size:
            continue
        try:
            with open_ro(path, mode="rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            continue
        for raw in data.split(b"\n"):
            if SELFHEAL_MARK.encode() in raw:
                line = raw.decode("utf-8", "replace").strip()
                found["selfheal_results"].append({"ts": line[:19], "result": line.split(SELFHEAL_MARK, 1)[1][:300]})
            elif REPAIR_MARK in raw and REPAIR_DONE in raw:
                line = raw.decode("utf-8", "replace").strip()
                found["bzdone_repairs"].append({"ts": line[:19]})
            elif VERIFY_FAILED in raw:
                line = raw.decode("utf-8", "replace").strip()
                found["verify_failed"].append({"ts": line[:19], "result": line[line.find("<verify_failed"):][:300]})
        con.execute("INSERT OR REPLACE INTO ingest (source, mtime, size, extra) VALUES(?,?,?,?)",
                    (key, os.path.getmtime(path), size, None))
    count = 0
    for kv, rows in found.items():
        if rows:
            merged = {(r["ts"], r.get("result", "")): r for r in _kv_list(con, kv) + rows}
            bbdb.kv_set(con, kv, json.dumps(sorted(merged.values(), key=lambda r: r["ts"])[-KEEP:]))
            count += len(rows)
    return count


def selfheal_results(con) -> list[dict]:
    return _kv_list(con, "selfheal_results")


def recent_repairs(con) -> list[dict]:
    day = time.strftime("%Y-%m-%d", time.localtime(_cutoff()))
    return [r for r in _kv_list(con, "bzdone_repairs") if r.get("ts", "")[:10] >= day]


# ---- D: files the server asked Backblaze to upload again ------------------------------------
def recent_purges(con) -> dict | None:
    """Sum of `purged` over upload records Backblaze changed recently (the indexer stores each
    file's count in `ingest.extra`). The record's change time is when the purge was written."""
    files, last = 0, 0.0
    for mtime, extra in con.execute("SELECT mtime, extra FROM ingest WHERE source LIKE 'bzdone:%'"):
        try:
            n = int((json.loads(extra) if extra else {}).get("purged") or 0)
        except (ValueError, AttributeError, TypeError):
            continue
        if n and (mtime or 0) >= _cutoff():
            files += n
            last = max(last, mtime)
    return {"files": files, "at": last} if files else None


# ---- what Health shows ---------------------------------------------------------------------
def notices(con) -> list[dict]:
    out = []
    shrinks = recent_shrinks(con)
    events = retransmit_events()
    source = "Backblaze's own report"
    if not events:
        events = [{"at": _ts(r["ts"])} for r in recent_repairs(con)]
        source = "Backblaze's log"
    if events:
        times = "once" if len(events) == 1 else f"{len(events)} times"
        text = (f"Backblaze lost track of files it had already uploaded and queued them again: {times}, "
                f"last on {_day(max(e['at'] for e in events))} ({source}).")
        if shrinks:
            text += f" Up to {_gb(max(r['lost_bytes'] for r in shrinks))} went back in the queue at once."
        out.append({"id": "retransmit", "level": "info", "text": text})
    else:
        for i, r in enumerate(shrinks):
            out.append({"id": f"bz_done_shrink_{i}", "level": "info",
                        "text": f"Backblaze rolled back its upload record on {_day(r['at'])}: {_gb(r['lost_bytes'])} "
                                f"of uploads are no longer listed, so those files may upload again."})
    purged = recent_purges(con)
    if purged:
        n = purged["files"]
        out.append({"id": "purged", "level": "info",
                    "text": f"Backblaze's server asked it to upload {n:,} file{'' if n == 1 else 's'} again, "
                            f"last on {_day(purged['at'])}."})
    return out


def diagnostics_lines(con) -> list[str]:
    """For the diagnostics bundle: the raw evidence, paths removed."""
    events = retransmit_events()
    lines = [f"  Retransmit report: {len(events)} recent wrong_size line(s)" if events else "  Retransmit report: none"]
    for e in events[-5:]:
        lines.append(f"    {_day(e['at'])} {e['file']} {e['reason']}")
    for r in _kv_list(con, "bz_done_shrinks")[-5:]:
        lines.append(f"  Upload record got smaller: {r['file']} {r['old_size']} -> {r['new_size']} bytes, "
                     f"{r['lost_files']} files / {r['lost_bytes']} bytes no longer listed")
    purged = recent_purges(con)
    if purged:
        lines.append(f"  Purged lines: {purged['files']} file(s), last record change {_day(purged['at'])}")
    for r in _kv_list(con, "bzdone_repairs")[-5:]:
        lines.append(f"  Upload record replaced by the server's copy: {r['ts']}")
    for r in _kv_list(con, "verify_failed")[-5:]:
        lines.append(f"  Check failed {r['ts']}: {_no_paths(r['result'])}")
    for r in selfheal_results(con)[-10:]:
        lines.append(f"  Self-heal {r['ts']}: {_no_paths(r['result'])}")
    return lines


def _kv_list(con, key: str) -> list[dict]:
    try:
        rows = json.loads(bbdb.kv_get(con, key) or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _ts(stamp: str) -> float:
    try:
        return time.mktime(time.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return time.time()


def _day(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(at))


def _gb(n: int) -> str:
    return f"{n / 1e9:,.1f} GB"


_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/Users/|/Volumes/|/home/)[^\s\"']*")


def _no_paths(text: str) -> str:
    return _PATH_RE.sub("<path>", text)
