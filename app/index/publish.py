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
        # Normalize both directions: the writer emits forward slashes, but normalizing the
        # manifest side too means a path that ever lands in a manifest via os.path.join
        # (backslashes on Windows) still matches its own protection instead of being GC'd.
        keep.update(r.replace("\\", "/") for r in rels)
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
    log.info("published generation %s", manifest.get("generation"))
    log.debug("published into %s", root)


def replace_artifacts(repo: str, tables: dict) -> int | None:
    """Publish a new generation with `tables` replaced and every other artifact carried forward.

    For the small, separately-computed artifacts (snapshot analysis, the content refcount) that are
    produced by a later worker stage rather than by the index build. Carrying the rest forward by
    reference is what makes this cheap: only the changed file is written.

    ``tables`` maps artifact name -> pyarrow.Table. Returns the new generation number, or None if
    another writer holds the repo (the caller's value is a cache and can be recomputed).
    """
    import pyarrow as pa  # noqa: F401 - the caller's tables are pyarrow

    from . import crypto, store

    root = store.repo_dir(repo)
    try:
        lock = WriterLock(repo)
        lock.__enter__()
    except RuntimeError:
        log.info("repo=%s replace_artifacts skipped, another writer holds the repo", repo)
        return None
    try:
        # Read the manifest while HOLDING the lock. Reading it first would let a publication land
        # between the read and the lock, and this function would then republish the stale
        # artifact lists - overwriting a newer index generation wholesale.
        manifest = store.read_manifest(repo)
        if manifest is None:
            return None
        encrypted = bool(manifest.get("encrypted"))
        gen = int(manifest.get("generation") or 0) + 1
        staging = os.path.join(root, "_staging")
        os.makedirs(staging, exist_ok=True)
        moves, artifacts = [], dict(manifest.get("artifacts", {}))
        with store.scratch_connection(repo) as con:
            for name, table in tables.items():
                staged = os.path.join(staging, f"{name}_g{gen}.parquet")
                con.register("_repl", table)
                con.execute("COPY (SELECT * FROM _repl) TO '%s' (%s)"
                            % (staged.replace("\\", "/").replace("'", "''"),
                               crypto.copy_options(encrypted=encrypted)))
                con.unregister("_repl")
                rel = f"pool/{name}/g{gen}.parquet"
                moves.append((staged, rel))
                artifacts[name] = [rel]
        new = dict(manifest)
        new["generation"] = gen
        new["artifacts"] = artifacts
        new["_moves"] = moves
        publish(root, new)
        return gen
    finally:
        lock.__exit__(None, None, None)


def _owner_stamp() -> str:
    """``<pid>:<process start time>`` - see WriterLock._stale for why the start time is needed."""
    try:
        import psutil
        return f"{os.getpid()}:{psutil.Process(os.getpid()).create_time()}"
    except Exception:  # noqa: BLE001 - psutil absent: pid alone, and _stale degrades gracefully
        return str(os.getpid())


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
            fh.write(_owner_stamp())

    def _stale(self) -> bool:
        """A lock left by a process that is gone.

        PIDs recycle, so `pid_exists` alone eventually reports a dead owner as alive and the repo
        stays locked forever. The stamp carries the process start time too: same pid AND same start
        time means it really is the same process. (The orphan-worker reaper hit this same trap and
        solved it by matching the command line.)
        """
        try:
            with open(self.path, encoding="utf-8") as fh:
                pid_s, _, started_s = fh.read().strip().partition(":")
            pid = int(pid_s or 0)
        except Exception:  # noqa: BLE001 - unreadable lock is a stale lock
            return True
        if not pid:
            return True
        try:
            import psutil
            if not started_s:                     # no start time: cannot prove ownership, so stale
                return True
            return abs(psutil.Process(pid).create_time() - float(started_s)) > 1.0
        except Exception:  # noqa: BLE001 - NoSuchProcess, or psutil missing entirely
            return True

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except Exception:  # noqa: BLE001
            pass