"""Disk lookup: the one way KopiaLens asks "what is on disk right now".

Providers: Everything (Windows, instant, must be running), the KopiaLens scanner (``kopialens-scan``,
any platform, parallel, time-boxed; a plain Python walk when the program is missing), or off.
Setting ``disk_lookup``: auto | everything | scan | off; auto picks Everything when it answers,
else the scanner. Every answer that may be incomplete says so (``None`` sizes, ``capped``) rather
than guessing; a folder that ran out of time is counted in the background for the next look.
"""

from __future__ import annotations

import collections
import contextvars
import fnmatch
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

MODES = ("auto", "everything", "scan", "off")
SCANNER_NAME = "kopialens-scan" + (".exe" if sys.platform == "win32" else "")
_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_STATUS_TTL = 30.0
_status_cache: tuple[float, dict] | None = None
_status_lock = threading.Lock()
# Set for the length of one API request about a demo repo (main.demo_repo_disk_middleware): a
# demo repo inside a normal install has fake paths that may exist here, so it never reads the disk.
_DEMO_REQUEST = contextvars.ContextVar("disklookup_demo_request", default=False)


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
    m = str(_kl().get("disk_lookup", "auto")).lower()
    return m if m in MODES else "auto"


def scan_budget() -> float:
    try:
        return max(1.0, float(_kl().get("disk_scan_budget", 15)))
    except Exception:  # noqa: BLE001
        return 15.0


