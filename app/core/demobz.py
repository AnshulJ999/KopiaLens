"""Fake Backblaze data for a demo folder, read by the real Backblaze indexer.

``write`` lays out a ``bzdata`` folder (volumes, per-drive file lists, file ids, upload queue,
per-day upload records, event log, status reports and exclusion rules) from the demo's own
latest files, plus a few system and temp files the rules exclude. Formats per
``app.backblaze.parsers`` and ``rules``. Nothing is read from this machine's Backblaze.
"""

from __future__ import annotations

import datetime as _dt
import os
import random

from .applog import get_logger

log = get_logger("demobz")

BZ_RECORD = {"id": "bz", "name": "Backblaze", "short_name": "Backblaze", "kind": "backblaze"}
_DAY = 86400.0
_EXCLUDED_TYPES = "dmg,iso,vhd,vhdx,vmdk,tmp,sys,lck"
_CATEGORY = {  # filestats.xml buckets
    "docs": {"doc", "docx", "pdf", "txt", "md", "odt", "rtf", "epub"},
    "spreadsheets": {"xls", "xlsx", "csv", "ods"},
    "presentations": {"ppt", "pptx", "odp"},
    "pict": {"jpg", "jpeg", "png", "gif", "heic", "dng", "svg", "webp", "bmp", "tif"},
    "music": {"mp3", "wav", "flac", "ogg", "m4a", "aac", "mid"},
    "video": {"mp4", "mkv", "mov", "avi", "webm"},
    "zips": {"zip", "7z", "rar", "gz", "tar", "msi", "exe"},
}


def _stamp(t: float) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%Y%m%d%H%M%S")


def _gmt_day(t: float) -> str:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y%m%d")


def _extras(user: str, now: float, r: random.Random) -> list[tuple[str, int, float]]:
    """System, program and temp files on C: that Backblaze's own rules leave out."""
    out = []
    for i in range(r.randint(900, 1400)):
        out.append((rf"C:\Windows\System32\drivers\driver{i:04d}.sys", r.randint(20_000, 900_000),
                    now - r.uniform(30, 900) * _DAY))
    for app in ("Office", "Browser", "Editor", "Player", "Studio"):
        for i in range(r.randint(60, 200)):
            out.append((rf"C:\Program Files\{app}\lib\module{i:03d}.dll", r.randint(40_000, 6_000_000),
                        now - r.uniform(10, 400) * _DAY))
    for i in range(r.randint(200, 500)):
        out.append((rf"C:\Users\{user}\AppData\Local\Temp\tmp{r.getrandbits(32):08x}.tmp",
                    r.randint(0, 2_000_000), now - r.uniform(0, 20) * _DAY))
    return out


