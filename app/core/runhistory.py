"""Per-run index history (): one JSONL line per heavy pass, written by the APP.

The worker's own stdout summary dies with its process, and the rolling worker log is prose -
answering "how did the last three nightlies go" meant reading logs. ``nightly.run_index_pass``
sees every outcome in one place (the parsed worker summary, a timeout, a launch failure), so it
records here: the app is the single writer per file, and killed-by-timeout runs are recorded by
the same hand that killed them - no run silently disappears the way a worker-written record would.

One file per repo under ``data/runs/``; append-only like the audit trail (a few KB per run).
Best-effort: a history gap must never fail the pass it describes.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time

from config import DATA_DIR

from . import filecrypto
from .applog import get_logger

log = get_logger("runhistory")

_LOCK = threading.Lock()

# Fields copied from the worker summary onto every record (absent on the app-side shapes).
_SUMMARY_FIELDS = ("stages", "errors", "skipped", "analysis_failed_sources",
                   "refcount_misses", "unreadable_dirs", "warnings")


def _runs_dir() -> str:
    return os.path.join(DATA_DIR, "runs")


def _path(repo: str) -> str:
    return os.path.join(_runs_dir(), f"repo{repo}.jsonl")


def record(repo: str, summary: dict, trigger: str, full: bool, elapsed: float) -> None:
    """Append one line for a finished pass. ``summary`` is the run_index_pass result (which
    embeds the worker's stage timings/errors when it produced one)."""
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        # same-second records are ambiguous on the ISO stamp alone - this orders them
        "ts_epoch": time.time(),
        "repo": repo, "trigger": trigger, "full": bool(full),
        "ok": bool(summary.get("ok", True)),
        "reason": summary.get("reason"),
        "elapsed": round(elapsed, 1),
    }
    for field in _SUMMARY_FIELDS:
        if summary.get(field) is not None:
            rec[field] = summary[field]
    if summary.get("log"):
        rec["log"] = summary["log"]
    try:
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            if not filecrypto.append_line(_path(repo), line):
                log.warning("run-history write failed repo=%s", repo)
    except Exception as exc:  # noqa: BLE001 - history must never break the pass
        log.warning("run-history write failed repo=%s: %s", repo, exc)


def read_tail(limit: int = 100, repos: list[str] | None = None) -> list[dict]:
    """Newest-first merged tail across the given repos (default: every file present)."""
    import glob
    paths = ([_path(r) for r in repos] if repos
             else sorted(glob.glob(os.path.join(_runs_dir(), "repo*.jsonl"))))
    out: list[dict] = []
    for path in paths:
        try:
            with _LOCK:
                lines = filecrypto.tail_lines(path, max(1, limit))
            for line in lines:
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001 - one damaged line must not hide the rest
                    out.append({"ts": None, "repo": "?", "ok": False, "reason": "corrupt record"})
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts_epoch") or 0, reverse=True)
    return out[:max(1, limit)]
