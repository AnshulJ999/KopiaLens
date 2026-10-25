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
import threading
import time

from config import DATA_DIR, HERE as _KL_ROOT, REPOS, SELF_EXE, is_demo, repo_kind

from .applog import get_logger
from ..settings import load_settings
from . import atomicfile

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


def summary_line(summary: dict, elapsed: float | None = None) -> str:
    """One readable sentence for a finished index pass, instead of the raw summary dict.

    e.g. ``OK in 500s - index 245s, dashboard 9s, ... - skipped: churn``. The dict itself stays
    where it already was (the run record, the progress API); only the app log gets the sentence.
    """
    state = "OK" if summary.get("ok") else "FAILED"
    head = f"{state} in {elapsed:.0f}s" if elapsed else state
    stages = summary.get("stages") or {}
    parts = ", ".join(f"{name} {float(secs):.0f}s" for name, secs in stages.items())
    if parts:
        head += f" - {parts}"
    if summary.get("skipped"):
        head += f" - skipped: {', '.join(summary['skipped'])}"
    if summary.get("errors"):
        head += f" - errors: {', '.join(summary['errors'])}"
    if summary.get("reason"):
        head += f" - {summary['reason']}"
    return head


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


_DEFAULT_FIRST_TIMEOUT = 24 * 3600   # a first index of a big cloud repository can run for hours
STOP_GRACE = 120         # seconds a worker gets to save its progress and exit after a stop request
_POLL = 5                # seconds between checks on a running worker
_SLEPT = 60              # a gap this much longer than a poll means the computer was asleep


class _WorkClock:
    """Time a worker spent working: a gap between polls far longer than the poll is the computer
    sleeping, which does not count. Past the limit it asks the worker to save and stop, and says
    "kill" once the grace period is over too."""

    def __init__(self) -> None:
        self.worked = 0.0
        self.last = time.time()
        self.stop_asked: float | None = None
        self.cause = ""                      # "limit" or "stuck" once a stop was asked for
        self._sig, self._sig_at = None, 0.0  # the tree read's last counters, and when they moved

    def stuck(self, repo: str, limit: float) -> bool:
        """Watchdog for the tree read (the long kopia part): counters that don't move for ``limit``
        seconds of working time mean it is stuck, not slow. Waiting for an unreachable repository
        has its own limit in the worker; the merge and the later stages report no counters and are
        not watched."""
        if limit <= 0 or self.stop_asked is not None:
            return False
        p = worker_progress(repo) or {}
        sync = p.get("sync") if p.get("phase") == "tree sync" else None
        if not isinstance(sync, dict) or sync.get("waiting_since"):
            self._sig = None
            return False
        sig = (sync.get("level"), sync.get("level_read"), sync.get("read"), sync.get("pieces_read"))
        if sig != self._sig:
            self._sig, self._sig_at = sig, self.worked
            return False
        if self.worked - self._sig_at <= limit:
            return False
        from ..index import resume
        log.warning("repo=%s index run made no progress for %.0f min (level %s, %s folders read) - "
                    "asking it to save and stop", repo, (self.worked - self._sig_at) / 60, sig[0], sig[2])
        resume.request_stop(repo)
        self.stop_asked, self.cause = time.time(), "stuck"
        return True

    def over(self, repo: str, limit: float) -> str:
        now = time.time()
        gap, self.last = now - self.last, now
        self.worked += gap if gap < _SLEPT else _POLL
        if self.stop_asked is None:
            if self.worked > limit:
                self.cause = "limit"
                from ..index import resume
                log.warning("repo=%s index run reached its time limit - asking it to save and stop",
                            repo)
                resume.request_stop(repo)
                self.stop_asked = now
            return ""
        return "kill" if now - self.stop_asked > STOP_GRACE else ""