def write(root: str, files: list[tuple[str, int, float]], *, user: str, host: str,
          now: float | None = None, seed: int = 1) -> dict:
    """Write ``root`` (a ``bzdata`` folder) for ``files``: (full path, size, mtime)."""
    r = random.Random(f"bz-{seed}")
    now = now or _dt.datetime.now().timestamp()
    # One row per path: several repos (and the extras) can hold the same file; Backblaze counts it once.
    every = list({f[0].lower(): f for f in [f for f in files if len(f[0]) > 3 and f[0][1:3] == ":\\"]
                  + _extras(user, now, r)}.values())
    drives: dict[str, list] = {}
    for f in every:
        drives.setdefault(f[0][0].upper(), []).append(f)
    excluded_dirs = [rf"C:\Users\{user}\AppData\Local\Temp" + "\\"]
    exts = set(_EXCLUDED_TYPES.split(","))

    def excluded(path: str) -> bool:
        p = path.lower()
        return (p[1:].startswith(r":\windows" + "\\") or p[1:].startswith(r":\program files" + "\\")
                or any(p.startswith(d.lower()) for d in excluded_dirs)
                or p.rsplit(".", 1)[-1] in exts)

    for sub in ("bzfilelists", "bzreports", os.path.join("bzbackup", "bzdatacenter"),
                os.path.join("bzlogs", "bzreports_eventlog")):
        os.makedirs(os.path.join(root, sub), exist_ok=True)

    vols, totals, remaining = [], [], []
    backed_all = []
    for letter in sorted(drives):
        guid = "v" + f"{r.getrandbits(92):023x}"
        rows = sorted(drives[letter])
        backed = [f for f in rows if not excluded(f[0])]
        backed_all += backed
        used = sum(f[1] for f in rows)
        total = max(used * 3, 500 * 10**9)
        vols.append(f'<bzvolume bzVolumeGuid="{guid}" mountPointPathHex="{(letter + ":" + chr(92)).encode().hex()}" '
                    f'typeOfVolumeTwoCharCode="hd" lastTimeVolumeWasSeenAttachedGmtMillis="{int(now * 1000)}" '
                    f'numBytesFreeOnVolume="{total - used}" numBytesUsedOnVolume="{used}" '
                    f'numBytesTotalOnVolume="{total}" />')
        totals.append(f'<bzvolume bzVolumeGuid="{guid}" pervol_sel_for_backup_numfiles="{len(backed)}" '
                      f'pervol_sel_for_backup_numbytes="{sum(f[1] for f in backed)}" />')
        left = r.randint(0, 12)
        remaining.append(f'<bzvolume bzVolumeGuid="{guid}" pervol_remaining_files_numfiles="{left}" '
                         f'pervol_remaining_files_numbytes="{left * r.randint(100_000, 9_000_000)}" />')
        with open(os.path.join(root, "bzfilelists", f"{guid}_{letter.lower()}____filelist.dat"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(f"# GmtMillisThisListWasStarted {int(now * 1000)}\n#\n# Dir: {letter}:\\\n"
                     f"# Count: {len(rows)}\n")
            for path, size, mtime in rows:
                fh.write(f"f\t{int(mtime * 1000):x}\t{size}\t{path}\n")

    _xml(root, "bzvolumes.xml", "contents",
         [f'<bzvol_metadata lastAssignedBzVolShortId="{len(vols)}" gmtMillisThisFileWasWritten="{int(now * 1000)}" '
          f'gmtDateTimeFileWasWritten="{_stamp(now)}" />'] + vols)
    nfiles, nbytes = len(backed_all), sum(f[1] for f in backed_all)
    _xml(root, os.path.join("bzreports", "bzstat_totalbackup.xml"), "contents",
         [f'<totals totnumfilesforbackup="{nfiles}" totnumbytesforbackup="{nbytes}" />'] + totals)
    _xml(root, os.path.join("bzreports", "bzstat_remainingbackup.xml"), "contents",
         [f'<remaining remainingnumfilesforbackup="{sum(int(x.split(chr(34))[3]) for x in remaining)}" '
          f'remainingnumbytesforbackup="{sum(int(x.split(chr(34))[5]) for x in remaining)}" />'] + remaining)
    last = now - r.uniform(600, 5400)
    _xml(root, os.path.join("bzreports", "bzstat_lastbackupcompleted.xml"), "contents",
         [f'<lastbackupcompleted gmt_millis="{int(last * 1000)}" localdatetime="{_stamp(last)}" />'])
    _xml(root, "overviewstatus.xml", "status",
         [f'<bztransmit cur_state="idle_waiting" current_file="none" current_file_fullpath="none" '
          f'millisFileWasWritten="{int(now * 1000)}" />'])
    _filestats(root, backed_all, host, now)
    _history(root, backed_all, now, r)
    _rules(root, user, host, excluded_dirs)
    log.info("demo bzdata: %d drives, %d files (%d backed up)", len(drives), len(every), nfiles)
    return {"drives": sorted(drives), "files": len(every), "backed": nfiles, "bytes": nbytes}


def _xml(root: str, rel: str, top: str, lines: list[str]) -> None:
    with open(os.path.join(root, rel), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'<?xml version="1.0" encoding="utf-8" ?>\n<{top}>\n')
        fh.write("\n".join(lines))
        fh.write(f"\n</{top}>\n")


def _filestats(root: str, files: list, host: str, now: float) -> None:
    acc = {c: [0, 0] for c in (*_CATEGORY, "financial", "email", "browserfavorites", "other", "everything")}
    for path, size, _m in files:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("\\", 1)[-1] else ""
        cat = next((c for c, es in _CATEGORY.items() if ext in es), "other")
        for c in (cat, "everything"):
            acc[c][0] += 1
            acc[c][1] += size
    attrs = " ".join(f'{c}num="{n}" {c}totbytes="{b}"' for c, (n, b) in acc.items())
    _xml(root, os.path.join("bzfilelists", "filestats.xml"), "filestats",
         ['<progress good_for_installer="true" totally_final="true" />',
          f'<info datetime="{_stamp(now)}" aonlinename="{host}" plat="win64" osdetail="Windows11" {attrs} />'])


def _history(root: str, files: list, now: float, r: random.Random) -> None:
    """File ids, the upload queue, 45 days of per-day upload records, and the event log."""
    dc = os.path.join(root, "bzbackup", "bzdatacenter")
    with open(os.path.join(root, "bzbackup", "bzfileids.dat"), "w", encoding="utf-8", newline="\n") as fh:
        for i, (path, _s, _m) in enumerate(sorted(files, key=lambda f: f[2])):
            fh.write(f"{0x1000000000000000 + i:016x}\t{path}\n")
    recent = sorted(files, key=lambda f: -f[2])
    queued = recent[: r.randint(3, 20)]
    with open(os.path.join(dc, f"bz_todo_{int(now * 1000)}_0.dat"), "w", encoding="utf-8", newline="\n") as fh:
        for path, size, mtime in queued:
            fh.write(f"1\t+\t{r.getrandbits(64):016x}\t{int(mtime * 1000):016x}\t{size}\t{path}\n")
    events, upload_days = [], []
    for d in range(45, 0, -1):
        day_start = now - d * _DAY
        start = day_start + r.uniform(8, 20) * 3600
        todays = [f for f in files if day_start <= f[2] < day_start + _DAY]
        if not todays and r.random() < 0.6:
            todays = r.sample(files, min(len(files), r.randint(5, 60)))
        name = f"bz_done_{int(start * 1000)}_0.dat"
        with open(os.path.join(dc, name), "w", encoding="utf-8", newline="\n") as fh:
            for path, size, _m in todays:
                t = start + r.uniform(0, 1800)
                fh.write("\t".join(["5", "-", "---", _stamp(t), "f_" + f"{r.getrandbits(80):020x}", "n--",
                                    f"{size:016x}", "n-_a-----", "-" * 20, "-" * 16, "-" * 16, "-", "0",
                                    path]) + "\n")
        os.utime(os.path.join(dc, name), (start + 1800, start + 1800))
        mb = sum(f[1] for f in todays) // 1_000_000
        events.append(f"{_dt.datetime.fromtimestamp(start):%Y-%m-%d %H:%M:%S} - STARTBACKUP")
        if r.random() < 0.35:
            end = start + r.uniform(300, 3000)
            events.append(f"{_dt.datetime.fromtimestamp(end):%Y-%m-%d %H:%M:%S} - END_BACKUP: "
                          f"Backed up {len(todays)} FILES / {mb} MB")
        upload_days.append(f'<one_upload_success_stat date_gmt="{_gmt_day(start)}" '
                           f'num_upload_success="{len(todays)}" num_upload_fail_CvtTooBusy="{r.choice([0, 0, 0, 1])}" '
                           f'num_upload_fail_CvtNoRoom="0" num_upload_fail_UnknownReason="0" />')
    with open(os.path.join(root, "bzlogs", "bzreports_eventlog", "01.log"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("\n".join(events) + "\n")
    _xml(root, os.path.join("bzreports", "bzstat_upload_success.xml"), "upload_success_history", upload_days)
    _xml(root, os.path.join("bzreports", "zbytes_history.xml"), "zbytes_history",
         [f'<one_zbyte_stat date_time_written_gmt="{_stamp(now - d * _DAY)}" '
          f'last_rollback_instr="none{_stamp(now - d * _DAY)}_ok000a" num_z_lines="0" tot_bytes_on_z_lines="0" />'
          for d in range(10, 0, -1)])


def _rules(root: str, user: str, host: str, excluded_dirs: list[str]) -> None:
    def rule(**kw) -> str:
        attrs = {"plat": "win", "osVers": "*", "ruleIsOptional": "f", "skipFirstCharThenStartsWith": "*",
                 "contains_1": "*", "contains_2": "*", "doesNotContain": "*", "endsWith": "*",
                 "hasFileExtension": "*", **kw}
        return "<excludefname_rule " + " ".join(f'{k}="{v}"' for k, v in attrs.items()) + " />"
    _xml(root, "bzexcluderules_mandatory.xml", "bzexclusions",
         ["<!-- Mandatory Exclusions -->", rule(skipFirstCharThenStartsWith=":\\windows\\"),
          rule(skipFirstCharThenStartsWith=":\\program files\\"),
          rule(contains_1="\\appdata\\local\\temp\\"), rule(hasFileExtension="sys")])
    _xml(root, "bzexcluderules_editable.xml", "bzexclusions",
         ["<!-- Editable Exclusions -->", rule(contains_1="\\node_modules\\.cache\\", ruleIsOptional="t"),
          rule(endsWith=".tmp", ruleIsOptional="t")])
    _xml(root, "bzinfo.xml", "bzinfo",
         [f'<myidentity online_hostname="{host}" bzlogin="{user}@example.com" />',
          f'<globalexcludes excludefiletypes="{_EXCLUDED_TYPES}" />',
          '<do_backup>'] + [f'<bzdirfilter dir="{d}" whichfiles="none" />' for d in excluded_dirs] + ['</do_backup>'])


def trim(root: str) -> int:
    """After indexing, drop what only the indexer reads: per-day upload records, file-list bodies
    (the screens use only their names; a header-only list reads as an aborted scan, so a manual
    re-index keeps the index) and all but the tail of the file ids the churn view scans.
    Returns bytes freed."""
    from ..backblaze.queries import _CHURN_TAIL
    freed = 0
    dc = os.path.join(root, "bzbackup", "bzdatacenter")
    for name in os.listdir(dc):
        if name.startswith("bz_done_"):
            freed += os.path.getsize(os.path.join(dc, name))
            os.remove(os.path.join(dc, name))
    lists = os.path.join(root, "bzfilelists")
    for name in os.listdir(lists):
        if name.endswith("filelist.dat"):
            path = os.path.join(lists, name)
            before = os.path.getsize(path)
            with open(path, encoding="utf-8") as fh:
                head = [next(fh, "") for _ in range(4)]
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("".join(head))
            freed += before - os.path.getsize(path)
    ids = os.path.join(root, "bzbackup", "bzfileids.dat")
    size = os.path.getsize(ids)
    if size > _CHURN_TAIL:
        with open(ids, "rb") as fh:
            fh.seek(size - _CHURN_TAIL)
            fh.readline()   # a partial first line
            tail = fh.read()
        with open(ids, "wb") as fh:
            fh.write(tail)
        freed += size - len(tail)
    return freed


def index(root: str) -> dict:
    """Build the demo folder's Backblaze index from ``root`` with the real indexer."""
    from ..backblaze import indexer, paths
    if os.path.normcase(os.path.abspath(paths.bzdata_root())) != os.path.normcase(os.path.abspath(root)):
        raise RuntimeError("demo Backblaze index: the reader is not pointed at the demo's bzdata")
    return indexer.refresh(force=True)
