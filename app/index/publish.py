"""M1 writer: atomic generation publication.

Moves artifacts.py's staged files into ``pool/`` under generation-stamped names, then atomically
swaps ``manifest.json`` (tmp + fsync + ``os.replace`` - the single switch a reader trusts), then
GCs pool files no live generation references. The manifest is the only publication mechanism; a
reader never lists, globs, or otherwise infers the generation from the directory.
"""

from __future__ import annotations

import json
import os
import time

from ..core.applog import get_logger
from .store import repo_dir

log = get_logger("store")

_GC_GRACE_S = 3600


def _public(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if not k.startswith("_")}


def _previous_artifacts(root: str) -> dict[str, list[str]]:
    try:
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh).get("artifacts", {})
    except Exception:
        return {}


def move_into_pool(root: str, moves: list[tuple[str, str]]) -> None:
    """os.replace each staged file into its final pool/ path (same volume, atomic per file).
    Names are generation-stamped, so this never overwrites a file a reader can be holding."""
    for staged, rel in moves:
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(staged, dst)


def write_manifest(root: str, manifest: dict) -> None:
    """The atomic switch. tmp + fsync + os.replace, with the proven gc.collect()+retry loop for
    the WinError 32 a reader holding the old manifest can raise at that instant."""
    path = os.path.join(root, "manifest.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_public(manifest), fh)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 11:
                raise
            import gc
            gc.collect()
            time.sleep(0.3)


def _gc(root: str, current: dict[str, list[str]], previous: dict[str, list[str]]) -> None:
    keep: set[str] = set()
    for rels in list(current.values()) + list(previous.values()):
        keep.update(rels)
    pool = os.path.join(root, "pool")
    if not os.path.isdir(pool):
        return
    now = time.time()
    for dirpath, _dirs, files in os.walk(pool):
        for fn in files:
            if not fn.endswith(".parquet"):
                continue
            abs_ = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_, root).replace("\\", "/")
            if rel in keep or fn.startswith("."):
                continue
            try:
                if now - os.path.getmtime(abs_) < _GC_GRACE_S:
                    continue
                os.remove(abs_)
            except Exception:  # noqa: BLE001 - a reader-held file is deleted next cycle
                pass


def publish(root: str, manifest: dict) -> None:
    """Move staged files into pool/ then swap the manifest, then GC. The previous manifest is
    captured BEFORE the swap so GC can keep current + previous generations."""
    previous = _previous_artifacts(root)
    move_into_pool(root, manifest.get("_moves", []))
    write_manifest(root, manifest)
    _gc(root, manifest.get("artifacts", {}), previous)
    log.info("published generation %s -> %s", manifest.get("generation"), root)


class WriterLock:
    """One-process-at-a-time publication via an O_CREAT|O_EXCL lock file (PID inside, stale
    detection by process check - the technique reap_orphan_workers already uses)."""

    def __init__(self, repo: str):
        self.path = os.path.join(repo_dir(repo), ".writer")

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._stale():
                try:
                    os.remove(self.path)
                except Exception:  # noqa: BLE001
                    pass
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise RuntimeError(f"another writer holds {self.path}")
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))

    def _stale(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as fh:
                pid = int(fh.read().strip() or 0)
        except Exception:
            return True
        if not pid:
            return True
        try:
            import psutil
            return not psutil.pid_exists(pid)
        except Exception:  # noqa: BLE001
            return False

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except Exception:  # noqa: BLE001
            pass