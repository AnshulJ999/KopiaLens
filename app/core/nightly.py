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


INTERRUPTED = "Indexing stopped before it finished (KopiaLens was closed or restarted)"


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


def result_path(repo: str) -> str:
    """The worker's summary file (``app/index/worker.py`` writes it before printing to stdout)."""
    return os.path.join(DATA_DIR, f"index_result_repo{repo}.json")


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - missing or half-written reads as nothing
        return None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _with_reason(summary: dict, returncode: int | None = None) -> dict:
    """A stage failure carries ``errors`` but no ``reason``; build one so the UI never shows a
    placeholder while the real error sits in the summary."""
    if summary.get("ok", True) or summary.get("reason"):
        return summary
    errs = summary.get("errors") or {}
    if errs:
        summary["reason"] = "; ".join(f"{s}: {m}" for s, m in errs.items())[:500]
    else:
        summary["reason"] = f"worker exit {returncode}" if returncode is not None else "index pass failed"
    return summary


def _our_worker(pid: int):
    """The live process for ``pid`` when it is one of our workers, else None. Checked by command
    line, never by PID alone (PIDs get recycled). Raises when the process cannot be inspected."""
    import psutil
    try:
        proc = psutil.Process(pid)
        cmd = " ".join(proc.cmdline())
    except psutil.NoSuchProcess:
        return None
    if "app.index.worker" in cmd or "app.backblaze.worker" in cmd:
        return proc
    return None


def _finish_resumed(repo: str, summary: dict, started: float, trigger: str, full: bool,
                    on_done) -> None:
    """Bookkeeping for a run this process did not start: state, run history, freshness."""
    from . import runhistory  # lazy: keeps nightly import-light
    _with_reason(summary)
    now = time.time()
    _STATE[repo] = {"running": False, "started": started, "finished": now,
                    "elapsed": now - started, "error": None if summary.get("ok", True) else summary.get("reason"),
                    "last": summary}
    runhistory.record(repo, summary, trigger, full, float(summary.get("elapsed") or (now - started)))
    if on_done:
        try:
            on_done(repo, bool(summary.get("ok", False)), summary.get("reason"))
        except Exception:  # noqa: BLE001 - a reporting callback must not break startup
            log.exception("repo=%s on_done callback failed", repo)


def _watch_adopted(repo: str, proc, pid: int, info: dict, on_done) -> None:
    """Wait for a worker taken over at startup, then record its result like any other run."""
    import psutil
    started = float(info.get("started") or time.time())
    summary = None
    try:
        while True:
            try:
                proc.wait(timeout=5)
                break
            except psutil.TimeoutExpired:
                if time.time() - started > worker_timeout():
                    log.error("repo=%s index worker (pid %s) timed out - killing it", repo, pid)
                    proc.kill()
                    _remove(os.path.join(DATA_DIR, f"index_progress_repo{repo}.json"))
                    summary = {"ok": False, "reason": "timeout"}
                    break
            except psutil.NoSuchProcess:
                break
        if summary is None:
            res = _read_json(result_path(repo))
            if res and int(res.get("pid") or 0) == pid:
                summary = res
                _remove(result_path(repo))
            else:
                summary = {"ok": False, "reason": INTERRUPTED}
    except Exception as exc:  # noqa: BLE001 - the slot must be released whatever happens
        log.exception("repo=%s watching the resumed worker failed", repo)
        summary = {"ok": False, "reason": str(exc)[:500]}
    finally:
        _release(repo)
        _finish_resumed(repo, summary or {"ok": False, "reason": INTERRUPTED}, started,
                        info.get("trigger") or "nightly", bool(info.get("full")), on_done)
    log.info("repo=%s resumed index pass done: %s", repo, summary)


