"""Central file-based logging + lightweight timing helpers.

KopiaLens runs headless under the tray (stdout/stderr go to DEVNULL), so all
diagnostics must land in a file. Two files under ``data/logs/``:

- ``kopialens.log`` - the app log, INFO and above: what the app did and what went wrong.
- ``kopialens-debug.log`` - everything including DEBUG, written only while the ``debug_log``
  setting (or env ``KOPIALENS_DEBUG_LOG``) is on. For troubleshooting, not for everyday reading.

``timed()`` is a context manager for ad-hoc span timing; request timing is wired as FastAPI
middleware in ``app.main`` (frequent polling requests are summarised by ``RequestStats``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from config import DATA_DIR

LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "kopialens.log")
DEBUG_LOG_FILE = os.path.join(LOG_DIR, "kopialens-debug.log")

_FORMAT = "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
_configured = False
_debug_handler: RotatingFileHandler | None = None
_handler_lock = threading.Lock()


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=15, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(level)
    root = logging.getLogger("kopialens")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def debug_log_wanted(settings: dict | None) -> bool:
    """Env ``KOPIALENS_DEBUG_LOG`` (1/0) beats the ``kopialens.debug_log`` setting."""
    env = os.environ.get("KOPIALENS_DEBUG_LOG")
    if env is not None and env.strip() != "":
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(((settings or {}).get("kopialens") or {}).get("debug_log", False))


def apply_logging_settings(settings: dict | None) -> None:
    """Attach or detach the debug log file to match the current settings. Safe to call on every
    settings save; the app log is untouched."""
    global _debug_handler
    root = logging.getLogger("kopialens")
    want = debug_log_wanted(settings)
    with _handler_lock:
        if want and _debug_handler is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            h = RotatingFileHandler(DEBUG_LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
            h.setFormatter(logging.Formatter(_FORMAT))
            h.setLevel(logging.DEBUG)
            root.addHandler(h)
            _debug_handler = h
        elif not want and _debug_handler is not None:
            root.removeHandler(_debug_handler)
            _debug_handler.close()
            _debug_handler = None
        # DEBUG records are only built while something will write them.
        root.setLevel(logging.DEBUG if _debug_handler is not None else logging.INFO)


class RequestStats:
    """Aggregates frequent requests (UI polling) into one summary line per interval instead of
    one line per request. ``add()`` returns the summary text when an interval has elapsed."""

    def __init__(self, interval: float = 900.0):
        self.interval = interval
        self._lock = threading.Lock()
        self._since = time.time()
        self._paths: dict[str, list[float]] = {}   # path -> [count, total_ms, max_ms]

    def add(self, path: str, ms: float, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        with self._lock:
            rec = self._paths.setdefault(path, [0, 0.0, 0.0])
            rec[0] += 1
            rec[1] += ms
            rec[2] = max(rec[2], ms)
            if now - self._since < self.interval:
                return None
            minutes = (now - self._since) / 60.0
            parts = [f"{p} {int(c)}x avg {t / c:.0f} ms max {m:.0f} ms"
                     for p, (c, t, m) in sorted(self._paths.items(), key=lambda kv: -kv[1][0])]
            total = sum(int(v[0]) for v in self._paths.values())
            self._paths = {}
            self._since = now
        return f"polling requests, last {minutes:.0f} min: {total} total - " + "; ".join(parts)


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
