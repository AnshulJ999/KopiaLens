"""Disk lookup: the one way KopiaLens asks "what is on disk right now".

Providers: Everything (Windows, instant, must be running) or the KopiaLens scanner
(``kopialens-scan``, any platform). Setting ``disk_lookup``: everything | auto | scan | off.
The scanner is opt-in (``auto`` or ``scan``): it reads the disk, so nothing is counted unless the
user turned it on, and even then only what a screen asks for. Every scan goes through one queue
here: at most one per disk per lane (short = a screen waiting, long = an explicit or background
count), progress is visible, a scan the page stopped asking about is stopped, and a scan never
outlives KopiaLens. Counted sizes are kept until Recount (``disk_sizes_minutes`` 0) or for the
chosen minutes.
"""

from __future__ import annotations

import atexit
import collections
import contextvars
import fnmatch
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time

from config import DEMO_INSTANCE

from ..settings import load_settings
from .applog import get_logger

log = get_logger("disklookup")

MODES = ("everything", "auto", "scan", "off")
SCANNER_NAME = "kopialens-scan" + (".exe" if sys.platform == "win32" else "")
_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_STATUS_TTL = 30.0
_status_cache: tuple[float, dict] | None = None
_status_lock = threading.Lock()
# Set for the length of one API request about a demo repo (main.demo_repo_disk_middleware): a
# demo repo inside a normal install has fake paths that may exist here, so it never reads the disk.
_DEMO_REQUEST = contextvars.ContextVar("disklookup_demo_request", default=False)
# The old Python folder walk, used when the scanner program is missing. Slow on big folders and
# blocking, so it is off; set KOPIALENS_PY_WALK=1 to use it (development only).
_PY_WALK = os.environ.get("KOPIALENS_PY_WALK") == "1"

_LONG_BUDGET = 1800.0     # an explicit or background count
_FIRST_WAIT = 2.0         # how long opening a folder waits for its count before answering
_LEASE = 30.0             # a screen's count stops when the screen hasn't asked for this long
_STALL = 60.0             # a scan whose counters don't move for this long is stopped
_MAX_SCANS = 3            # scanner processes at once, all disks together
_UNFINISHED_TTL = 600.0   # a folder that ran out of time isn't counted again on its own for this long


def demo_request(on: bool) -> contextvars.Token:
    return _DEMO_REQUEST.set(on)


def end_demo_request(token: contextvars.Token) -> None:
    _DEMO_REQUEST.reset(token)


