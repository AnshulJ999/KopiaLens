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

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time

from config import DATA_DIR, HERE as _KL_ROOT, REPOS, repo_kind

from .applog import get_logger
from ..settings import load_settings

log = get_logger("nightly")

_DEFAULT_TIMEOUT = 4 * 3600  # a full rebuild of the big repo is tens of minutes; leave real headroom

_LOCKS_GUARD = threading.Lock()
_RESERVED: str | None = None     # the repo currently allowed to run a heavy pass; None = free
_STATE: dict[str, dict] = {}  # repo -> {"running": bool, "started": ts, "last": {...}, "error": str|None}


def _reserve(repo: str) -> bool:
    """Claim the single global heavy-pass slot. An atomic compare-and-set under the guard, so it
    is the ONLY gate needed - whoever gets True runs, everyone else is told no. Two workers at
    once would double the memory and thrash the same disks, which this design exists to avoid."""
    global _RESERVED
    with _LOCKS_GUARD:
        if _RESERVED is not None:
            return False
        _RESERVED = repo
        return True


def _release(repo: str) -> None:
    global _RESERVED
    with _LOCKS_GUARD:
        if _RESERVED == repo:
            _RESERVED = None


def reserved_repo() -> str | None:
    with _LOCKS_GUARD:
        return _RESERVED


def is_running(repo: str) -> bool:
    return bool(_STATE.get(repo, {}).get("running"))


def any_running() -> bool:
    """True while ANY repo's heavy pass is reserved or in flight. Two workers at once would double
    the memory and thrash the same disks, which is the whole thing this design exists to avoid."""
    return reserved_repo() is not None or any(s.get("running") for s in _STATE.values())


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


_PROC_LOCK = threading.Lock()
_PROCS: dict[str, subprocess.Popen] = {}   # live worker processes, so shutdown can end them


def worker_log_path(repo: str) -> str:
    return os.path.join(DATA_DIR, "logs", f"index_worker_repo{repo}.log")


WORKER_LOG_MAX_BYTES = 1_000_000
WORKER_LOG_KEEP = 10


def _roll_worker_log(path: str, max_bytes: int = WORKER_LOG_MAX_BYTES,
                     keep: int = WORKER_LOG_KEEP) -> None:
    """Size-based rotation, mirroring the app log's RotatingFileHandler.

    The worker log used to be truncated on every run, so only the most recent nightly existed -
    which makes any question of the form "has this stage been healthy for a few nights?"
    unanswerable after the fact. A run writes a few KB, so this holds months."""
    try:
        if os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return  # no log yet
    try:
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(keep - 1, 0, -1):
            src = f"{path}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")
    except OSError as exc:
        log.warning("worker log rotation failed for %s: %s", path, exc)


def terminate_running(grace: float = 10.0) -> None:
    """End any live worker. Called on app shutdown: a worker outlives its parent otherwise, and
    since it holds the DuckDB exclusively, the app you just restarted could not open that repo at
    all. A half-finished index is harmless - the next pass sees the missing snapshots and continues.
    Waiting for it instead could block shutdown for the better part of an hour."""
    with _PROC_LOCK:
        procs = list(_PROCS.items())
    for repo, proc in procs:
        if proc.poll() is not None:
            continue
        log.warning("repo=%s terminating index worker (pid %s) for shutdown", repo, proc.pid)
        try:
            proc.terminate()
            proc.wait(timeout=grace)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def wait_idle(timeout: float = 20.0) -> bool:
    """Wait (briefly) for any in-flight pass thread to finish unwinding. terminate_running() ends
    the CHILD process, but the parent thread still has to reopen and possibly compact the DuckDB;
    tearing the connection down underneath it is asking for trouble. Returns True if it went idle."""
    deadline = time.time() + timeout
    while any_running() and time.time() < deadline:
        time.sleep(0.2)
    return not any_running()


