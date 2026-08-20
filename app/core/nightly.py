"""The one place a heavy index pass is started, for every repo kind.

Both the nightly scheduler and the UI's "Index" button come through here, so there is a single
answer to "is this repo being rebuilt right now" and a single implementation of the handover:

  kopia repo -> take the DuckDB offline for this process, run ``app.index.worker`` as a subprocess,
                reopen, then compact if it has grown enough
  backblaze  -> its own long-standing subprocess refresh (``indexer.refresh_via_worker``)

Everything expensive runs in a child process that exits afterwards, which is what actually returns
the memory to Windows - CPython and DuckDB both hold on to large freed heaps otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from config import DATA_DIR, REPOS, repo_kind

from .applog import get_logger
from ..settings import load_settings

log = get_logger("nightly")

_KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_TIMEOUT = 4 * 3600  # a full rebuild of the big repo is tens of minutes; leave real headroom

_RUN_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_STATE: dict[str, dict] = {}  # repo -> {"running": bool, "started": ts, "last": {...}, "error": str|None}


def _lock(repo: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(repo, threading.Lock())


def is_running(repo: str) -> bool:
    return bool(_STATE.get(repo, {}).get("running"))


def any_running() -> bool:
    """True while ANY repo's heavy pass is in flight. The scheduler uses this to keep passes
    strictly sequential - two workers at once would double the memory and thrash the same disks,
    which is the whole thing this design exists to avoid."""
    return any(s.get("running") for s in _STATE.values())


def state(repo: str) -> dict:
    return dict(_STATE.get(repo, {}))


def worker_timeout() -> float:
    try:
        return float(load_settings().get("kopialens", {}).get("index_worker_timeout", _DEFAULT_TIMEOUT))
    except Exception:
        return _DEFAULT_TIMEOUT


def worker_progress(repo: str) -> dict | None:
    """The subprocess's live progress file, if it is mid-run. The app cannot query the DuckDB while
    the worker holds it, so this is the only way to render a progress badge during the window."""
    try:
        with open(os.path.join(DATA_DIR, f"index_progress_repo{repo}.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _run_worker(repo: str, full: bool) -> dict:
    args = [sys.executable, "-m", "app.index.worker", repo]
    if full:
        args.append("--full")
    try:
        res = subprocess.run(
            args, cwd=_KL_ROOT, capture_output=True, text=True, timeout=worker_timeout(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash under pythonw
        )
    except subprocess.TimeoutExpired:
        log.error("repo=%s index worker timed out", repo)
        return {"ok": False, "reason": "timeout"}
    if res.returncode != 0:
        tail = (res.stderr or "")[-2000:]
        log.error("repo=%s index worker exit %d: %s", repo, res.returncode, tail)
        return {"ok": False, "reason": f"worker exit {res.returncode}", "stderr": tail}
    try:
        return json.loads((res.stdout or "").strip())
    except Exception as exc:  # noqa: BLE001 - malformed output must not crash the scheduler
        log.error("repo=%s index worker output unparsable: %s", repo, exc)
        return {"ok": False, "reason": f"bad worker output: {exc}"}


def maybe_compact(repo: str) -> None:
    """Compact the DuckDB when it has grown past ``compact_ratio`` x its last compacted size and
    the min interval has elapsed. DuckDB never reclaims dead space on its own, so this is its GC.
    Runs right after the worker's write burst, which is when the file is at its most bloated."""
    kl = load_settings().get("kopialens", {})
    if not kl.get("auto_compact", True):
        return
    from ..index.schema import compact_in_place, compact_state, db_path, record_compact_size

    live = db_path(repo)
    if not os.path.exists(live):
        return
    cur = os.path.getsize(live)
    st = compact_state().get(repo, {})
    base = st.get("size")
    if not base:
        record_compact_size(repo, cur)  # establish a baseline; never compact a just-rebuilt DB
        return
    ratio = float(kl.get("compact_ratio", 1.8))
    interval = float(kl.get("compact_min_interval", 86400))
    if time.time() - st.get("ts", 0) < interval or cur < base * ratio:
        return
    log.info("repo=%s auto-compact: size %d >= %.2fx baseline %d", repo, cur, ratio, base)
    try:
        compact_in_place(repo)  # records the new baseline on success
    except Exception as exc:  # noqa: BLE001
        log.error("repo=%s auto-compact failed: %s", repo, exc)


def _run_kopia(repo: str, full: bool) -> dict:
    from ..index.schema import exclusive_offline

    with exclusive_offline(repo, reason="indexing"):
        summary = _run_worker(repo, full)
    maybe_compact(repo)  # after the connection is back, while the file is at its most bloated
    return summary


def _run_backblaze(repo: str) -> dict:
    from ..backblaze import indexer as bz_indexer

    summary = bz_indexer.refresh_via_worker()
    try:
        # The worker's own WAL truncate can no-op while the app holds a read snapshot; retry here.
        from ..backblaze import bbdb as bz_bbdb
        bz_bbdb.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:  # noqa: BLE001 - housekeeping only
        log.warning("BZ wal_checkpoint(TRUNCATE) skipped: %s", exc)
    return summary


def run_index_pass(repo: str, full: bool = False) -> dict:
    """Run this repo's heavy pass to completion. Returns immediately with ``already running`` if
    one is in flight. Safe to call from a request thread as long as the caller does not block on it."""
    if repo not in REPOS:
        return {"ok": False, "reason": f"unknown repo {repo}"}
    lk = _lock(repo)
    if not lk.acquire(blocking=False):
        return {"ok": False, "reason": "already running", "repo": repo}
    _STATE[repo] = {"running": True, "started": time.time(), "error": None,
                    "last": _STATE.get(repo, {}).get("last")}
    started = time.time()
    try:
        summary = _run_kopia(repo, full) if repo_kind(repo) != "backblaze" else _run_backblaze(repo)
    except Exception as exc:  # noqa: BLE001 - a failed pass must never kill the scheduler loop
        log.exception("repo=%s index pass failed", repo)
        summary = {"ok": False, "reason": str(exc)[:500]}
    finally:
        elapsed = time.time() - started
        _STATE[repo] = {
            "running": False, "started": started, "finished": time.time(), "elapsed": elapsed,
            "error": None if summary.get("ok", True) else summary.get("reason"),
            "last": summary,
        }
        lk.release()
    log.info("repo=%s index pass done in %.0fs: %s", repo, elapsed, summary)
    return summary


def start_index_pass_async(repo: str, full: bool = False) -> dict:
    """Fire-and-forget wrapper for the UI's Index button - the caller polls ``/api/index/status``."""
    if is_running(repo):
        return {"started": False, "reason": "already running", "repo": repo}
    threading.Thread(target=run_index_pass, args=(repo, full), daemon=True).start()
    return {"started": True, "repo": repo, "full": full}