def _kl() -> dict:
    try:
        return load_settings().get("kopialens", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def mode() -> str:
    if DEMO_INSTANCE or _DEMO_REQUEST.get():
        return "off"   # fake paths can exist on this machine; a demo never reads the disk
    m = str(_kl().get("disk_lookup", "everything")).lower()
    return m if m in MODES else "everything"


def scan_budget() -> float:
    try:
        return max(1.0, float(_kl().get("disk_scan_budget", 15)))
    except Exception:  # noqa: BLE001
        return 15.0


def _threads_setting() -> int:
    try:
        n = int(_kl().get("disk_scan_threads", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    return max(0, min(n, 64))


def _sizes_ttl() -> float | None:
    """How long a counted size is used; None = until Recount."""
    try:
        m = float(_kl().get("disk_sizes_minutes", 0) or 0)
    except (TypeError, ValueError):
        m = 0
    return None if m <= 0 else max(1.0, m) * 60


def _opt(key: str) -> bool:
    return _kl().get(key) is True


def _background_on() -> bool:
    return _opt("disk_scan_background")


def follow_links() -> bool:
    return _opt("disk_follow_links")


def ignore_search_on() -> bool:
    """The nightly search for ignore files on disk (scanner only; Everything always answers)."""
    return _opt("disk_ignore_search")


# ---- the KopiaLens scanner program ----------------------------------------------------------------

_scanner: tuple[str | None] | None = None


def _scanner_candidates() -> list[str]:
    out = []
    env = os.environ.get("KOPIALENS_SCANNER")
    if env:
        out.append(env)
    for raw in (sys.executable, sys.argv[0] if sys.argv else ""):
        if raw:
            out.append(os.path.join(os.path.dirname(os.path.abspath(raw)), SCANNER_NAME))
    kl_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out.append(os.path.join(kl_root, "shell", "disk-scanner", "target", "release", SCANNER_NAME))
    found = shutil.which("kopialens-scan")
    if found:
        out.append(found)
    return out


def scanner_path() -> str | None:
    """The scanner program, found once per process (env KOPIALENS_SCANNER, next to the server, a
    source checkout's build, PATH)."""
    global _scanner
    if _scanner is None:
        _scanner = (next((p for p in _scanner_candidates() if os.path.isfile(p)), None),)
        log.info("disk scanner: %s", _scanner[0] or "not found")
    return _scanner[0]


_SYSTEM_ROOTS = ("/proc", "/sys", "/dev", "/run", "/etc", "/boot", "/snap", "/var/lib/docker")


def _skips() -> list[str]:
    """Folders never entered: KopiaLens's own data and snapshot mounts, and system plumbing."""
    from config import DATA_DIR
    from . import host
    out = [DATA_DIR]
    mounts = host.mount_dir()
    if mounts:
        out.append(mounts)
    if sys.platform != "win32":
        out += list(_SYSTEM_ROOTS)
    return out


# ---- what kind of disk a path is on, and how many threads suit it --------------------------------

_KIND: dict[str, str] = {}
_NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "sshfs", "9p", "afpfs", "webdav",
               "davfs", "fuse.rclone", "fakeowner", "grpcfuse", "virtiofs", "fuse.grpcfuse"}


def _volume(path: str) -> str:
    """The disk a path is on, as a key: a drive letter or share on Windows, a device elsewhere."""
    if sys.platform == "win32":
        return (os.path.splitdrive(path)[0] or path[:2]).lower()
    try:
        return f"dev:{os.stat(path).st_dev}"
    except OSError:
        return path.split("/", 2)[1] if path.startswith("/") and "/" in path[1:] else path


def _win_seek_penalty(letter: str) -> bool | None:
    """True for a spinning disk, False for an SSD, None when Windows won't say."""
    import ctypes
    from ctypes import wintypes

    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int), ("Additional", ctypes.c_ubyte * 1)]

    class Penalty(ctypes.Structure):
        _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD), ("IncursSeekPenalty", ctypes.c_ubyte)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.CreateFileW("\\\\.\\" + letter + ":", 0, 3, None, 3, 0, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        q, out, got = Query(7, 0), Penalty(), wintypes.DWORD()
        ok = k32.DeviceIoControl(handle, 0x2D1400, ctypes.byref(q), ctypes.sizeof(q), ctypes.byref(out),
                                 ctypes.sizeof(out), ctypes.byref(got), None)
        return bool(out.IncursSeekPenalty) if ok else None
    finally:
        k32.CloseHandle(handle)


def _posix_kind(path: str) -> str:
    try:
        import psutil
        best = None
        for part in psutil.disk_partitions(all=True):
            mp = part.mountpoint or ""
            if (path == mp or path.startswith(mp.rstrip("/") + "/")) and (best is None or len(mp) > len(best.mountpoint)):
                best = part
        if best is not None and (best.fstype or "").lower() in _NETWORK_FS:
            return "network"
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("linux"):
        try:
            st = os.stat(path)
            dev = f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
            real = os.path.realpath(dev)
            for cand in (os.path.join(real, "queue", "rotational"), os.path.join(os.path.dirname(real), "queue", "rotational")):
                if os.path.isfile(cand):
                    with open(cand, encoding="ascii") as fh:
                        return "hdd" if fh.read().strip() == "1" else "ssd"
        except OSError:
            pass
        return "unknown"
    return "ssd"   # macOS: internal disks are SSDs


def disk_kind(path: str) -> str:
    """"ssd" | "hdd" | "network" | "unknown", remembered per disk."""
    vol = _volume(path)
    if vol in _KIND:
        return _KIND[vol]
    kind = "unknown"
    try:
        if sys.platform == "win32":
            import ctypes
            if path.startswith(("\\\\", "//")):
                kind = "network"
            elif vol[1:2] == ":":
                if ctypes.windll.kernel32.GetDriveTypeW(vol.upper() + "\\") == 4:   # DRIVE_REMOTE
                    kind = "network"
                else:
                    seek = _win_seek_penalty(vol[:1].upper())
                    kind = "unknown" if seek is None else ("hdd" if seek else "ssd")
        else:
            kind = _posix_kind(path)
    except Exception as exc:  # noqa: BLE001 - only picks a thread count
        log.debug("disk kind of %s: %s", path, exc)
    _KIND[vol] = kind
    return kind


def _threads_for(path: str) -> int:
    n = _threads_setting()
    if n:
        return n
    if disk_kind(path) == "ssd":
        return max(1, min(os.cpu_count() or 4, 8))
    return 2   # hard disks, shares, and disks that don't say (usually USB drives)


# ---- counted folder sizes (path -> counted time, size, files) ------------------------------------

_TOTALS: collections.OrderedDict[str, tuple[float, int, int]] = collections.OrderedDict()
_TOTALS_MAX = 50000
_UNFINISHED: dict[str, float] = {}     # folders a time limit or Stop cut, not counted again on their own
_NOTES: dict[str, dict] = {}           # folder -> {unreadable, other_fs} from its last count
_TOTALS_LOCK = threading.Lock()


def _ckey(path: str) -> str:
    p = path.rstrip("\\/") or path
    return p.lower() if sys.platform == "win32" else p


def counted(path: str) -> tuple[int, int, float] | None:
    """(size, files, counted at) when the folder's size is known and still fresh."""
    ttl = _sizes_ttl()
    with _TOTALS_LOCK:
        hit = _TOTALS.get(_ckey(path))
        if not hit or (ttl is not None and time.time() - hit[0] > ttl):
            return None
        _TOTALS.move_to_end(_ckey(path))
        return hit[1], hit[2], hit[0]


def _remember(path: str, size: int, files: int, at: float) -> None:
    with _TOTALS_LOCK:
        key = _ckey(path)
        old = _TOTALS.get(key)
        if old is not None and old[0] > at:
            return   # a count that started later (of this folder or below it) already answered
        _TOTALS[key] = (at, size, files)
        _TOTALS.move_to_end(key)
        _UNFINISHED.pop(key, None)
        while len(_TOTALS) > _TOTALS_MAX:
            _TOTALS.popitem(last=False)


def _unfinished(path: str) -> bool:
    with _TOTALS_LOCK:
        at = _UNFINISHED.get(_ckey(path))
    return at is not None and time.time() - at < _UNFINISHED_TTL


def _within(key: str, base: str) -> bool:
    return key == base or key.startswith((base + "\\", base + "/")) or base == "/"


def forget(path: str) -> None:
    """Drops what is known about ``path`` and everything below it (Recount)."""
    base = _ckey(path)
    with _TOTALS_LOCK:
        for store in (_TOTALS, _UNFINISHED, _NOTES):
            for k in [k for k in store if _within(k, base)]:
                del store[k]


def clear_cache() -> None:
    with _TOTALS_LOCK:
        _TOTALS.clear()
        _UNFINISHED.clear()
        _NOTES.clear()


def notes_for(folder: str) -> dict:
    """Unreadable folders and other disks the last count of ``folder`` (or a folder above it) met."""
    base = _ckey(folder)
    unreadable: list[str] = []
    other: list[str] = []
    with _TOTALS_LOCK:
        for k, n in _NOTES.items():
            if _within(base, k):
                unreadable += [p for p in n["unreadable"] if _within(_ckey(p), base)]
                other += [p for p in n["other_fs"] if _within(_ckey(p), base)]
    return {"unreadable": sorted(set(unreadable)), "other_fs": sorted(set(other))}


# ---- scans: one queue for every scan this process runs -------------------------------------------

_ids = itertools.count(1)


class Scan:
    """One scanner run. ``lane`` "short" (a screen waits for it, time-limited) or "long" (an
    explicit or background count, shown with Stop). ``lease_until`` set = stops when no screen
    has asked about it for ``_LEASE`` seconds."""

    def __init__(self, kind: str, path: str, args: list[str], lane: str, budget: float, depth: int = 0,
                 lease: bool = False, why: str = ""):
        self.id = next(_ids)
        self.kind, self.path, self.args, self.lane, self.budget, self.depth = kind, path, args, lane, budget, depth
        self.why = why
        self.volume = _volume(path)
        self.lease_until: float | None = time.time() + _LEASE if lease else None
        self.state = "queued"         # queued | running | done | stopped
        self.stop_reason = ""
        self.files = self.dirs = self.bytes = 0
        self.started = 0.0
        self.rows: list[dict] = []
        self.end: dict = {}
        self.proc: subprocess.Popen | None = None
        self.done = threading.Event()

    def renew(self) -> None:
        if self.lease_until is not None:
            self.lease_until = time.time() + _LEASE

    def info(self) -> dict:
        return {"id": self.id, "path": self.path, "kind": self.kind, "state": self.state, "lane": self.lane,
                "why": self.why, "files": self.files, "folders": self.dirs, "bytes": self.bytes,
                "seconds": round(time.time() - self.started, 1) if self.started else 0}


_SCANS: dict[int, Scan] = {}
_LANES: dict[tuple[str, str], collections.deque] = {}
_LANE_BUSY: set[tuple[str, str]] = set()
_SCANS_LOCK = threading.Lock()
_PROCS = threading.BoundedSemaphore(_MAX_SCANS)


def _active() -> list[Scan]:
    with _SCANS_LOCK:
        return [s for s in _SCANS.values() if s.state in ("queued", "running")]


def scans() -> list[dict]:
    """Scans queued or running, for the screens (Excluded shows them with Stop)."""
    return [s.info() for s in _active()]


def _submit(scan: Scan) -> Scan:
    key = (scan.volume, scan.lane)
    with _SCANS_LOCK:
        _SCANS[scan.id] = scan
        _LANES.setdefault(key, collections.deque()).append(scan)
        if key in _LANE_BUSY:
            return scan
        _LANE_BUSY.add(key)
    threading.Thread(target=_lane_worker, args=(key,), name=f"disk-scan-{scan.lane}", daemon=True).start()
    return scan


def _lane_worker(key: tuple[str, str]) -> None:
    while True:
        with _SCANS_LOCK:
            q = _LANES.get(key)
            if not q:
                _LANE_BUSY.discard(key)
                return
            scan = q.popleft()
        if scan.state == "queued":
            with _PROCS:   # waits while other scans run; a Stop or a leave can land meanwhile
                if scan.done.is_set() or scan.state != "queued":
                    pass
                elif scan.lease_until is not None and time.time() > scan.lease_until:
                    _finish(scan, "stopped", "left")
                else:
                    _execute(scan)
        _forget_old()


def _forget_old() -> None:
    cutoff = time.time() - 300
    with _SCANS_LOCK:
        for sid in [i for i, s in _SCANS.items() if s.done.is_set() and s.started < cutoff]:
            del _SCANS[sid]


def _finish(scan: Scan, state: str, reason: str = "") -> None:
    scan.state, scan.stop_reason = state, reason
    scan.done.set()


def _watch(scan: Scan) -> None:
    """Stops a scan that was stopped, left by its screen, stalled, or ran far past its limit."""
    last = (-1, -1)
    moved = time.time()
    hard = scan.started + scan.budget + 30
    while not scan.done.is_set():
        time.sleep(1)
        now = time.time()
        reason = ""
        if scan.stop_reason in ("stop", "superseded", "left"):
            reason = scan.stop_reason
        elif scan.lease_until is not None and now > scan.lease_until:
            reason = "left"
        elif now > hard:
            reason = "limit"
        else:
            counters = (scan.files, scan.dirs)
            if counters != last:
                last, moved = counters, now
            elif now - moved > _STALL:
                reason = "stalled"
        if reason:
            scan.stop_reason = reason
            try:
                if scan.proc and scan.proc.poll() is None:
                    scan.proc.kill()
            except OSError:
                pass
            if reason == "stalled":
                log.warning("disk scan of %s stopped: no progress for %d s", scan.path, _STALL)
            return


def _execute(scan: Scan) -> None:
    exe = scanner_path()
    if not exe:
        _finish(scan, "stopped", "no scanner")
        return
    cmd = [exe, *scan.args, "--time-ms", str(int(scan.budget * 1000)), "--threads", str(_threads_for(scan.path)),
           "--progress-ms", "1000", "--watch-stdin"]
    for s in _skips():
        cmd += ["--skip", s]
    if sys.platform != "win32":
        cmd.append("--one-fs")
    if follow_links():
        cmd.append("--follow-links")
    scan.state, scan.started = "running", time.time()
    try:
        scan.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", errors="replace", creationflags=_NOWINDOW)
        _LIVE.add(scan.proc)
    except OSError as exc:
        log.warning("disk scanner failed to start (%s): %s", scan.path, exc)
        _finish(scan, "stopped", "failed")
        return
    threading.Thread(target=_watch, args=(scan,), name="disk-scan-watch", daemon=True).start()
    rows: list[dict] = []
    try:
        for line in scan.proc.stdout:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("progress"):
                scan.files, scan.dirs, scan.bytes = int(row["files"]), int(row["dirs"]), int(row["bytes"])
            else:
                rows.append(row)
        scan.proc.wait()
    finally:
        _LIVE.discard(scan.proc)
        try:
            scan.proc.stdin.close()
        except OSError:
            pass
    if rows and rows[-1].get("end"):
        scan.end, scan.rows = rows[-1], rows[:-1]
        if scan.kind == "totals":
            _store_totals(scan)
        log.info("disk scan %s %s: %s in %.1f s", scan.kind, scan.path,
                 "complete" if scan.end.get("complete") else "stopped at the time limit", time.time() - scan.started)
        _finish(scan, "done")
        return
    if scan.stop_reason in ("stop", "stalled") and scan.kind == "totals":
        with _TOTALS_LOCK:
            _UNFINISHED[_ckey(scan.path)] = time.time()
    log.info("disk scan %s %s ended: %s", scan.kind, scan.path, scan.stop_reason or f"exit {scan.proc.returncode}")
    _finish(scan, "stopped", scan.stop_reason or "failed")


def _store_totals(scan: Scan) -> None:
    at = scan.started
    # A place that could not be read at all (an offline share) is not counted, not "0 bytes".
    # Unreadable folders below it keep the partial total plus a note.
    root_unreadable = any(_ckey(p) == _ckey(scan.path) for p in scan.end.get("unreadable") or [])
    for r in scan.rows:
        rel = r.get("rel") or []
        full = os.path.join(scan.path, *rel) if rel else scan.path
        if r.get("complete") and not (root_unreadable and not rel):
            _remember(full, int(r["size"]), int(r["files"]), at)
        else:
            with _TOTALS_LOCK:
                _UNFINISHED[_ckey(full)] = time.time()
    unreadable = [p for p in scan.end.get("unreadable") or [] if not _system_folder(p)]
    with _TOTALS_LOCK:
        _NOTES[_ckey(scan.path)] = {"unreadable": unreadable, "other_fs": list(scan.end.get("other_fs") or [])}


# Folders the operating system keeps for itself; nobody can read them, so they aren't reported.
_SYSTEM_FOLDERS = {"system volume information", "$recycle.bin", "windowsapps", "config.msi", "lost+found"}


def _system_folder(path: str) -> bool:
    return any(part.lower() in _SYSTEM_FOLDERS for part in path.replace("\\", "/").split("/"))


_LIVE: set = set()


@atexit.register
def _kill_all() -> None:
    for p in list(_LIVE):
        try:
            p.kill()
        except OSError:
            pass


def _totals_scan(path: str, *, lane: str, budget: float, depth: int, lease: bool, why: str) -> Scan:
    """The count of ``path`` already queued or running, else a new one. A screen's short count of
    the same folder gives way to an explicit long one."""
    key = _ckey(path)
    for s in _active():
        if s.kind != "totals" or _ckey(s.path) != key:
            continue
        if lane == "long" and s.lane == "short":
            s.stop_reason = "superseded"
            if s.state == "queued":
                _finish(s, "stopped", "superseded")
            continue
        s.renew()
        if not lease:
            s.lease_until = None
        return s
    return _submit(Scan("totals", path, ["totals", path, "--depth", str(depth)], lane, budget, depth, lease, why))


def count(path: str, why: str = "count") -> dict:
    """An explicit count of ``path`` (Count / Recount): long lane, shown with Stop, forgets what
    was known first."""
    forget(path)
    return _totals_scan(path, lane="long", budget=_LONG_BUDGET, depth=2, lease=False, why=why).info()


def stop(scan_id: int) -> bool:
    with _SCANS_LOCK:
        s = _SCANS.get(int(scan_id))
    if not s or s.done.is_set():
        return False
    s.stop_reason = "stop"
    if s.state == "queued":
        _finish(s, "stopped", "stop")
    return True


def stop_all_left() -> None:
    """Stops every count started by a screen (the Excluded tab was left)."""
    for s in _active():
        if s.lease_until is not None:
            s.stop_reason = "left"
            if s.state == "queued":
                _finish(s, "stopped", "left")


def _covering(folder: str) -> Scan | None:
    """A totals scan queued or running that will count ``folder``'s children."""
    key = _ckey(folder)
    parent = _ckey(os.path.dirname(folder.rstrip("\\/")) or folder)
    for s in _active():
        if s.kind != "totals":
            continue
        sk = _ckey(s.path)
        if sk == key or (s.depth >= 2 and sk == parent and parent != key):
            return s
    return None


# ---- status ----------------------------------------------------------------------------------

def everything_state() -> dict:
    """Is Everything installed and answering through es.exe?"""
    from . import everything
    if sys.platform != "win32":
        return {"supported": False, "installed": False, "running": False, "es_path": ""}
    exe = everything.es_path()
    found = os.path.isfile(exe) or bool(shutil.which(exe))
    running = bool(found) and everything.available()
    installed = running or everything.program_installed()
    return {"supported": True, "installed": installed, "running": running, "es_path": exe, "es_found": found}


def _scan_ready() -> bool:
    return bool(scanner_path()) or _PY_WALK


def status(refresh: bool = False) -> dict:
    """Which provider answers, and why. Cached briefly: the Everything probe is a subprocess."""
    global _status_cache
    if _DEMO_REQUEST.get():   # never cached: the shared cache answers for the real repos
        return {"mode": "off", "provider": None, "available": False,
                "everything": {"supported": sys.platform == "win32", "installed": None, "running": None},
                "note": "Sample data isn't on this computer's disk.", "scan_budget": scan_budget(), "scans": []}
    with _status_lock:
        if not refresh and _status_cache and time.time() - _status_cache[0] < _STATUS_TTL:
            return {**_status_cache[1], "scans": scans()}
    m = mode()
    ev = everything_state() if m in ("auto", "everything") else {"supported": sys.platform == "win32",
                                                                   "installed": None, "running": None}
    note = ""
    if m == "off":
        provider, note = None, "Disk scanning is off."
    elif m == "everything":
        provider = "everything" if ev["running"] else None
        if not provider:
            note = ("Everything is not running." if ev["installed"] else "Everything is not installed.") \
                if ev["supported"] else "Disk scanning is off."
    else:
        if m == "auto" and ev["running"]:
            provider = "everything"
        else:
            provider = "scan" if _scan_ready() else None
            note = "" if provider else "The KopiaLens scanner isn't part of this install."
    out = {"mode": m, "provider": provider, "available": provider is not None, "everything": ev,
           "scanner": {"found": bool(scanner_path())}, "note": note, "scan_budget": scan_budget()}
    with _status_lock:
        _status_cache = (time.time(), out)
    return {**out, "scans": scans()}


def provider() -> str | None:
    return status()["provider"]


# ---- the old Python walk (off unless KOPIALENS_PY_WALK=1) ----------------------------------------

def _walk(root: str, deadline: float):
    """os.walk with a deadline; yields (dirpath, dirnames, filenames) and stops when time is up."""
    for entry in os.walk(root, onerror=lambda _e: None):
        yield entry
        if time.time() > deadline:
            raise TimeoutError


def _scan_total(path: str, budget: float) -> tuple[int | None, int | None]:
    deadline = time.time() + budget
    size = count_ = 0
    try:
        for dirpath, _dirs, files in _walk(path, deadline):
            for f in files:
                try:
                    size += os.stat(os.path.join(dirpath, f)).st_size
                    count_ += 1
                except OSError:
                    pass
    except TimeoutError:
        return None, None
    return size, count_


# ---- the questions features ask ---------------------------------------------------------------

def children(folder: str) -> list[dict]:
    """Direct children: name, is_dir, mtime, size (folders recursive), and for the scanner a
    ``state`` per folder: counted | counting | not_counted | other_disk, plus ``files`` and
    ``counted_at``. Opening a folder starts one count of it (two levels, so a child opens
    instantly) and waits a moment; what isn't done shows as counting and fills in on the next ask."""
    p = provider()
    if p == "everything":
        from . import everything
        return everything.es_children(folder)
    if p is None:
        return []
    try:
        entries = list(os.scandir(folder))
        folder_dev = os.stat(folder).st_dev if sys.platform != "win32" else None
    except OSError:
        return []
    follow = follow_links()
    rows = []
    for e in entries:
        try:
            # A Windows junction is a link too; the scanner doesn't follow either unless asked.
            link = e.is_symlink() or bool(getattr(e, "is_junction", lambda: False)())
            if link and not follow:
                continue   # not followed, like kopia, which stores the link itself
            st = e.stat(follow_symlinks=link)
            is_dir = e.is_dir(follow_symlinks=link)
        except OSError:
            continue
        other = bool(is_dir and folder_dev is not None and st.st_dev != folder_dev)
        rows.append((e, st, is_dir, other))
    if not scanner_path():
        return _children_walked(rows) if _PY_WALK else _children_plain(rows)
    want = [e.path for e, _st, d, other in rows if d and not other and counted(e.path) is None and not _unfinished(e.path)]
    scan = _covering(folder)
    if scan:
        scan.renew()
    elif want:
        bg = _background_on()
        scan = _totals_scan(folder, lane="long" if bg else "short", budget=_LONG_BUDGET if bg else scan_budget(),
                            depth=2, lease=not bg, why="background" if bg else "open")
        scan.done.wait(_FIRST_WAIT)
    running = scan is not None and not scan.done.is_set()
    out: list[dict] = []
    for e, st, is_dir, other in rows:
        row = {"name": e.name, "is_dir": is_dir, "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))}
        if not is_dir:
            row.update(size=st.st_size, files=1)
        elif other:
            row.update(size=None, state="other_disk")
        else:
            hit = counted(e.path)
            if hit:
                row.update(size=hit[0], files=hit[1], counted_at=hit[2], state="counted")
            else:
                row.update(size=None, state="counting" if running else "not_counted")
        out.append(row)
    return out


def not_indexed(path: str) -> bool:
    """Everything reported nothing here, yet the folder holds something: Everything doesn't index it."""
    try:
        with os.scandir(path) as it:
            return next(it, None) is not None
    except OSError:
        return False


def _children_plain(rows) -> list[dict]:
    return [{"name": e.name, "is_dir": d, "size": None if d else st.st_size, "files": None if d else 1,
             "state": "not_counted" if d else None,
             "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))} for e, st, d, _o in rows]


def _children_walked(rows) -> list[dict]:
    per_dir = scan_budget() / 4
    out = []
    for e, st, d, _o in rows:
        size, files = _scan_total(e.path, per_dir) if d else (st.st_size, 1)
        out.append({"name": e.name, "is_dir": d, "size": size, "files": files,
                    "state": ("counted" if size is not None else "not_counted") if d else None,
                    "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))})
    return out


def drive_totals(path: str) -> dict:
    """A drive's or added place's counted size, never started by looking: {size, files, state,
    counted_at}. With "Count drives automatically" on, an uncounted one starts a long count."""
    p = provider()
    if p == "everything":
        size, files = recursive_size(path), recursive_count(path)
        if not size and not files and not_indexed(path):
            return {"size": None, "files": None, "state": "not_indexed"}
        return {"size": size, "files": files, "state": "counted"}
    if p != "scan":
        return {"size": None, "files": None, "state": "not_counted"}
    hit = counted(path)
    if hit:
        return {"size": hit[0], "files": hit[1], "state": "counted", "counted_at": hit[2]}
    if any(s.kind == "totals" and _ckey(s.path) == _ckey(path) for s in _active()):
        return {"size": None, "files": None, "state": "counting"}
    if _opt("disk_count_drives") and scanner_path() and not _unfinished(path):
        _totals_scan(path, lane="long", budget=_LONG_BUDGET, depth=2, lease=False, why="drive")
        return {"size": None, "files": None, "state": "counting"}
    return {"size": None, "files": None, "state": "not_counted"}


def _totals_now(path: str, fresh: bool) -> tuple[int | None, int | None]:
    """Size and file count, waiting up to the scan time limit (a job measuring a folder)."""
    if not fresh:
        hit = counted(path)
        if hit:
            return hit[0], hit[1]
    if not scanner_path():
        return _scan_total(path, scan_budget()) if _PY_WALK else (None, None)
    if fresh:
        forget(path)
    scan = _totals_scan(path, lane="short", budget=scan_budget(), depth=0, lease=False, why="measure")
    scan.done.wait(scan_budget() + 35)
    hit = counted(path)
    return (hit[0], hit[1]) if hit else (None, None)


def recursive_size(path: str, fresh: bool = False) -> int | None:
    p = provider()
    if p == "everything":
        from . import everything
        return everything.recursive_size(path)
    return _totals_now(path, fresh)[0] if p == "scan" else None


def recursive_count(path: str, fresh: bool = False) -> int | None:
    p = provider()
    if p == "everything":
        from . import everything
        return everything.recursive_count(path)
    return _totals_now(path, fresh)[1] if p == "scan" else None


_SYSTEM_ROOTS_SET = frozenset(_SYSTEM_ROOTS)


def _user_folder(mp: str) -> bool:
    """Linux/macOS: a mounted folder a user could back up - not a single file Docker mounts in
    (/etc/hosts), not system plumbing, not KopiaLens's own data folder."""
    from config import DATA_DIR
    if not os.path.isdir(mp) or any(mp == r or mp.startswith(r + "/") for r in _SYSTEM_ROOTS_SET):
        return False
    data = os.path.realpath(DATA_DIR)
    real = os.path.realpath(mp)
    return not (real == data or real.startswith(data + "/"))


def list_drives(local: bool | None = None) -> list[dict]:
    """Local fixed drives (Windows) or mounted filesystems, with used/total bytes, then the places
    added in Settings > Disk scanning. ``local`` None = the "Show this computer's drives" setting."""
    if DEMO_INSTANCE or _DEMO_REQUEST.get():
        return []
    import psutil
    if local is None:
        local = _kl().get("disk_show_local", True) is not False
    out: list[dict] = []
    for part in psutil.disk_partitions(all=False) if local else []:
        mp = part.mountpoint or ""
        if sys.platform == "win32":
            if "fixed" not in (part.opts or "").lower() or ":" not in mp:
                continue
            mp = mp if mp.endswith("\\") else mp + "\\"
        elif not _user_folder(mp):
            continue
        try:
            u = psutil.disk_usage(mp)
            out.append({"path": mp, "used": int(u.used), "total": int(u.total)})
        except Exception:  # noqa: BLE001
            out.append({"path": mp, "used": 0, "total": 0})
    out.sort(key=lambda d: d["path"])
    have = {_ckey(d["path"]) for d in out}
    for pl in places():
        if _ckey(pl["path"]) in have:
            continue
        try:
            u = psutil.disk_usage(pl["path"])
            used, total = int(u.used), int(u.total)
        except Exception:  # noqa: BLE001 - an unreachable share still shows, with no usage
            used = total = 0
        out.append({"path": pl["path"], "used": used, "total": total, "place": pl})
    return out


# ---- extra places to scan, and how they map onto backed-up paths ---------------------------------

def places() -> list[dict]:
    """Settings > Disk scanning: extra folders to check (a network share, a mounted folder), each
    optionally backed up under another path (``backup_path``) on another computer (``computer``)."""
    out = []
    for raw in _kl().get("disk_paths") or []:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "").strip()
        if not path:
            continue
        out.append({"path": path, "backup_path": str(raw.get("backup_path") or "").strip(),
                    "computer": str(raw.get("computer") or "").strip().lower()})
    return out


def _under(path: str, base: str) -> str | None:
    """The part of ``path`` below ``base`` ("" for base itself), by the base's own path rules."""
    from . import paths as pathrules
    sep = pathrules.path_sep(base)
    fold = sep == pathrules.WIN_SEP
    b = base.rstrip("\\/") or base
    p = path.rstrip("\\/") or path
    if (p.lower() if fold else p) == (b.lower() if fold else b):
        return ""
    head = b + sep if not b.endswith(sep) else b
    if (p.lower() if fold else p).startswith(head.lower() if fold else head):
        return p[len(head):]
    return None


def mapping_for(folder: str) -> dict | None:
    """The place whose folder holds ``folder`` and that is backed up under another path, with the
    backed-up equivalent of ``folder`` (``backup_folder``) and the computer (or "" for any)."""
    from . import paths as pathrules
    best = None
    for pl in places():
        if not pl["backup_path"]:
            continue
        rest = _under(folder, pl["path"])
        if rest is None or (best and len(pl["path"]) <= len(best[0]["path"])):
            continue
        best = (pl, rest)
    if not best:
        return None
    pl, rest = best
    disk_sep = pathrules.path_sep(pl["path"])
    bsep = pathrules.path_sep(pl["backup_path"])
    parts = [s for s in rest.split(disk_sep) if s]
    root = pl["backup_path"].rstrip("\\/") or pl["backup_path"]
    if not parts:
        target = pl["backup_path"]
    else:
        target = (root if root.endswith(bsep) else root + bsep) + bsep.join(parts)
    return {"place": pl, "backup_folder": target, "computer": pl["computer"]}


def find(root: str, name: str | list[str], folders_only: bool = False, limit: int = 20000) -> tuple[list[dict], bool]:
    """Entries under ``root`` whose name matches ``name`` (one glob or several); (rows, complete)."""
    names = [name] if isinstance(name, str) else list(name)
    if not names:
        return [], True
    p = provider()
    if p == "everything":
        from . import everything
        rows: list[dict] = []
        complete = True
        for n in names:
            got = everything.find(root, n, folders_only, limit)
            rows += got
            complete = complete and len(got) < limit
        return rows, complete
    if p is None:
        return [], False
    fold = sys.platform == "win32"
    if scanner_path():
        args = ["find", root, names[0]]
        for n in names[1:]:
            args += ["--or", n]
        args += ["--limit", str(max(1, int(limit)))]
        if folders_only:
            args.append("--dirs")
        if fold:
            args.append("--fold")
        scan = _submit(Scan("find", root, args, "short", scan_budget(), why="find"))
        scan.done.wait(scan_budget() + 35)
        if scan.state != "done":
            return [], False
        rows = [{"path": r["path"], "size": r.get("size"), "is_dir": bool(r.get("dir"))} for r in scan.rows]
        return rows, bool(scan.end.get("complete"))
    if not _PY_WALK:
        return [], False
    rows = []
    pats = [n.lower() if fold else n for n in names]
    deadline = time.time() + scan_budget()
    try:
        for dirpath, dirs, files in _walk(root, deadline):
            entries = [(d, True) for d in dirs] + ([] if folders_only else [(f, False) for f in files])
            for n, is_dir in entries:
                if any(fnmatch.fnmatchcase(n.lower() if fold else n, pat) for pat in pats):
                    full = os.path.join(dirpath, n)
                    size = None if is_dir else _stat_size(full)
                    rows.append({"path": full, "size": size, "is_dir": is_dir})
                    if len(rows) >= limit:
                        return rows, False
    except TimeoutError:
        return rows, False
    return rows, True


def _stat_size(path: str) -> int | None:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


# ---- rule preview: what an active exclusion rule keeps out on disk ----------------------------

def _search_parts(pattern: str) -> tuple[str, str, bool]:
    """(literal leading folders for an anchored rule, last-segment name glob, dir_only)."""
    body = pattern.strip()
    dir_only = body.endswith("/")
    anchored = body.startswith("/")
    segs = [s for s in body.strip("/").split("/") if s]
    if not segs:
        return "", "*", dir_only
    last = segs[-1]
    glob = "*" if ("**" in last or any(c in last for c in "[]{}\\")) else last
    lead = []
    if anchored:
        for s in segs[:-1]:
            if any(c in s for c in "*?[]{}\\"):
                break
            lead.append(s)
    return "/".join(lead), glob, dir_only


def excluded_by_rule(source_key: str, rules: list[str], index: int, limit: int = 500) -> dict:
    """Entries on disk under the source that rule ``index`` keeps out, evaluated with kopia's rule
    order (a later ``!`` rule can bring files back) and pruning at already-excluded folders."""
    from ..index.policypricing import _machine
    from . import paths as pathrules
    from .ignorematch import compile_pattern

    st = status()
    out = {"provider": st["provider"], "items": [], "total": 0, "bytes": 0, "capped": False,
           "note": st["note"]}
    if not st["available"]:
        return out
    pattern = rules[index]
    try:
        compiled = [compile_pattern(r) for r in rules]
    except ValueError as exc:
        out["note"] = f"This rule cannot be read: {exc}"
        return out
    if compiled[index].negated:
        out["note"] = "This rule brings files back into the backup rather than keeping them out."
        return out
    root = pathrules.source_root(source_key)
    sep = pathrules.sep(source_key)
    lead, glob, dir_only = _search_parts(pattern)
    base = root.rstrip(sep)
    search_root = base + sep + lead.replace("/", sep) if lead else root
    if not os.path.isdir(search_root):
        out["note"] = "Nothing at this location on disk."
        return out
    rows, complete = find(search_root, glob, folders_only=dir_only, limit=20000)
    memo: dict[str, bool] = {}
    fold = not pathrules.case_sensitive(source_key)

    def ignored_dir(rel: str) -> bool:
        if rel not in memo:
            memo[rel] = _machine(compiled, rel, True)[0]
        return memo[rel]

    hits = []
    prefix = (base + sep).lower() if fold else base + sep
    for r in rows:
        full = r["path"]
        if not (full.lower() if fold else full).startswith(prefix):
            continue
        rel = "/" + full[len(base) + 1:].replace(sep, "/")
        parts = rel.strip("/").split("/")
        if any(ignored_dir("/" + "/".join(parts[:i])) for i in range(1, len(parts))):
            continue
        ignored, who = _machine(compiled, rel, r["is_dir"])
        if ignored and who == index:
            hits.append(r)
    hits.sort(key=lambda h: -(h["size"] or 0))
    out.update(items=hits[:max(1, limit)], total=len(hits), bytes=sum(h["size"] or 0 for h in hits),
               capped=not complete or len(hits) > limit)
    if not complete:
        out["note"] = (out["note"] + " " if out["note"] else "") + "The search stopped early; there may be more."
    return out
