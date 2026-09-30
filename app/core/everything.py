"""Everything (`es.exe`) CLI wrapper — the local-disk side of the Excluded browser.

Local NTFS only (no UNC/network shares, no os.scandir fallback by design — `es.exe` is instant
and scandir across many big drives would be brutal). If `es.exe` returns nothing we surface that
rather than crawling the filesystem.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess

from ..core.applog import get_logger
from ..settings import load_settings

log = get_logger("everything")

_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _install_locations() -> list[str]:
    roots = {os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")}
    return [os.path.join(r, "Everything", "es.exe") for r in sorted(filter(None, roots))]


def es_path() -> str:
    """The configured es.exe, else one on PATH, else the Everything install folder, else "es"."""
    configured = load_settings().get("kopialens", {}).get("es_path")
    if configured:
        return configured
    if shutil.which("es"):
        return "es"
    for candidate in _install_locations():
        if os.path.isfile(candidate):
            return candidate
    return "es"


def _run(args: list[str]) -> str:
    try:
        r = subprocess.run(
            [es_path(), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, creationflags=_NOWINDOW,
        )
        if r.returncode != 0:
            log.warning("es.exe rc=%d args=%s stderr=%s", r.returncode, args[:2], (r.stderr or "").strip()[:200])
            return ""
        return r.stdout
    except Exception as exc:  # noqa: BLE001 - es missing / not running: surface empty, don't crash
        log.warning("es.exe failed: %s", exc)
        return ""


def available() -> bool:
    """Quick liveness probe: es.exe present AND Everything service reachable."""
    return bool(_run(["-n", "1", "-name", "*"]).strip())


def list_drives() -> list[dict]:
    """Local fixed drives only (psutil), each with used/total bytes. Network/removable excluded."""
    import psutil

    out: list[dict] = []
    for part in psutil.disk_partitions(all=False):
        if "fixed" in (part.opts or "").lower() and ":" in (part.mountpoint or ""):
            mp = part.mountpoint if part.mountpoint.endswith("\\") else part.mountpoint + "\\"
            try:
                u = psutil.disk_usage(mp)
                out.append({"path": mp, "used": int(u.used), "total": int(u.total)})
            except Exception:  # noqa: BLE001
                out.append({"path": mp, "used": 0, "total": 0})
    return sorted(out, key=lambda d: d["path"])


def _count_out(out: str) -> int:
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    try:
        return int(lines[-1]) if lines else 0
    except ValueError:
        return 0


def recursive_count(path: str) -> int:
    """Recursive FILE count under `path` - Everything-computed, instant (no enumeration)."""
    return _count_out(_run(["-path", path, "file:", "-get-result-count"]))


def recursive_size(path: str) -> int:
    """Recursive FILE byte total under `path` - Everything-computed, instant."""
    return _count_out(_run(["-path", path, "file:", "-get-total-size"]))


def find(root: str, name: str, folders_only: bool = False, limit: int = 20000) -> list[dict]:
    """Entries under ``root`` whose name matches ``name`` (a wildcard, or an exact whole name),
    biggest first: full path, size (folders: recursive), is_dir."""
    term = name  # a plain name matches as a substring; callers check the exact match
    args = ["-path", root, "-csv", "-full-path-and-name", "-size", "-attributes", "-size-format", "1",
            "-sort", "size-descending", "-n", str(max(1, int(limit)))]
    if folders_only:
        args.append("folder:")
    out = _run(args + [term])
    rows: list[dict] = []
    reader = csv.reader(io.StringIO(out))
    next(reader, None)  # header: Filename,Size,Attributes
    for rec in reader:
        if len(rec) < 3:
            continue
        rows.append({"path": rec[0], "size": int(rec[1]) if rec[1].isdigit() else 0,
                     "is_dir": "D" in (rec[2] or "")})
    return rows


def es_children(folder: str) -> list[dict]:
    """Direct children (files + folders) of `folder` from Everything: name, size (bytes), mtime,
    is_dir. Folders report their RECURSIVE size (Everything 1.5 folder-size index). One es call."""
    out = _run([
        "-parent", folder, "-csv", "-name", "-size", "-date-modified", "-attributes",
        "-size-format", "1", "-date-format", "1", "-n", "1000000",
    ])
    rows: list[dict] = []
    if not out.strip():
        return rows
    reader = csv.reader(io.StringIO(out))
    next(reader, None)  # header: Name,Size,Date Modified,Attributes
    for rec in reader:
        if len(rec) < 4:
            continue
        name, size, mtime, attrs = rec[0], rec[1], rec[2], rec[3]
        rows.append({
            "name": name,
            "size": int(size) if size.isdigit() else 0,
            "mtime": mtime,
            "is_dir": "D" in (attrs or ""),
        })
    return rows
