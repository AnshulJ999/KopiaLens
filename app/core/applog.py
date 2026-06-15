"""Central file-based logging + lightweight timing helpers.

KopiaLens runs headless under the tray (stdout/stderr go to DEVNULL), so all
diagnostics must land in a file. Logs rotate at ``data/logs/kopialens.log``.
``timed()`` is a context manager for ad-hoc span timing; request timing is wired
as FastAPI middleware in ``app.main``.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from config import DATA_DIR

LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "kopialens.log")

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s"))
    root = logging.getLogger("kopialens")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"kopialens.{name}")


@contextmanager
def timed(logger: logging.Logger, label: str, min_ms: float = 0.0):
    """Log how long a block took (only if it exceeds ``min_ms``)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - start) * 1000
        if ms >= min_ms:
            logger.info("%s took %.0f ms", label, ms)
