"""Disk lookup: the one way KopiaLens asks "what is on disk right now".

Providers: Everything (Windows, instant, must be running), a bounded folder scan (any platform,
slow on big trees, time-boxed), or off. Setting ``disk_lookup``: auto | everything | scan | off;
auto picks Everything when it answers, else the scan. Every answer that may be incomplete says so
(``None`` sizes, ``capped``) rather than guessing.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import sys
import threading
import time

from ..settings import load_settings
from .applog import get_logger

log = get_logger("disklookup")

MODES = ("auto", "everything", "scan", "off")
_STATUS_TTL = 30.0
_status_cache: tuple[float, dict] | None = None
_status_lock = threading.Lock()


def _kl() -> dict:
    try:
        return load_settings().get("kopialens", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def mode() -> str:
    m = str(_kl().get("disk_lookup", "auto")).lower()
    return m if m in MODES else "auto"


def scan_budget() -> float:
    try:
        return max(1.0, float(_kl().get("disk_scan_budget", 15)))
    except Exception:  # noqa: BLE001
        return 15.0


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
    elif m == "scan":
        provider, note = "scan", ""
    else:
        provider = "everything" if ev["running"] else "scan"
        note = "" if ev["running"] else "Folder scan in use: slower on big folders."
    out = {"mode": m, "provider": provider, "available": provider is not None, "everything": ev,
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
    """Direct children: name, size (folders recursive; None when the scan ran out of time), mtime, is_dir."""
    p = provider()
    if p == "everything":
        from . import everything
        return everything.es_children(folder)
    if p is None:
        return []
    out: list[dict] = []
    per_dir = scan_budget() / 4
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        is_dir = e.is_dir(follow_symlinks=False)
        size = _scan_total(e.path, per_dir)[0] if is_dir else st.st_size
        out.append({"name": e.name, "size": size, "is_dir": is_dir,
                    "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))})
    return out


def recursive_size(path: str) -> int | None:
    p = provider()
    if p == "everything":
        from . import everything
        return everything.recursive_size(path)
    return _scan_total(path, scan_budget())[0] if p == "scan" else None


def recursive_count(path: str) -> int | None:
    p = provider()
    if p == "everything":
        from . import everything
        return everything.recursive_count(path)
    return _scan_total(path, scan_budget())[1] if p == "scan" else None


def list_drives() -> list[dict]:
    """Local fixed drives (Windows) or mounted filesystems, with used/total bytes."""
    import psutil
    out: list[dict] = []
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint or ""
        if sys.platform == "win32":
            if "fixed" not in (part.opts or "").lower() or ":" not in mp:
                continue
            mp = mp if mp.endswith("\\") else mp + "\\"
        try:
            u = psutil.disk_usage(mp)
            out.append({"path": mp, "used": int(u.used), "total": int(u.total)})
        except Exception:  # noqa: BLE001
            out.append({"path": mp, "used": 0, "total": 0})
    return sorted(out, key=lambda d: d["path"])


def find(root: str, name: str, folders_only: bool = False, limit: int = 20000) -> tuple[list[dict], bool]:
    """Entries under ``root`` whose name matches ``name``; (rows, complete)."""
    p = provider()
    if p == "everything":
        from . import everything
        rows = everything.find(root, name, folders_only, limit)
        return rows, len(rows) < limit
    if p is None:
        return [], False
    rows: list[dict] = []
    fold = sys.platform == "win32"
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