def resume_workers(on_done=None) -> dict:
    """Startup: take over index workers left running by the previous app process.

    A live kopia worker is adopted (slot reserved, watched to exit, result recorded); a result a
    worker wrote while no app was running is recorded; a worker that died without one is recorded
    as interrupted, so the normal retry runs. A leftover Backblaze worker is stopped - it retries
    straight after startup anyway. Returns the repos per outcome."""
    import glob
    out: dict[str, list[str]] = {"adopted": [], "finished": [], "interrupted": [], "stopped": []}
    dead: dict[str, dict] = {}
    for path in glob.glob(os.path.join(DATA_DIR, "index_progress_repo*.json")):
        repo = os.path.basename(path)[len("index_progress_repo"):-len(".json")]
        info = _read_json(path) or {}
        pid = int(info.get("pid") or 0)
        try:
            proc = _our_worker(pid) if pid else None
        except Exception as exc:  # noqa: BLE001 - never block startup on cleanup
            # Keep the file: it is the only record of which PID to check next startup.
            log.warning("could not inspect worker pid %s (%s) - keeping %s for the next startup",
                        pid, exc, os.path.basename(path))
            continue
        if proc is None:
            _remove(path)
            dead[repo] = info
            continue
        if repo == "bz" or repo_kind(repo) == "backblaze":
            log.warning("stopping the Backblaze index worker (pid %s) left by a previous run", pid)
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
            _remove(path)
            out["stopped"].append(repo)
            continue
        if repo not in REPOS or not _reserve(repo):
            log.warning("repo=%s index worker (pid %s) left running untracked", repo, pid)
            continue
        _STATE[repo] = {"running": True, "started": float(info.get("started") or time.time()),
                        "error": None, "last": None}
        log.info("repo=%s taking over the running index worker (pid %s)", repo, pid)
        threading.Thread(target=_watch_adopted, args=(repo, proc, pid, info, on_done),
                         name=f"resumed-worker-{repo}", daemon=True).start()
        out["adopted"].append(repo)

    for path in glob.glob(os.path.join(DATA_DIR, "index_result_repo*.json")):
        repo = os.path.basename(path)[len("index_result_repo"):-len(".json")]
        if repo in out["adopted"]:
            continue  # its watcher reads the file when the worker exits
        res = _read_json(path)
        _remove(path)
        if not res or repo not in REPOS:
            continue
        started = float(res.get("finished") or time.time()) - float(res.get("elapsed") or 0)
        _finish_resumed(repo, res, started, res.get("trigger") or "nightly", bool(res.get("full")), on_done)
        log.info("repo=%s recorded an index pass that finished while KopiaLens was closed: ok=%s",
                 repo, res.get("ok"))
        out["finished"].append(repo)
        dead.pop(repo, None)

    for repo, info in dead.items():
        if repo not in REPOS or repo_kind(repo) == "backblaze":
            continue
        started = float(info.get("started") or time.time())
        _finish_resumed(repo, {"ok": False, "reason": INTERRUPTED,
                               "elapsed": round(float(info.get("updated") or started) - started, 1)},
                        started, info.get("trigger") or "nightly", bool(info.get("full")), on_done)
        log.warning("repo=%s index worker stopped before it finished; recorded as interrupted", repo)
        out["interrupted"].append(repo)
    return out


def _run_worker(repo: str, full: bool, trigger: str = "manual") -> dict:
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
                env={**os.environ, "KOPIALENS_INDEX_TRIGGER": trigger},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash under pythonw
            )
            try:
                out, _ = proc.communicate(timeout=worker_timeout())
            except subprocess.TimeoutExpired:
                log.error("repo=%s index worker timed out - killing it", repo)
                proc.kill()
                proc.communicate()
                return {"ok": False, "reason": "timeout"}
            finally:
                # This process records the run from stdout; the file is for a restarted app only.
                res = _read_json(result_path(repo))
                if res and int(res.get("pid") or 0) == proc.pid:
                    _remove(result_path(repo))
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
            _with_reason(summary, proc.returncode)
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


def _run_kopia(repo: str, full: bool, trigger: str = "manual") -> dict:
    """Run the worker. No handover, no compaction.

    The index is immutable Parquet published under a manifest, so the worker and the app read and
    write the same directory concurrently: there is no exclusive file lock to hand over, the repo
    never goes offline, and nothing accumulates dead space to reclaim. The app serves the previous
    generation for the whole run and picks up the new one when the manifest swaps.
    """
    return _run_worker(repo, full, trigger)


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


def run_index_pass(repo: str, full: bool = False, _owned: bool = False,
                   trigger: str = "manual") -> dict:
    """Run this repo's heavy pass to completion. Returns immediately with ``already running`` if
    one is in flight. Safe to call from a request thread as long as the caller does not block on it.

    ``_owned`` means the caller already holds the slot for this repo (``start_index_pass_async``
    reserves synchronously so its return value is truthful) and is handing ownership over; this
    call then releases it at the end as usual. ``trigger`` labels the run history record
    (): "nightly" (the scheduler) or "manual" (the Index button)."""
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
        summary = _run_kopia(repo, full, trigger) if repo_kind(repo) != "backblaze" else _run_backblaze(repo)
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
        #: every outcome (worker summary, timeout, launch failure) is visible right here,
        # so the run history is written by the app - including for runs the worker never
        # survived to report.
        from . import runhistory  # lazy: keeps nightly import-light
        runhistory.record(repo, summary, trigger, full, elapsed)
    log.info("repo=%s index pass done in %.0fs: %s", repo, elapsed, summary)
    return summary


def start_index_pass_async(repo: str, full: bool = False, on_done=None,
                           trigger: str = "manual") -> dict:
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
        summary = run_index_pass(repo, full, _owned=True, trigger=trigger)
        if on_done and not summary.get("skipped"):
            try:
                on_done(repo, bool(summary.get("ok", False)), summary.get("reason"))
            except Exception:  # noqa: BLE001 - a reporting callback must not break the pass
                log.exception("repo=%s on_done callback failed", repo)

    threading.Thread(target=_go, daemon=True).start()
    return {"started": True, "repo": repo, "full": full}
