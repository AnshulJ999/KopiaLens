"""Cross-process lock for config files.

The settings and record stores are rewritten with read-modify-write; a thread lock protects one
process, this protects against a second KopiaLens process (a test instance, a reset command)
writing the same file at the same moment. The lock lives in a sidecar ``<file>.lock``.
Stdlib only: ``config.py`` uses it while it is itself being imported.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("kopialens.filelock")

_HELD = threading.local()


def _try_lock(fh) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def locked(path: str, timeout: float = 10.0):
    """Hold the cross-process lock for ``path``. Re-entrant per thread. If the lock cannot be
    taken within ``timeout`` the write goes ahead with a warning: a stuck lock file must never
    stop settings from saving."""
    held = getattr(_HELD, "paths", None)
    if held is None:
        held = _HELD.paths = {}
    key = os.path.abspath(path)
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    fh = None
    acquired = False
    try:
        os.makedirs(os.path.dirname(key) or ".", exist_ok=True)
        fh = open(key + ".lock", "a+b")
        deadline = time.monotonic() + timeout
        while not (acquired := _try_lock(fh)) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not acquired:
            log.warning("could not lock %s within %.0fs; writing without the lock", key, timeout)
    except OSError as exc:
        log.warning("lock file for %s unavailable (%s); writing without the lock", key, exc)
    held[key] = 1
    try:
        yield
    finally:
        held.pop(key, None)
        if fh is not None:
            if acquired:
                _unlock(fh)
            fh.close()
