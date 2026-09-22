"""Central file-based logging + lightweight timing helpers.

KopiaLens runs headless under the tray (stdout/stderr go to DEVNULL), so all
diagnostics must land in a file. ``data/logs`` holds three groups, so the app log is not buried
under the noisy ones:

- ``app/``       ``kopialens.log`` (INFO and above) + its old copies, ``kopialens-debug.log``
                 (everything at DEBUG while the ``debug_log`` setting is on), and any leftover
                 ``kopia-server-*.log``.
- ``indexing/``  one ``index_worker_repoN.log`` per repo, size-rolled.
- ``jobs/``      job logs, dry runs and mount logs - the files that accumulate per action.

Legacy flat files are moved into these folders once at startup (:func:`migrate`); ``jobs/`` is
pruned by the ``log_cleanup_days`` setting, always keeping the newest hundred.

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
APP_LOG_DIR = os.path.join(LOG_DIR, "app")
INDEX_LOG_DIR = os.path.join(LOG_DIR, "indexing")
JOB_LOG_DIR = os.path.join(LOG_DIR, "jobs")
LOG_FILE = os.path.join(APP_LOG_DIR, "kopialens.log")
DEBUG_LOG_FILE = os.path.join(APP_LOG_DIR, "kopialens-debug.log")

KEEP_NEWEST_LOGS = 100          # jobs/ always keeps this many, whatever the age setting says

_FORMAT = "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
_configured = False
_debug_handler: RotatingFileHandler | None = None
_handler_lock = threading.Lock()


def ensure_dirs() -> None:
    for d in (LOG_DIR, APP_LOG_DIR, INDEX_LOG_DIR, JOB_LOG_DIR):
        os.makedirs(d, exist_ok=True)


def _dest_for(name: str) -> str | None:
    low = name.lower()
    if low.startswith(("kopialens.log", "kopialens-debug.log", "kopia-server-repo")):
        return APP_LOG_DIR
    if low.startswith("index_worker_repo"):
        return INDEX_LOG_DIR
    if low.startswith(("job_", "dry_run_", "mount_")):
        return JOB_LOG_DIR
    return None


def migrate() -> int:
    """Move legacy flat ``data/logs/*`` files into app/, indexing/ and jobs/ once. Idempotent;
    never clobbers a file already in place. Returns how many moved."""
    ensure_dirs()
    try:
        names = [e.name for e in os.scandir(LOG_DIR) if e.is_file()]
    except OSError:
        return 0
    moved = 0
    for name in names:
        dest_dir = _dest_for(name)
        if not dest_dir:
            continue
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            continue
        try:
            os.replace(os.path.join(LOG_DIR, name), dest)
        except OSError as exc:  # noqa: BLE001 - a locked file must not stop the others
            logging.getLogger("kopialens.applog").warning("could not move legacy log %s: %s",
                                                           name, exc)
            continue
        moved += 1
    return moved


def cleanup_jobs(days: float | None = None, *, now: float | None = None) -> int:
    """Remove job/dry-run/mount logs older than ``days`` from jobs/ (0 or None from the setting
    means OFF), always keeping the newest ``KEEP_NEWEST_LOGS``. Returns how many were removed."""
    if days is None:
        try:
            from ..settings import load_settings
            days = float(((load_settings().get("kopialens") or {}).get("log_cleanup_days") or 0))
        except Exception:  # noqa: BLE001 - a settings read must never break a cleanup tick
            return 0
    if not days or days <= 0:
        return 0
    try:
        entries = [e for e in os.scandir(JOB_LOG_DIR) if e.is_file()]
    except OSError:
        return 0
    try:
        entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = 0
    for entry in entries[KEEP_NEWEST_LOGS:]:
        try:
            if entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
        except OSError:
            continue
    if removed:
        logging.getLogger("kopialens.applog").info(
            "logs: removed %d job log(s) older than %.0f day(s)", removed, days)
    return removed


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    moved = migrate()
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=15, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(level)
    root = logging.getLogger("kopialens")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    _configured = True
    if moved:
        root.info("logs: moved %d legacy log file(s) into app/, indexing/ and jobs/", moved)


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
            ensure_dirs()
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