def reap_orphan_workers() -> list[str]:
    """Kill any index worker left over from a previous app run, before the DuckDB is opened.

    Identified by the PID recorded in its own progress file, and confirmed by checking the process
    is genuinely one of our workers (name + command line) so a recycled PID can never be mistaken
    for one. Returns the repos cleaned up.
    """
    import glob
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return []
    reaped: list[str] = []
    for path in glob.glob(os.path.join(DATA_DIR, "index_progress_repo*.json")):
        repo = None
        try:
            with open(path, encoding="utf-8") as fh:
                info = json.load(fh)
            repo, pid = info.get("repo"), int(info.get("pid") or 0)
        except Exception:  # noqa: BLE001 - unreadable progress file: just remove it
            pid = 0
        if pid:
            try:
                proc = psutil.Process(pid)
                cmd = " ".join(proc.cmdline())
                # Confirm it is OUR worker before killing anything - PIDs get recycled.
                if "app.index.worker" in cmd:
                    log.warning("reaping orphaned index worker for repo=%s (pid %s) left by a "
                                "previous run - it still holds the DuckDB", repo, pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except Exception:  # noqa: BLE001
                        proc.kill()
                    if repo:
                        reaped.append(repo)
            except psutil.NoSuchProcess:
                pass
            except Exception as exc:  # noqa: BLE001 - never block startup on cleanup
                # Could not inspect it - it may still be alive and holding the DuckDB. KEEP the
                # progress file: deleting it would destroy the only record of which PID to reap,
                # so the next startup could not recover either.
                log.warning("could not inspect worker pid %s (%s) - keeping %s for the next "
                            "startup to retry", pid, exc, os.path.basename(path))
                continue
        try:
            os.remove(path)   # the worker that owned it is confirmed gone
        except Exception:  # noqa: BLE001
            pass
    return reaped


def _run_worker(repo: str, full: bool) -> dict:
    """Run the worker subprocess. Its stderr (all the run's log lines) goes to a file rather than a
    parent-side buffer: it survives the run for diagnosis, and none of it sits in this process.

    APPEND, never truncate - past runs are the only evidence for questions about whether a stage
    has been healthy over several nights. ``_roll_worker_log`` bounds the size."""
    args = [sys.executable, "-m", "app.index.worker", repo]
    if full:
        args.append("--full")
    logp = worker_log_path(repo)
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    _roll_worker_log(logp)
    try:
        with open(logp, "a", encoding="utf-8", errors="replace") as errfh:
            errfh.write(f"\n===== run started {dt.datetime.now():%Y-%m-%d %H:%M:%S} "
                        f"repo={repo} full={full} =====\n")
            errfh.flush()
            proc = subprocess.Popen(
                args, cwd=_KL_ROOT, stdout=subprocess.PIPE, stderr=errfh, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash under pythonw
            )
            with _PROC_LOCK:
                _PROCS[repo] = proc
            try:
                out, _ = proc.communicate(timeout=worker_timeout())
            except subprocess.TimeoutExpired:
                log.error("repo=%s index worker timed out - killing it", repo)
                proc.kill()
                proc.communicate()
                return {"ok": False, "reason": "timeout"}
            finally:
                with _PROC_LOCK:
                    _PROCS.pop(repo, None)
    except Exception as exc:  # noqa: BLE001 - could not even launch
        log.error("repo=%s index worker launch failed: %s", repo, exc)
        return {"ok": False, "reason": f"launch failed: {exc}"}
    # Parse FIRST, exit code second. The worker prints a JSON summary carrying the real reason
    # ("repository unavailable", the failing stage) and THEN exits 1 - on both of its failure
    # shapes. Reading the exit code first threw that away and surfaced "worker exit 1", which is
    # exactly the message the freshness strip is not supposed to be showing.
    summary = None
    parse_error = None
    try:
        parsed = json.loads((out or "").strip())
        summary = parsed if isinstance(parsed, dict) else None
    except Exception as exc:  # noqa: BLE001 - malformed output must not crash the scheduler
        parse_error = exc

    if proc.returncode != 0:
        if summary is not None:
            summary["ok"] = False
            # A stage failure prints the full summary with `errors: {stage: msg}` and NO `reason`
            # (index/worker.py) - build the reason from those instead of the placeholder, or the
            # freshness strip shows "worker exit 1" while the real error sits right there.
            if "reason" not in summary:
                errs = summary.get("errors") or {}
                if errs:
                    summary["reason"] = "; ".join(f"{s}: {m}" for s, m in errs.items())[:500]
                else:
                    summary["reason"] = f"worker exit {proc.returncode}"
            summary["log"] = logp
            log.error("repo=%s index worker exit %d: %s (see %s)",
                      repo, proc.returncode, summary["reason"], logp)
            return summary
        log.error("repo=%s index worker exit %d, no usable output (%s) (see %s)",
                  repo, proc.returncode, parse_error or "empty", logp)
        return {"ok": False, "reason": f"worker exit {proc.returncode}", "log": logp}

    if summary is None:
        log.error("repo=%s index worker output unparsable: %s (see %s)", repo, parse_error, logp)
        return {"ok": False, "reason": f"bad worker output: {parse_error}", "log": logp}
    return summary


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


def run_index_pass(repo: str, full: bool = False, _owned: bool = False) -> dict:
    """Run this repo's heavy pass to completion. Returns immediately with ``already running`` if
    one is in flight. Safe to call from a request thread as long as the caller does not block on it.

    ``_owned`` means the caller already holds the slot for this repo (``start_index_pass_async``
    reserves synchronously so its return value is truthful) and is handing ownership over; this
    call then releases it at the end as usual."""
    if repo not in REPOS:
        if _owned:
            _release(repo)   # the caller reserved for us; bailing must not strand the slot
        return {"ok": False, "skipped": True, "reason": f"unknown repo {repo}"}
    if not _owned and not _reserve(repo):
        busy = reserved_repo()
        return {"ok": False, "skipped": True, "repo": repo,
                "reason": "already indexing" if busy == repo else f"repo {busy} is indexing"}
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
        _release(repo)
    log.info("repo=%s index pass done in %.0fs: %s", repo, elapsed, summary)
    return summary


def start_index_pass_async(repo: str, full: bool = False, on_done=None) -> dict:
    """Fire-and-forget wrapper for the UI's Index button - the caller polls ``/api/index/status``.
    Takes the same single global slot the scheduler uses, so a manual Index cannot start a second
    worker alongside a nightly one on another repo."""
    if repo not in REPOS:     # validate BEFORE taking the slot, never after
        return {"started": False, "repo": repo, "reason": f"unknown repo {repo}"}
    if not _reserve(repo):    # synchronous, so a "started" answer is never a guess
        busy = reserved_repo()
        return {"started": False, "repo": repo,
                "reason": "already indexing" if busy == repo else f"repo {busy} is indexing"}

    def _go() -> None:
        summary = run_index_pass(repo, full, _owned=True)
        if on_done and not summary.get("skipped"):
            try:
                on_done(repo, bool(summary.get("ok", False)), summary.get("reason"))
            except Exception:  # noqa: BLE001 - a reporting callback must not break the pass
                log.exception("repo=%s on_done callback failed", repo)

    threading.Thread(target=_go, daemon=True).start()
    return {"started": True, "repo": repo, "full": full}