def worker_timeout(repo: str | None = None) -> float:
    """The time limit for one run; a repo with no index yet gets the first-index limit."""
    kl = load_settings().get("kopialens", {})
    first = False
    if repo is not None and repo_kind(repo) != "backblaze":
        try:
            from ..index import store
            first = store.read_manifest(repo) is None
        except Exception:  # noqa: BLE001 - unreadable manifest: the normal limit
            first = False
    try:
        if first:
            return float(kl.get("index_first_timeout", _DEFAULT_FIRST_TIMEOUT))
        return float(kl.get("index_worker_timeout", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return float(_DEFAULT_FIRST_TIMEOUT if first else _DEFAULT_TIMEOUT)


def stall_limit() -> float:
    """Settings > Advanced: how long the tree read may make no progress before the run stops."""
    try:
        return float(load_settings().get("kopialens", {}).get("index_stall_timeout", 3600))
    except (TypeError, ValueError):
        return 3600.0


def worker_progress(repo: str) -> dict | None:
    """The subprocess's live progress file, if it is mid-run. The app cannot query the DuckDB while
    the worker holds it, so this is the only way to render a progress badge during the window."""
    from . import filecrypto
    data = _read_json(os.path.join(DATA_DIR, f"index_progress_repo{repo}.json"))
    if data is None:
        return None
    detail = filecrypto.read_json(_detail_path(repo))     # paths: follows the encryption level
    if isinstance(detail, dict):
        data.update(detail)
    return data


def _detail_path(repo: str) -> str:
    return os.path.join(DATA_DIR, f"index_progress_detail_repo{repo}.json")


def _remove_progress(repo: str) -> None:
    _remove(os.path.join(DATA_DIR, f"index_progress_repo{repo}.json"))
    _remove(_detail_path(repo))


INTERRUPTED = "Indexing stopped before it finished (KopiaLens was closed or restarted)"


def worker_log_path(repo: str) -> str:
    from .applog import INDEX_LOG_DIR
    return os.path.join(INDEX_LOG_DIR, f"index_worker_repo{repo}.log")


def worker_log_lines(repo: str, after: int = 0, limit: int = 400) -> tuple[list[str], int]:
    """ ``(the lines after ``after``, the new cursor)`` - the panel's poll.

    ``after`` is the cursor a previous call returned (a line count). A first call (0) starts at the
    latest run's first line - the file holds many nights. A cursor above the current length means
    the file rolled: start from that run again. Only the first ``limit`` new lines come back, with
    the cursor advanced by exactly those - the pane pages through a burst instead of skipping the
    middle."""
    from . import filecrypto
    try:
        lines = filecrypto.read_lines(worker_log_path(repo))
    except Exception:  # noqa: BLE001 - no log is not an error
        return [], max(0, int(after))
    base = max(0, int(after))
    if base == 0 or base > len(lines):
        base = next((i for i in range(len(lines) - 1, -1, -1)
                     if lines[i].startswith("===== run started")), 0)
    chunk = lines[base:base + max(1, int(limit))]
    return chunk, base + len(chunk)


def queued_repo(active: str) -> str | None:
    """The next repo the nightly will take, for the panel's "then: ..." line.

    Read from the scheduler's own state file instead of guessing: the repos run in ``REPOS`` order,
    one per slot, so the queued one is the next repo whose nightly stamp is older than the ACTIVE
    repo's slot. None when nothing is waiting (the last repo of the night)."""
    from config import REPOS, repo_kind
    from . import filecrypto
    state = filecrypto.read_json(os.path.join(DATA_DIR, "bg_worker_state.json"))
    if not isinstance(state, dict):
        return None
    attempts = state.get("attempts") or {}
    stamps = state.get("nightly") or {}
    slot = float((attempts.get(active) or {}).get("slot") or stamps.get(active) or 0)
    order = [r for r in list(REPOS) if repo_kind(r) != "backblaze"]
    if not slot or active not in order:
        return None
    for repo in order[order.index(active) + 1:]:
        if float(stamps.get(repo) or 0) < slot:
            return repo
    return None


def worker_log_tail(repo: str, limit: int = 200) -> list[str]:
    """ The last ``limit`` lines of this repo's worker log, for the progress panel's live
    log pane. Read-only and bounded; nothing here can block the run it describes."""
    from . import filecrypto
    try:
        return filecrypto.read_lines(worker_log_path(repo))[-max(1, int(limit)):]
    except Exception:  # noqa: BLE001 - no log is not an error
        return []


# The worker's stage order (app/index/worker.py). The panel lists exactly these.
STAGE_ORDER = ("index", "dashboard", "analysis", "churn", "folders", "policies",
               "policy_prices", "ignore_files", "rank_paths", "next_snapshots")


def _stage_weights(repo: str, full: bool) -> dict[str, float]:
    """Typical seconds per stage, from recent runs of the SAME kind (a full rebuild and a nightly
    update differ by minutes). The median of the last few good runs resists one freak night."""
    try:
        from . import runhistory
        runs = [r for r in runhistory.read_tail(limit=40, repos=[repo])
                if r.get("ok") and bool(r.get("full")) == bool(full) and r.get("stages")][:5]
    except Exception:  # noqa: BLE001 - no history is simply no estimate
        runs = []
    weights: dict[str, float] = {}
    for name in STAGE_ORDER:
        vals = sorted(float((r.get("stages") or {}).get(name) or 0) for r in runs)
        vals = [v for v in vals if v > 0]
        if vals:
            weights[name] = vals[len(vals) // 2]
    return weights


def run_progress(repo: str, prog: dict | None = None) -> dict:
    """ One whole-run percentage and ETA, never a lame per-stage bar.

    Completed stages' typical seconds plus the current stage's own fraction, over the sum of the
    typical seconds. Where a stage has a REAL fraction (``prog["pct"]`` - the index stage's folder
    count) that is used; elsewhere the fraction is time-based and capped below 100 % so the number
    cannot free-run on a slow stage. Without run history there is no estimate: ``estimated`` is
    False and ``pct`` is None, and the panel shows the counters it does have."""
    prog = prog or {}
    stage = str(prog.get("stage") or "")
    weights = _stage_weights(repo, bool(prog.get("full")))
    first = False
    if not weights and (prog.get("estimate") or {}).get("stage_seconds"):
        weights, first = dict(prog["estimate"]["stage_seconds"]), True   # a first index
    total = sum(weights.values())
    stages = [{"name": n, "seconds": weights.get(n), "state": "pending"} for n in STAGE_ORDER]
    if stage not in STAGE_ORDER or not total:
        return {"stage": stage or None, "pct": None, "eta_seconds": None, "estimated": False,
                "stages": stages}
    idx = STAGE_ORDER.index(stage)
    for i, item in enumerate(stages):
        item["state"] = "done" if i < idx else ("current" if i == idx else "pending")
    done = sum(weights.get(n, 0.0) for n in STAGE_ORDER[:idx])
    cur_w = weights.get(stage, 0.0)
    real = prog.get("pct")
    if real is not None:
        frac = max(0.0, min(1.0, float(real) / 100.0))
    else:
        started = float(prog.get("stage_started") or 0)
        frac = min(0.95, max(0.0, (time.time() - started) / cur_w)) if (started and cur_w) else 0.0
    overall = (done + cur_w * frac) / total if total else 0.0
    return {"stage": stage, "pct": int(max(0.0, min(0.99, overall)) * 100),
            "eta_seconds": int(max(0.0, total - done - cur_w * frac)),
            "estimated": True, "first_estimate": first, "stages": stages}


def stop_index_pass(repo: str) -> dict:
    """ Terminate this repo's in-flight worker and report what was stopped.

    The worker writes its pid into the progress file and ``our_worker`` confirms the pid is really
    ours (never a pid alone - they get recycled). A kill LOOKS like a crash to the scheduler, which
    is why the caller must also record the stop: that record is what keeps it stopped."""
    info = _read_json(os.path.join(DATA_DIR, f"index_progress_repo{repo}.json")) or {}
    pid = int(info.get("pid") or 0)
    if not pid:
        return {"stopped": False, "reason": "nothing is indexing this repo"}
    try:
        proc = our_worker(pid)
    except Exception as exc:  # noqa: BLE001 - an uninspectable pid is not a licence to kill
        return {"stopped": False, "reason": f"cannot inspect pid {pid}: {exc}"}
    if proc is None:
        return {"stopped": False, "reason": "the worker has already exited"}
    # The index stage saves its progress and exits when asked; anything still running after the
    # grace period, and every later stage, is ended straight away (the desktop app's Quit rule).
    from ..index import resume
    asked = info.get("stage") == "index"
    if asked:
        resume.request_stop(repo)

    def _finish() -> None:
        try:
            if asked:
                try:
                    proc.wait(timeout=STOP_GRACE)
                    return
                except Exception:  # noqa: BLE001 - still running (or uninspectable): end it
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 - fall back to a hard kill
                try:
                    proc.kill()
                except Exception as exc:  # noqa: BLE001
                    log.error("repo=%s could not stop index worker pid %s: %s", repo, pid, exc)
        finally:
            resume.clear_stop(repo)
    threading.Thread(target=_finish, name=f"index-stop-{repo}", daemon=True).start()
    log.info("repo=%s index worker (pid %s) asked to stop", repo, pid)
    return {"stopped": True, "pid": pid}


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
                atomicfile.replace(src, f"{path}.{i + 1}")
        atomicfile.replace(path, f"{path}.1")
    except OSError as exc:
        log.warning("worker log rotation failed for %s: %s", path, exc)


def result_path(repo: str) -> str:
    """The worker's summary file (``app/index/worker.py`` writes it before printing to stdout)."""
    return os.path.join(DATA_DIR, f"index_result_repo{repo}.json")


def _read_json(path: str) -> dict | None:
    """The worker's PLAIN status file (pid, stage, counts)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - missing or half-written reads as nothing
        return None


def _read_result(path: str) -> dict | None:
    """The worker's summary file, encrypted at the Full level."""
    from . import filecrypto
    data = filecrypto.read_json(path)
    return data if isinstance(data, dict) else None


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


def our_worker(pid: int):
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
    limit = worker_timeout(repo)
    clock = _WorkClock()
    summary = None
    try:
        while True:
            try:
                proc.wait(timeout=_POLL)
                break
            except psutil.TimeoutExpired:
                if clock.over(repo, limit) == "kill":
                    log.error("repo=%s index worker (pid %s) timed out - killing it", repo, pid)
                    proc.kill()
                    _remove_progress(repo)
                    summary = {"ok": False, "reason": "timeout"}
                    break
            except psutil.NoSuchProcess:
                break
        if summary is None:
            res = _read_result(result_path(repo))
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
    log.info("repo=%s resumed index pass: %s", repo, summary_line(summary, None))


def resume_workers(on_done=None) -> dict:
    """Startup: take over index workers left running by the previous app process.

    A live kopia worker is adopted (slot reserved, watched to exit, result recorded); a result a
    worker wrote while no app was running is recorded; a worker that died without one is recorded
    as interrupted, so the normal retry runs. A leftover Backblaze worker is left to finish (it
    records its own result). Returns the repos per outcome."""
    import glob

    from . import filecrypto
    out: dict[str, list[str]] = {"adopted": [], "finished": [], "interrupted": [], "left_running": []}
    dead: dict[str, dict] = {}
    for path in glob.glob(os.path.join(DATA_DIR, "index_progress_repo*.json")):
        repo = os.path.basename(path)[len("index_progress_repo"):-len(".json")]
        info = _read_json(path) or {}
        pid = int(info.get("pid") or 0)
        try:
            proc = our_worker(pid) if pid else None
        except Exception as exc:  # noqa: BLE001 - never block startup on cleanup
            # Keep the file: it is the only record of which PID to check next startup.
            log.warning("could not inspect worker pid %s (%s) - keeping %s for the next startup",
                        pid, exc, os.path.basename(path))
            continue
        if proc is None:
            _remove_progress(repo)
            dead[repo] = info
            continue
        if repo == "bz" or repo_kind(repo) == "backblaze":
            # It records its own result in the Backblaze database; the BZ checks treat it as running.
            log.info("Backblaze index worker (pid %s) from the previous run left to finish", pid)
            out["left_running"].append(repo)
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
        res = _read_result(path)
        if res is None and filecrypto.file_state(path) == "encrypted" and not filecrypto.available():
            # Encrypted and no key yet (remember-me off): keep it for the next start after an unlock.
            log.info("repo=%s index result is encrypted and locked - kept for later", repo)
            continue
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
    has been healthy over several nights. ``_roll_worker_log`` bounds the size.

    Encryption: the worker gets the data key over its stdin (never argv or env, which other
    same-user processes can read) as one JSON line, with the OS keychain as the other carrier.
    A locked install SKIPS the pass rather than writing a plaintext generation."""
    from ..index import crypto
    if crypto.enabled() and not crypto.unlocked() and not crypto.unlock_from_keychain():
        log.error("repo=%s index pass skipped: the index is encrypted and locked", repo)
        return {"ok": False, "skipped": True,
                "reason": "the index is encrypted and locked - unlock KopiaLens to run the index"}
    args = [SELF_EXE, "-m", "app.index.worker", repo]
    if full:
        args.append("--full")
    limit = worker_timeout(repo)    # decided before the run: a finished first index has a manifest
    from ..index import resume
    resume.clear_stop(repo)         # a stop request left over from an earlier run
    logp = worker_log_path(repo)
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    _roll_worker_log(logp)
    payload = crypto.worker_payload() if crypto.enabled() else None
    env = {**os.environ, "KOPIALENS_INDEX_TRIGGER": trigger}
    if payload is not None:
        env["KOPIALENS_KEY_STDIN"] = "1"
    from . import filecrypto
    child_logs = filecrypto.available()
    if child_logs:
        # The child writes its own log: a redirected descriptor would bypass any wrapper and
        # land plaintext in a file the Full level says is encrypted.
        env["KOPIALENS_WORKER_LOG"] = logp
        filecrypto.append_line(logp, "")
        filecrypto.append_line(logp, f"===== run started {dt.datetime.now():%Y-%m-%d %H:%M:%S} "
                                     f"repo={repo} full={full} =====")
    errfh = None if child_logs else open(logp, "a", encoding="utf-8", errors="replace")
    try:
        if errfh is not None:
            errfh.write(f"\n===== run started {dt.datetime.now():%Y-%m-%d %H:%M:%S} "
                        f"repo={repo} full={full} =====\n")
            errfh.flush()
        proc = subprocess.Popen(
            args, cwd=_KL_ROOT, stdout=subprocess.PIPE,
            stderr=errfh if errfh is not None else subprocess.DEVNULL, text=True,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash under pythonw
        )
        clock = _WorkClock()
        stall = stall_limit()
        send = payload
        try:
            while True:
                try:
                    out, _ = proc.communicate(input=send, timeout=_POLL)
                    break
                except subprocess.TimeoutExpired:
                    send = None          # the key goes in once; later calls only read
                    clock.stuck(repo, stall)
                    if clock.over(repo, limit) == "kill":
                        log.error("repo=%s index worker didn't stop after %s - killing it", repo,
                                  "making no progress" if clock.cause == "stuck" else "its time limit")
                        proc.kill()
                        proc.communicate()
                        return {"ok": False, "reason": "stuck - no progress" if clock.cause == "stuck" else "timeout"}
        finally:
            # This process records the run from stdout; the file is for a restarted app only.
            res = _read_result(result_path(repo))
            if res and int(res.get("pid") or 0) == proc.pid:
                _remove(result_path(repo))
    except Exception as exc:  # noqa: BLE001 - could not even launch
        log.error("repo=%s index worker launch failed: %s", repo, exc)
        return {"ok": False, "reason": f"launch failed: {exc}"}
    finally:
        if errfh is not None:
            errfh.close()
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
            if summary.get("stopped") and clock.stop_asked is not None:
                summary["stopped"] = False      # the time limit or the watchdog, not the user: retried as usual
                summary["reason"] = ("no progress for a long time (stuck) - the next run continues from where it stopped"
                                     if clock.cause == "stuck" else
                                     "time limit reached - the next run continues from where it stopped")
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
    if is_demo(repo):
        if _owned:
            _release(repo)
        return {"ok": False, "skipped": True, "reason": "a demo copy is not indexed"}
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
    log.info("repo=%s index pass: %s", repo, summary_line(summary, elapsed))
    return summary


def start_index_pass_async(repo: str, full: bool = False, on_done=None,
                           trigger: str = "manual") -> dict:
    """Fire-and-forget wrapper for the UI's Index button - the caller polls ``/api/index/status``.
    Takes the same single global slot the scheduler uses, so a manual Index cannot start a second
    worker alongside a nightly one on another repo."""
    if repo not in REPOS:     # validate BEFORE taking the slot, never after
        return {"started": False, "repo": repo, "reason": f"unknown repo {repo}"}
    from . import repos as repo_store
    if repo_store.via_server(repo):
        return {"started": False, "repo": repo,
                "reason": "reached through a kopia server, which cannot be indexed"}
    if is_demo(repo):
        return {"started": False, "repo": repo, "reason": "a demo copy is not indexed"}
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