def _threads() -> int:
    try:
        n = int(_kl().get("disk_scan_threads", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    return max(0, min(n, 64))


def _cache_ttl() -> float:
    try:
        return max(0.0, float(_kl().get("disk_cache_minutes", 5))) * 60
    except (TypeError, ValueError):
        return 300.0


def _background_on() -> bool:
    return _kl().get("disk_scan_background", True) is not False


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
        log.info("disk scanner: %s", _scanner[0] or "not found, using the Python folder walk")
    return _scanner[0]


def _skips() -> list[str]:
    return [] if sys.platform == "win32" else list(_SYSTEM_ROOTS)


def _run_scanner(args: list[str], budget: float, threads: int | None = None) -> list[dict] | None:
    """Runs the scanner with a time limit; its JSON lines, or None when it could not run."""
    exe = scanner_path()
    if not exe:
        return None
    cmd = [exe, *args, "--time-ms", str(int(budget * 1000))]
    n = _threads() if threads is None else threads
    if n:
        cmd += ["--threads", str(n)]
    for s in _skips():
        cmd += ["--skip", s]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=budget + 30, stdin=subprocess.DEVNULL, creationflags=_NOWINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("disk scanner failed (%s): %s", args[:2], exc)
        return None
    if r.returncode != 0:
        log.warning("disk scanner rc=%d (%s): %s", r.returncode, args[:2], (r.stderr or "").strip()[:200])
        return None
    rows = []
    for line in r.stdout.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows if rows and rows[-1].get("end") else None


# ---- folder totals cache (path -> size, files), complete answers only ----------------------------

_TOTALS: collections.OrderedDict[str, tuple[float, int, int]] = collections.OrderedDict()
_TOTALS_MAX = 50000
_TOTALS_LOCK = threading.Lock()


def _ckey(path: str) -> str:
    p = path.rstrip("\\/") or path
    return p.lower() if sys.platform == "win32" else p


def _cached(path: str) -> tuple[int, int] | None:
    ttl = _cache_ttl()
    with _TOTALS_LOCK:
        hit = _TOTALS.get(_ckey(path))
        if not hit or time.time() - hit[0] > ttl:
            return None
        _TOTALS.move_to_end(_ckey(path))
        return hit[1], hit[2]


def _remember(path: str, size: int, files: int) -> None:
    with _TOTALS_LOCK:
        _TOTALS[_ckey(path)] = (time.time(), size, files)
        _TOTALS.move_to_end(_ckey(path))
        while len(_TOTALS) > _TOTALS_MAX:
            _TOTALS.popitem(last=False)


def clear_cache() -> None:
    with _TOTALS_LOCK:
        _TOTALS.clear()


def _scan_totals(path: str, budget: float, depth: int = 2, threads: int | None = None) -> tuple[int, int] | None:
    """Totals for ``path`` (None when it ran out of time), remembering every finished folder up to
    ``depth`` levels below it."""
    rows = _run_scanner(["totals", path, "--depth", str(depth)], budget, threads)
    if rows is None:
        return None
    root = None
    for r in rows[:-1]:
        if not r.get("complete"):
            continue
        rel = r.get("rel") or []
        full = os.path.join(path, *rel) if rel else path
        _remember(full, int(r["size"]), int(r["files"]))
        if not rel:
            root = (int(r["size"]), int(r["files"]))
    return root


# ---- background counting of folders that ran out of time ------------------------------------------

_BG_QUEUE: collections.deque[str] = collections.deque()
_BG_LOCK = threading.Lock()
_BG_RUNNING: list[str] = []
_BG_ACTIVE = False
_BG_BUDGET = 1800.0


def _bg_worker() -> None:
    global _BG_ACTIVE
    while True:
        with _BG_LOCK:
            if not _BG_QUEUE:
                _BG_RUNNING.clear()
                _BG_ACTIVE = False
                return
            path = _BG_QUEUE.popleft()
            _BG_RUNNING[:] = [path]
        if _cached(path) is None:
            n = _threads() or max(1, (os.cpu_count() or 4) // 2)
            t0 = time.time()
            got = _scan_totals(path, _BG_BUDGET, threads=min(n, 4))
            log.info("background disk count %s: %s in %.1f s", path, "done" if got else "unfinished", time.time() - t0)


def _count_later(path: str) -> None:
    """Counts ``path`` in the background (one folder at a time), so the next look has it."""
    global _BG_ACTIVE
    if not _background_on() or not scanner_path():
        return
    with _BG_LOCK:
        if path in _BG_QUEUE or path in _BG_RUNNING:
            return
        _BG_QUEUE.append(path)
        if _BG_ACTIVE:
            return
        _BG_ACTIVE = True
    threading.Thread(target=_bg_worker, name="disk-count", daemon=True).start()


def counting() -> int:
    """Folders waiting for or in a background count."""
    with _BG_LOCK:
        return len(_BG_QUEUE) + len(_BG_RUNNING)


def everything_state() -> dict:
    """Is Everything installed (es.exe found) and answering?"""
    from . import everything
    exe = everything.es_path()
    found = os.path.isfile(exe) or bool(shutil.which(exe))
    running = bool(found) and everything.available()
    return {"supported": sys.platform == "win32", "installed": found, "running": running, "es_path": exe}


def status(refresh: bool = False) -> dict:
    """Which provider answers, and why. Cached briefly: the Everything probe is a subprocess."""
    global _status_cache
    if _DEMO_REQUEST.get():   # never cached: the shared cache answers for the real repos
        return {"mode": "off", "provider": None, "available": False,
                "everything": {"supported": sys.platform == "win32", "installed": None, "running": None},
                "note": "Sample data isn't on this computer's disk.", "scan_budget": scan_budget()}
    with _status_lock:
        if not refresh and _status_cache and time.time() - _status_cache[0] < _STATUS_TTL:
            return _status_cache[1]
    m = mode()
    ev = everything_state() if m in ("auto", "everything") else {"supported": sys.platform == "win32",
                                                                   "installed": None, "running": None}
    if m == "off":
        provider, note = None, "Disk lookup is turned off in Settings."
    elif m == "everything":
        provider = "everything" if ev["running"] else None
        note = "" if provider else ("Everything is not running." if ev["installed"]
                                    else "Everything is not installed.")
    else:
        provider = "everything" if (m == "auto" and ev["running"]) else "scan"
        note = "" if provider == "everything" or scanner_path() else "Basic folder scan in use: slower on big folders."
    out = {"mode": m, "provider": provider, "available": provider is not None, "everything": ev,
           "scanner": {"found": bool(scanner_path())}, "counting": counting(),
           "note": note, "scan_budget": scan_budget()}
    with _status_lock:
        _status_cache = (time.time(), out)
    return out


def provider() -> str | None:
    return status()["provider"]


# ---- folder scan ------------------------------------------------------------------------------

def _walk(root: str, deadline: float):
    """os.walk with a deadline; yields (dirpath, dirnames, filenames) and stops when time is up."""
    for entry in os.walk(root, onerror=lambda _e: None):
        yield entry
        if time.time() > deadline:
            raise TimeoutError


def _scan_total(path: str, budget: float) -> tuple[int | None, int | None]:
    deadline = time.time() + budget
    size = count = 0
    try:
        for dirpath, _dirs, files in _walk(path, deadline):
            for f in files:
                try:
                    size += os.stat(os.path.join(dirpath, f)).st_size
                    count += 1
                except OSError:
                    pass
    except TimeoutError:
        return None, None
    return size, count


# ---- the questions features ask ---------------------------------------------------------------

def children(folder: str) -> list[dict]:
    """Direct children: name, size (folders recursive; None while not counted yet), mtime, is_dir.
    Folder totals come from the scanner (two levels at a time, so opening a child is instant)."""
    p = provider()
    if p == "everything":
        from . import everything
        return everything.es_children(folder)
    if p is None:
        return []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    rows = []
    for e in entries:
        try:
            if e.is_symlink():
                continue   # never followed, like kopia, which stores the link itself
            st = e.stat(follow_symlinks=False)
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            continue
        rows.append((e, st, is_dir))
    scanner = bool(scanner_path())
    if scanner and any(d and _cached(e.path) is None for e, _st, d in rows):
        _scan_totals(folder, scan_budget())
    out: list[dict] = []
    per_dir = scan_budget() / 4
    for e, st, is_dir in rows:
        if is_dir and scanner:
            hit = _cached(e.path)
            size = hit[0] if hit else None
            if hit is None:
                _count_later(e.path)
        else:
            size = _scan_total(e.path, per_dir)[0] if is_dir else st.st_size
        out.append({"name": e.name, "size": size, "is_dir": is_dir,
                    "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))})
    return out


def _totals(path: str) -> tuple[int | None, int | None]:
    hit = _cached(path)
    if hit:
        return hit
    if scanner_path():
        with _BG_LOCK:
            if path in _BG_QUEUE or path in _BG_RUNNING:
                return None, None   # already being counted in the background
        got = _scan_totals(path, scan_budget())
        if got is None:
            _count_later(path)
            return None, None
        return got
    return _scan_total(path, scan_budget())


def quick_totals(path: str) -> tuple[int | None, int | None]:
    """(size, files) without making the caller wait on a scan: Everything's answer, a remembered
    count, or None while the folder is counted in the background."""
    p = provider()
    if p == "everything":
        return recursive_size(path), recursive_count(path)
    if p != "scan":
        return None, None
    hit = _cached(path)
    if hit:
        return hit
    if scanner_path() and _background_on():
        _count_later(path)
        return None, None
    return _totals(path)


def recursive_size(path: str) -> int | None:
    p = provider()
    if p == "everything":
        from . import everything
        return everything.recursive_size(path)
    return _totals(path)[0] if p == "scan" else None


def recursive_count(path: str) -> int | None:
    p = provider()
    if p == "everything":
        from . import everything
        return everything.recursive_count(path)
    return _totals(path)[1] if p == "scan" else None


_SYSTEM_ROOTS = ("/proc", "/sys", "/dev", "/run", "/etc", "/boot", "/snap", "/var/lib/docker")


def _user_folder(mp: str) -> bool:
    """Linux/macOS: a mounted folder a user could back up - not a single file Docker mounts in
    (/etc/hosts), not system plumbing, not KopiaLens's own data folder."""
    from config import DATA_DIR
    if not os.path.isdir(mp) or any(mp == r or mp.startswith(r + "/") for r in _SYSTEM_ROOTS):
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


def find(root: str, name: str, folders_only: bool = False, limit: int = 20000) -> tuple[list[dict], bool]:
    """Entries under ``root`` whose name matches ``name``; (rows, complete)."""
    p = provider()
    if p == "everything":
        from . import everything
        rows = everything.find(root, name, folders_only, limit)
        return rows, len(rows) < limit
    if p is None:
        return [], False
    fold = sys.platform == "win32"
    args = ["find", root, name, "--limit", str(max(1, int(limit)))]
    if folders_only:
        args.append("--dirs")
    if fold:
        args.append("--fold")
    found = _run_scanner(args, scan_budget())
    if found is not None:
        rows = [{"path": r["path"], "size": r.get("size"), "is_dir": bool(r.get("dir"))} for r in found[:-1]]
        return rows, bool(found[-1].get("complete"))
    rows: list[dict] = []
    pat = name.lower() if fold else name
    deadline = time.time() + scan_budget()
    try:
        for dirpath, dirs, files in _walk(root, deadline):
            names = [(d, True) for d in dirs] + ([] if folders_only else [(f, False) for f in files])
            for n, is_dir in names:
                if fnmatch.fnmatchcase(n.lower() if fold else n, pat):
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
