"""Snapshot mounts started by KopiaLens.

The registry is saved to disk so mounts survive an app restart. On Windows kopia serves the
snapshot over WebDAV on 127.0.0.1 and maps it with ``net use``; it removes that mapping only on
Ctrl-C, which a background process never receives. Stopping the process therefore leaves the
drive letter behind, so unmount and cleanup remove our own mappings explicitly.

On Linux/macOS kopia mounts with FUSE into a folder under ``host.mount_dir()`` and unmounts on
SIGTERM. A process that died (a container restart) leaves a dead FUSE mount behind, so cleanup
unmounts our own folders lazily and removes them once empty.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time

from config import DATA_DIR

from . import filecrypto
from .applog import get_logger

log = get_logger("mounts")

_LOCK = threading.RLock()
_RECORDS: dict[int, dict] = {}     # pid -> {repo, snapshot_id, mountpoint, started, source, path, snap_time}
_PROCS: dict[int, subprocess.Popen] = {}
_LOADED = False
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _path() -> str:
    return os.path.join(DATA_DIR, "mounts.json")


def _save() -> None:
    if not filecrypto.write_json(_path(), list(_RECORDS.values())):
        log.warning("could not save the mount list")


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    data = filecrypto.read_json(_path(), [])
    if not isinstance(data, list):
        return
    for rec in data or []:
        if isinstance(rec, dict) and rec.get("pid"):
            _RECORDS.setdefault(int(rec["pid"]), rec)


def _alive(pid: int) -> bool:
    """True when ``pid`` is still a kopia mount process (PIDs are reused, so check the command)."""
    proc = _PROCS.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        import psutil
        p = psutil.Process(pid)
        cmd = " ".join(p.cmdline()).lower()
        return "kopia" in cmd and " mount " in f" {cmd} "
    except Exception:  # noqa: BLE001 - gone, or not ours
        return False


def _drive(mountpoint: str) -> str | None:
    m = re.fullmatch(r"([A-Za-z]):\\?", (mountpoint or "").strip())
    return m.group(1).upper() + ":" if m else None


def local_mappings() -> dict[str, str]:
    """Drive letter -> remote for ``net use`` mappings served from this machine (kopia's WebDAV)."""
    if os.name != "nt":
        return {}
    try:
        out = subprocess.run(["net", "use"], capture_output=True, text=True, timeout=15,
                             creationflags=_NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        return {}
    found = {}
    for line in out.splitlines():
        m = re.search(r"\b([A-Z]:)\s+(\S+)", line)
        if m and ("127.0.0.1" in m.group(2) or "localhost" in m.group(2).lower()):
            found[m.group(1)] = m.group(2)
    return found


def drop_mapping(mountpoint: str) -> bool:
    """Remove a leftover kopia drive mapping. Only a letter mapped to this machine is touched."""
    letter = _drive(mountpoint)
    if not letter or letter not in local_mappings():
        return False
    res = subprocess.run(["net", "use", letter, "/delete", "/y"], capture_output=True, text=True,
                         timeout=30, creationflags=_NO_WINDOW)
    if res.returncode == 0:
        log.info("removed drive mapping %s", letter)
        return True
    log.warning("could not remove drive mapping %s: %s", letter, (res.stderr or res.stdout).strip())
    return False


def _ours(mountpoint: str) -> bool:
    """A folder KopiaLens created under its mount folder (never anything else)."""
    from . import host
    root = host.mount_dir()
    if not root or not mountpoint:
        return False
    root = os.path.realpath(root)
    return os.path.dirname(os.path.realpath(mountpoint)) == root


def auto_mountpoint(repo: str, snapshot_id: str) -> str | None:
    """A drive letter on Windows; elsewhere a new folder name under the mount folder."""
    from . import host
    root = host.mount_dir()
    if root is None:
        from .kopia import next_free_drive
        return next_free_drive()
    os.makedirs(root, exist_ok=True)
    base = os.path.join(root, f"repo{repo}-{snapshot_id[:8]}")
    path, n = base, 1
    while os.path.lexists(path) and (os.path.ismount(path) or os.listdir(path)):
        n += 1
        path = f"{base}-{n}"
    return path


def release_folder(mountpoint: str) -> None:
    """POSIX: unmount a leftover mount in one of our folders, then remove the empty folder."""
    if os.name == "nt" or not _ours(mountpoint):
        return
    from . import host
    fm = host.fusermount()
    if sys.platform == "darwin":
        if os.path.ismount(mountpoint):
            subprocess.run(["/sbin/umount", "-f", mountpoint], capture_output=True, timeout=15)
    elif fm:
        # Lazy: a dead mount ("transport endpoint is not connected") or a busy one goes now.
        subprocess.run([fm, "-u", "-z", mountpoint], capture_output=True, timeout=15)
    try:
        os.rmdir(mountpoint)
    except OSError:
        pass   # not empty, or still mounted: leave it


def _cleanup(mountpoint: str) -> None:
    if os.name == "nt":
        drop_mapping(mountpoint)
    else:
        release_folder(mountpoint)


def _shown(rec: dict) -> dict:
    from . import host
    return {**rec, "shown": host.shown_path(rec.get("mountpoint", ""))}


def add(proc: subprocess.Popen, record: dict) -> None:
    with _LOCK:
        _load()
        rec = {**record, "pid": proc.pid, "started": time.time()}
        _RECORDS[proc.pid] = rec
        _PROCS[proc.pid] = proc
        _save()


def active() -> list[dict]:
    """Mounts whose process is still running. Dead ones are dropped and their mapping removed."""
    with _LOCK:
        _load()
        dead = [pid for pid in _RECORDS if not _alive(pid)]
        for pid in dead:
            rec = _RECORDS.pop(pid)
            _PROCS.pop(pid, None)
            try:
                _cleanup(rec.get("mountpoint", ""))
            except Exception as exc:  # noqa: BLE001
                log.debug("mount cleanup for %s failed: %s", rec.get("mountpoint"), exc)
        if dead:
            _save()
        return sorted((_shown(r) for r in _RECORDS.values()), key=lambda r: r.get("started", 0))


def unmount(pid: int) -> bool:
    with _LOCK:
        _load()
        rec = _RECORDS.get(pid)
        if not rec:
            return False
        proc = _PROCS.get(pid)
        try:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            elif _alive(pid):
                import psutil
                p = psutil.Process(pid)
                p.terminate()
                p.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001 - still try to clear the mapping
            log.warning("stopping mount pid=%s failed: %s", pid, exc)
        _cleanup(rec.get("mountpoint", ""))
        _RECORDS.pop(pid, None)
        _PROCS.pop(pid, None)
        _save()
    return True


def unmount_all() -> int:
    return sum(1 for r in active() if unmount(int(r["pid"])))


def get(pid: int) -> dict | None:
    with _LOCK:
        _load()
        rec = _RECORDS.get(pid)
        return _shown(rec) if rec else None


def startup_cleanup() -> None:
    """App start: forget mounts whose process died and remove the drive letters they left."""
    try:
        n = len(active())
        if n:
            log.info("%d mount(s) from before the restart are still running", n)
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("mount cleanup failed: %s", exc)
