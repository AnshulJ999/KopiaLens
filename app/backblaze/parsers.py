"""Pure parsers for the Backblaze bzdata formats. Read-only; return/yield data, no DB.

Formats (verified against the live bzdata 2026-06-20):
  - per-volume filelist line:  f \t <hexMtime> \t <sizeBytes> \t <fullPath>
    ('#' = Dir/comment headers; 's' = zero-byte symlink entries, skipped like bbtoolkit)
  - bzfileids.dat line:        <fileIdHex> \t <fullPath>            (cumulative, id-ordered)
  - bz_todo_*.dat line:        n \t + \t fileId \t hexTime \t sizeBytes \t fullPath   (upload queue)
  - eventlog line:             "YYYY-MM-DD HH:MM:SS - STARTBACKUP"
                               "YYYY-MM-DD HH:MM:SS - END_BACKUP: Backed up N FILES / M MB"
  - bigfilelist.dat:           same 'f' line shape as the per-volume filelists
"""

from __future__ import annotations

import os
import re
import zipfile

from .safety import open_ro

_TAB = "\t"
_SEP = "\\"


# ---- per-volume filelists --------------------------------------------------------------
def iter_filelist(path: str):
    """Yield (mtime_hex:str, size:int, full_path:str) for every 'f' (regular file) line."""
    if not os.path.exists(path):
        return
    with open_ro(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("f" + _TAB):
                continue
            parts = line.rstrip("\n").split(_TAB)
            if len(parts) < 4:
                continue
            try:
                sz = int(parts[2])
            except ValueError:
                sz = 0
            yield parts[1], sz, parts[3]


def is_stub_filelist(path: str) -> bool:
    """An aborted scan: the filelist holds its header line and nothing after it (the 77-byte F:-drive
    signature). A finished scan lists folders after the header, however few files the drive has."""
    try:
        with open_ro(path, mode="rb") as fh:
            fh.readline()
            return not fh.read(4096).strip()
    except OSError:
        return True


# ---- bzfileids (cumulative; churn) -----------------------------------------------------
def fileids_total_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def iter_fileids_from(path: str, byte_offset: int = 0):
    """Yield full_path for fileid lines starting at byte_offset (for tail/incremental churn).
    Returns the new end offset via the generator's `.send`-free contract: caller can read
    os.path.getsize after. Tolerates a mid-line offset by skipping the first partial line."""
    if not os.path.exists(path):
        return
    with open_ro(path, encoding="utf-8", errors="replace") as fh:
        if byte_offset > 0:
            fh.seek(byte_offset)
            fh.readline()  # discard a possibly-partial line
        for line in fh:
            t = line.find(_TAB)
            if t == -1:
                continue
            yield line[t + 1:].rstrip("\n")


def folder_key(path: str, depth: int) -> str:
    comps = path.split(_SEP)
    return _SEP.join(comps[:depth + 1]) if len(comps) > depth else path


# ---- upload queue (bz_todo) ------------------------------------------------------------
def parse_queue(dcenter_dir: str) -> list[tuple[int, str]]:
    """All bz_todo_*.dat rows -> [(size, full_path)]."""
    out: list[tuple[int, str]] = []
    if not os.path.isdir(dcenter_dir):
        return out
    for fn in os.listdir(dcenter_dir):
        if fn.startswith("bz_todo_") and fn.endswith(".dat"):
            with open_ro(os.path.join(dcenter_dir, fn), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split(_TAB)
                    if len(parts) < 6:
                        continue
                    try:
                        sz = int(parts[4])
                    except ValueError:
                        sz = 0
                    out.append((sz, parts[5]))
    return out


# ---- bz_done summaries (sessions / mass-deletion detection) ----------------------------
def bz_done_files(dcenter_dir: str) -> list[tuple[str, float, int]]:
    """[(filename, mtime_epoch, size_bytes)] for every bz_done_*.dat, newest first."""
    out: list[tuple[str, float, int]] = []
    if not os.path.isdir(dcenter_dir):
        return out
    for fn in os.listdir(dcenter_dir):
        if fn.startswith("bz_done_") and fn.endswith(".dat"):
            fp = os.path.join(dcenter_dir, fn)
            try:
                out.append((fn, os.path.getmtime(fp), os.path.getsize(fp)))
            except OSError:
                pass
    out.sort(key=lambda x: x[1], reverse=True)
    return out


# ---- bz_done per-day totals (what was ACTUALLY backed up) ------------------------------
# The Sessions view used to price uploads from the eventlog's "END_BACKUP: Backed up N FILES /
# M MB" lines. Backblaze only writes those when a session ends CLEANLY, which it almost never
# does - measured on real data: 25 end-lines in 10 months, one in the last 30 days, and 19
# sessions that started and never reported at all. So that number was not "how much was
# uploaded", it was "how much the one session that bothered to report uploaded".
#
# bz_done_*.dat is the real record: one tab-separated line per file Backblaze finished backing
# up, carrying a timestamp and the file's size in hex. Summing it for the same 30 days gave
# 237 GB across 60,518 files against the eventlog's 22.6 GB.
_BZDONE_TS = 3       # field 3: YYYYMMDDHHMMSS
_BZDONE_SIZE = 6     # field 6: size, hex
_BZDONE_MIN_FIELDS = 8


def parse_bz_done_day_totals(path: str) -> dict[str, dict]:
    """``{"YYYY-MM-DD": {"files": n, "bytes": n}}`` for one bz_done file.

    Rows whose first field is not ``5`` are skipped: Backblaze pads these files with its own
    marker rows (first field ``#``, path ``C:\\zzz_bloat_yyy\\...``) whose "size" is the literal
    ``0123456789abcdef``. Summed naively those add ~4.9e17 bytes and make the day meaningless -
    which is exactly what happened on the first pass over this data."""
    out: dict[str, dict] = {}
    try:
        with open_ro(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < _BZDONE_MIN_FIELDS or parts[0] != "5":
                    continue
                stamp = parts[_BZDONE_TS]
                if len(stamp) < 8 or not stamp[:8].isdigit():
                    continue
                try:
                    size = int(parts[_BZDONE_SIZE], 16)
                except ValueError:
                    continue
                day = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
                entry = out.get(day)
                if entry is None:
                    out[day] = {"files": 1, "bytes": size}
                else:
                    entry["files"] += 1
                    entry["bytes"] += size
    except Exception:
        return out
    return out


# ---- eventlog (backup session history) -------------------------------------------------
_EVT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*-\s*(.*)$")
_END_RE = re.compile(r"END_BACKUP:\s*Backed up\s*(\d+)\s*FILES\s*/\s*(\d+)\s*MB", re.IGNORECASE)


def _emit_eventlog_text(text: str, src: str, out: list[dict]) -> None:
    for line in text.splitlines():
        m = _EVT_RE.match(line.strip())
        if not m:
            continue
        ts, rest = m.group(1), m.group(2)
        if rest.upper().startswith("STARTBACKUP"):
            out.append({"ts": ts, "kind": "session_start", "files": 0, "bytes": 0, "detail": "", "src": src})
        else:
            em = _END_RE.search(rest)
            if em:
                out.append({"ts": ts, "kind": "session_end", "files": int(em.group(1)),
                            "bytes": int(em.group(2)) * 1_000_000, "detail": "", "src": src})
            else:
                out.append({"ts": ts, "kind": "event", "files": 0, "bytes": 0, "detail": rest, "src": src})


def parse_eventlog(eventlog_dir: str) -> list[dict]:
    """Parse all rotated (.zip) + live (.log) eventlog files -> session/event records.
    Rotated .zip are immutable history (~1 year). Read in-memory, read-only."""
    out: list[dict] = []
    if not os.path.isdir(eventlog_dir):
        return out
    for fn in sorted(os.listdir(eventlog_dir)):
        fp = os.path.join(eventlog_dir, fn)
        try:
            if fn.endswith(".log"):
                with open_ro(fp, encoding="utf-8", errors="replace") as fh:
                    _emit_eventlog_text(fh.read(), fn, out)
            elif fn.endswith(".zip"):
                with zipfile.ZipFile(fp) as z:
                    for inner in z.namelist():
                        _emit_eventlog_text(z.read(inner).decode("utf-8", "replace"), fn, out)
        except Exception:
            continue
    out.sort(key=lambda e: e["ts"])
    return out


# ---- report XMLs -----------------------------------------------------------------------
def read_totalbackup(path: str) -> tuple[tuple[int, int], dict[str, tuple[int, int]]]:
    """((grand_files, grand_bytes), {guid: (files, bytes)}) from bzstat_totalbackup.xml."""
    grand = (0, 0)
    per: dict[str, tuple[int, int]] = {}
    if not os.path.exists(path):
        return grand, per
    txt = _slurp(path)
    m = re.search(r'totnumfilesforbackup="(\d+)" totnumbytesforbackup="(\d+)"', txt)
    if m:
        grand = (int(m.group(1)), int(m.group(2)))
    for guid, nf, nb in re.findall(
        r'bzVolumeGuid="(v[0-9a-f]+)" pervol_sel_for_backup_numfiles="(\d+)" '
        r'pervol_sel_for_backup_numbytes="(\d+)"', txt):
        per[guid] = (int(nf), int(nb))
    return grand, per


def read_simple_xml(path: str, patterns: dict[str, str]) -> dict:
    """Extract a flat dict of {key: first-regex-group} from a small status XML."""
    out: dict = {}
    if not os.path.exists(path):
        return out
    txt = _slurp(path)
    for key, pat in patterns.items():
        m = re.search(pat, txt)
        if m:
            out[key] = m.group(1)
    return out


def read_filestats(path: str) -> dict:
    """Category breakdown (docs/pictures/music/video/zips/other/everything num+bytes)."""
    txt = _slurp(path)
    out: dict = {}
    for cat in ("docs", "spreadsheets", "presentations", "financial", "pict", "music",
                "video", "email", "zips", "browserfavorites", "other", "everything"):
        mn = re.search(rf'{cat}num="(\d+)"', txt)
        mb = re.search(rf'{cat}totbytes="(\d+)"', txt)
        if mn and mb:
            out[cat] = {"num": int(mn.group(1)), "bytes": int(mb.group(1))}
    return out


def read_upload_success(path: str) -> list[dict]:
    """Per-day upload success/fail history from bzstat_upload_success.xml."""
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    for m in re.finditer(
        r'date_gmt="(\d+)"\s+num_upload_success="(\d+)"\s+num_upload_fail_CvtTooBusy="(\d+)"'
        r'\s+num_upload_fail_CvtNoRoom="(\d+)"\s+num_upload_fail_UnknownReason="(\d+)"', _slurp(path)):
        d, ok, b1, b2, b3 = m.groups()
        out.append({"date": d, "success": int(ok),
                    "fail": int(b1) + int(b2) + int(b3)})
    return out


def read_zbytes_history(path: str) -> list[dict]:
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    for m in re.finditer(
        r'date_time_written_gmt="(\d+)"\s+last_rollback_instr="([^"]*)"\s+num_z_lines="(\d+)"'
        r'\s+tot_bytes_on_z_lines="(\d+)"', _slurp(path)):
        dt, rb, nz, tb = m.groups()
        out.append({"date_time": dt, "rollback": rb, "z_lines": int(nz), "z_bytes": int(tb)})
    return out


def _slurp(path: str) -> str:
    try:
        with open_ro(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""
