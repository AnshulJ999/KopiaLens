"""The final swap of an atomic save, made to survive a briefly busy file on Windows.

On Windows ``os.replace`` fails while any other process has the target (or the temp file) open:
an antivirus scan of a fresh file, a backup or sync tool, an editor, or another KopiaLens process
reading it. Those holds last milliseconds to seconds, so the swap is retried for a few seconds.
Elsewhere the swap never fails this way and runs once.
"""
from __future__ import annotations

import logging
import os
import sys
import time

log = logging.getLogger("kopialens.atomicfile")

_BUSY = {5, 32, 33}                                   # access denied, sharing and lock violations
_WAITS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0, 1.0)   # about 5.5 s in all


def replace(src, dst, *, quiet: bool = False) -> None:
    """``os.replace(src, dst)``, retried on Windows while the file is busy.

    If it stays busy the original error is raised and ``dst`` is left as it was.
    ``quiet`` skips logging, for callers that must not log here (trap 39)."""
    if sys.platform != "win32":
        os.replace(src, dst)
        return
    waited = 0.0
    for wait in (*_WAITS, None):
        try:
            os.replace(src, dst)
        except PermissionError as e:
            if getattr(e, "winerror", None) not in _BUSY:
                raise
            if wait is None:
                if not quiet:
                    log.warning("Couldn't save %s: another program kept it open for %.1f s", dst, waited)
                raise
            time.sleep(wait)
            waited += wait
            continue
        if waited and not quiet:
            log.warning("Saved %s after waiting %.1f s for another program to let go of it", dst, waited)
        return
