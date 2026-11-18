"""Backblaze problems read from Backblaze's own files, for Health and diagnostics.

Every check reads evidence Backblaze writes itself and never guesses from KopiaLens's own counts.
A notice is {"id", "level", "text"}: "warning" only for a confirmed problem, "info" for a sign worth
knowing about. Each check stays quiet when its evidence is absent.
"""
from __future__ import annotations

import json
import os
import re
import time

from . import bbdb, paths
from .safety import open_ro

RETRANSMIT_REPORT = "bzlist_bzdone_retransmit.txt"
SELFHEAL_MARK = "SelfHealBzDoneFiles - "
SELFHEAL_OK = "healthy checkup"
SHRINK_MIN_BYTES = 1_000_000_000    # Backblaze trims a few lines in routine cleanups; a rollback loses far more
RECENT_DAYS = 60                    # older evidence leaves Health
KEEP = 20


# ---- A: Backblaze's own retransmit report --------------------------------------------------
def retransmit_report() -> dict | None:
    """`bzreports/bzlist_bzdone_retransmit.txt`: one `wrong_size` line each time Backblaze replaced
    its upload record with an older copy from its server and queued those files again."""
    path = paths.report(RETRANSMIT_REPORT)
    try:
        updated = os.path.getmtime(path)
        if updated < time.time() - RECENT_DAYS * 86400:
            return None
        with open_ro(path, encoding="utf-8", errors="replace") as fh:
            count = sum(1 for line in fh if "wrong_size" in line)
    except OSError:
        return None
    return {"count": count, "updated": updated} if count else None


# ---- B: an upload record (bz_done) got smaller ---------------------------------------------
def note_shrink(con, fn: str, old_size: int, new_size: int, lost_bytes: int, lost_files: int) -> None:
    """Called by the indexer when a bz_done file it read before is now smaller."""
    rows = _kv_list(con, "bz_done_shrinks")
    rows.append({"file": fn, "at": time.time(), "old_size": old_size, "new_size": new_size,
                 "lost_bytes": lost_bytes, "lost_files": lost_files})
    bbdb.kv_set(con, "bz_done_shrinks", json.dumps(rows[-KEEP:]))


def recent_shrinks(con) -> list[dict]:
    cutoff = time.time() - RECENT_DAYS * 86400
    return [r for r in _kv_list(con, "bz_done_shrinks")
            if r.get("at", 0) >= cutoff and r.get("lost_bytes", 0) >= SHRINK_MIN_BYTES]


# ---- C: self-heal results in the transmit log ----------------------------------------------
def refresh_selfheal(con) -> int:
    """Keep the self-heal result lines from Backblaze's transmit logs (newest KEEP). Each log is read
    from where the last pass stopped; a log that got shorter was started again and is read whole."""
    folder = paths.transmit_log_dir()
    try:
        names = sorted(n for n in os.listdir(folder) if n.startswith("bztransmit") and n.endswith(".log"))
    except OSError:
        return 0
    found: list[dict] = []
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
                found.append({"ts": line[:19], "result": line.split(SELFHEAL_MARK, 1)[1][:300]})
        con.execute("INSERT OR REPLACE INTO ingest (source, mtime, size, extra) VALUES(?,?,?,?)",
                    (key, os.path.getmtime(path), size, None))
    if found:
        rows = _kv_list(con, "selfheal_results") + found
        rows.sort(key=lambda r: r["ts"])
        bbdb.kv_set(con, "selfheal_results", json.dumps(rows[-KEEP:]))
    return len(found)


def selfheal_results(con) -> list[dict]:
    return _kv_list(con, "selfheal_results")


# ---- what Health shows ---------------------------------------------------------------------
def notices(con) -> list[dict]:
    out = []
    rt = retransmit_report()
    if rt:
        when = time.strftime("%Y-%m-%d", time.localtime(rt["updated"]))
        times = "once" if rt["count"] == 1 else f"{rt['count']} times"
        out.append({"id": "retransmit", "level": "info",
                    "text": f"Backblaze lost track of files it had already uploaded and queued them again: "
                            f"{times}, last on {when} (Backblaze's own report)."})
    for i, r in enumerate(recent_shrinks(con)):
        when = time.strftime("%Y-%m-%d", time.localtime(r["at"]))
        out.append({"id": f"bz_done_shrink_{i}", "level": "info",
                    "text": f"Backblaze rolled back its upload record on {when}: {_gb(r['lost_bytes'])} of "
                            f"uploads are no longer listed, so those files may upload again."})
    return out


def diagnostics_lines(con) -> list[str]:
    """For the diagnostics bundle: the raw evidence, paths removed."""
    rt = retransmit_report()
    lines = [f"  Retransmit report: {rt['count']} wrong_size line(s), updated "
             f"{time.strftime('%Y-%m-%d', time.localtime(rt['updated']))}" if rt else "  Retransmit report: none"]
    for r in _kv_list(con, "bz_done_shrinks")[-5:]:
        lines.append(f"  Upload record got smaller: {r['file']} {r['old_size']} -> {r['new_size']} bytes, "
                     f"{r['lost_files']} files / {r['lost_bytes']} bytes no longer listed")
    for r in selfheal_results(con)[-10:]:
        lines.append(f"  Self-heal {r['ts']}: {_no_paths(r['result'])}")
    return lines


def _kv_list(con, key: str) -> list[dict]:
    try:
        rows = json.loads(bbdb.kv_get(con, key) or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _gb(n: int) -> str:
    return f"{n / 1e9:,.1f} GB"


_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/Users/|/Volumes/|/home/)[^\s\"']*")


def _no_paths(text: str) -> str:
    return _PATH_RE.sub("<path>", text)
