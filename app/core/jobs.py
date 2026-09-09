"""Shared job runner for every long kopia action the user starts.

One registry, one streaming executor, one progress model for remove-files, snapshot
delete, restore, maintenance and every dry run of the first two. Each job runs on its
own thread, streams both of kopia's pipes into an in-memory ring buffer AND a log file,
reports progress by cursor so any browser can follow, and can be stopped. One audit
line per job; the full raw output is always in the log file.

Stop semantics (verified against kopia 0.23 source and live on a scratch repo, 2026-09-09):
kopia registers interrupt handlers only for snapshot create / mount / migrate; remove-files,
snapshot delete, restore and maintenance terminate immediately on a console event. The true
Ctrl+Break event is therefore future-proofing: it is attempted first (with a force timer),
and under pythonw - the deployment - AttachConsole fails, so Stop terminates the process
right away. That is safe: the repo stayed healthy after every interrupt test, remove-files
rewrites are per-snapshot atomic, and chunked jobs additionally stop before the next chunk.

Concurrency: one MUTATING job per repo at a time (kopia's repository lock would fight a
second one anyway). Restore is repo-read-only and runs concurrently. A mutating job is
refused while the repo's index pass is running, and the nightly scheduler in turn waits
while a maintenance job runs (main._tick checks maintenance_running) - user actions
outrank indexing.

Policy writes will ride the same registry: add a kind to KINDS with a command builder;
nothing else changes.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque

from config import DATA_DIR

from . import audit
from .applog import get_logger
from .kopia import (
    _quote_arg,
    kopia_job_stream,
    maintenance_run_command,
    remove_files_chunked_commands,
    restore_command_full,
    snapshot_delete_commands,
)

log = get_logger("jobs")

BUFFER_LINES = 2000        # in-memory ring; the full raw output always lands in the log file
HISTORY_JOBS = 50          # finished jobs kept in memory (their log files persist regardless)
EVENT_FORCE_AFTER = 300.0  # seconds a delivered Ctrl+Break gets before Stop forces the issue

KINDS = {
    "remove_files": {"label": "Removing files", "mutating": True},
    "snapshot_delete": {"label": "Deleting snapshots", "mutating": True},
    "maintenance": {"label": "Running maintenance", "mutating": True},
    "restore": {"label": "Restoring", "mutating": False},
}

AUDIT_ACTION = {
    "remove_files": "remove-files",
    "snapshot_delete": "snapshot-delete",
    "maintenance": "maintenance",
    "restore": "restore",
}

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


class JobRefused(Exception):
    """A start/stop precondition failed; the message is user-facing."""


def _build_commands(kind: str, repo: str, params: dict) -> tuple[list[list[str]], dict]:
    """The kopia arg-lists for one job, plus meta for the audit line. Every builder used here
    goes through core.kopia._repo_args(repo) - the source-level guard enforces their side."""
    dry = bool(params.get("dry"))
    if kind == "remove_files":
        items = params.get("items") or []
        cmds = remove_files_chunked_commands(
            repo, items, source=params.get("source"), commit=not dry,
            max_chars=32000, flag=params.get("flag", "--object-id"))
        meta = {"items": len(items), "dry": dry}
    elif kind == "snapshot_delete":
        ids = params.get("snapshot_ids") or []
        cmds = snapshot_delete_commands(repo, ids, batch=True)
        if dry:
            # The batch builder always ends with --delete; dropping it turns the same command
            # into kopia's preview (what WOULD be deleted).
            cmds = [c[:-1] for c in cmds]
        meta = {"snapshots": len(ids), "dry": dry}
    elif kind == "maintenance":
        cmd = maintenance_run_command(repo)
        if not params.get("full", True):
            cmd = cmd[:-1]  # drop --full -> quick maintenance
        cmds, meta = [cmd], {"full": bool(params.get("full", True)), "dry": False}
    elif kind == "restore":
        cmd = restore_command_full(repo, params["source_id"], params["target"],
                                   params.get("options"))
        cmds, meta = [cmd], {"source_id": params["source_id"], "target": params["target"], "dry": False}
    else:
        raise JobRefused(f"unknown job kind: {kind}")
    # The builders return PASTEABLE commands starting with "kopia" (that is their contract - the
    # same text is shown/copied in the UI), but the streamer consumes bare args via
    # _base_command, which prepends "kopia" itself. Skipping the strip yields "kopia kopia ..."
    # (caught live: kopia exits instantly with "expected command but got kopia").
    stripped = [c[1:] if c and c[0] == "kopia" else c for c in cmds]
    return stripped, meta


def _default_label(kind: str, params: dict, meta: dict) -> str:
    if kind == "remove_files":
        src = params.get("source")
        scope = ("everywhere" if not src
                 else f"{len(src) if isinstance(src, list) else 1} source(s)")
        return f"{meta['items']} object(s) - {scope}" + (" - dry run" if meta["dry"] else "")
    if kind == "snapshot_delete":
        return f"{meta['snapshots']} snapshot(s)" + (" - dry run" if meta["dry"] else "")
    if kind == "maintenance":
        return "full maintenance" if meta["full"] else "quick maintenance"
    if kind == "restore":
        target = meta["target"]
        return f"to {target}"
    return kind


def _new_job(kind: str, repo: str, label: str, meta: dict, cmds: list[list[str]],
             on_done) -> dict:
    job_id = uuid.uuid4().hex[:12]
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    return {
        "job_id": job_id, "kind": kind, "repo": repo,
        "kind_label": KINDS[kind]["label"], "label": label,
        "mutating": KINDS[kind]["mutating"] and not meta.get("dry"), "dry": bool(meta.get("dry")),
        "detail": meta, "state": "running", "rc": None, "error": None,
        "created": time.time(), "started": None, "finished": None,
        "total": len(cmds), "done": 0, "errors": 0,
        "line_count": 0, "buffer": deque(maxlen=BUFFER_LINES),
        "log_path": os.path.join(log_dir, f"job_{kind}_repo{repo}_{ts}_{job_id}.log"),
        "commands_text": "\n".join(" ".join(_quote_arg(a) for a in c) for c in cmds),
        "stop_requested": False, "_stream": None, "on_done": on_done,
    }


def start_job(kind: str, repo: str, params: dict, label: str | None = None,
              on_done=None) -> dict:
    """Start a job and return its record. Raises JobRefused with a user-facing message when
    a precondition fails (unknown kind, nothing to run, repo indexing, write already running)."""
    if kind not in KINDS:
        raise JobRefused(f"unknown job kind: {kind}")
    try:
        cmds, meta = _build_commands(kind, repo, params)
    except ValueError as exc:  # _repo_args rejected the repo (unknown id, or a non-kopia one)
        raise JobRefused(str(exc)) from exc
    if not cmds:
        raise JobRefused("nothing to run for this job")
    if KINDS[kind]["mutating"] and not meta.get("dry"):
        from . import nightly  # lazy: avoids an import cycle at module load
        if nightly.is_running(repo):
            raise JobRefused(
                f"Repo {repo} is being indexed right now - start after the index pass finishes.")
    job = _new_job(kind, repo, label or _default_label(kind, params, meta), meta, cmds, on_done)
    with _JOBS_LOCK:
        # The write-at-a-time check and the registration are one atomic step: two rapid starts
        # must not both pass the check and register.
        if job["mutating"]:
            clash = next((j for j in _JOBS.values() if j["state"] == "running"
                          and j["mutating"] and j["repo"] == repo), None)
            if clash:
                raise JobRefused(
                    f"{clash['kind_label']} is already running on repo {repo} - "
                    "one write at a time per repo.")
        _JOBS[job["job_id"]] = job
    threading.Thread(target=_run, args=(job, cmds), daemon=True).start()
    log.warning("JOB START %s repo=%s %s", kind, repo, job["label"])
    return job


def _stream_one(job: dict, cmd: list[str], lf) -> tuple[int, str]:
    """Run one command, stamping every line into the ring buffer and appending the raw line
    to the job log. Returns (rc, last stderr line) - the last line feeds the error text."""
    result: dict = {}
    job["_stream"] = result  # Stop reaches the live child the moment it exists - a command
    last_err = ""            # that goes quiet before its first line must still be stoppable
    for stream, line in kopia_job_stream(job["repo"], cmd, result):
        if not line:
            continue
        job["buffer"].append(time.strftime("%H:%M:%S ") + line)
        job["line_count"] += 1
        lf.write(line + "\n")
        if stream == "stderr" and line.strip():
            last_err = line
    lf.flush()
    return int(result.get("returncode") or 0), last_err


def _run(job: dict, cmds: list[list[str]]) -> None:
    """The job thread: chunk by chunk, stopping before the next chunk once stop_requested is
    set (a stop landing between the check and the spawn runs at most one extra batch -
    bounded and per-snapshot atomic)."""
    job["started"] = time.time()
    rc = 0
    try:
        with open(job["log_path"], "a", encoding="utf-8", errors="replace") as lf:
            lf.write(f"=== job {job['job_id']} {job['kind']} repo={job['repo']} "
                     f"{'dry-run' if job['dry'] else 'execute'} {len(cmds)} command(s) ===\n")
            lf.flush()
            for i, cmd in enumerate(cmds, 1):
                if job["stop_requested"]:
                    break
                lf.write(f"\n--- command {i}/{len(cmds)} ---\n")
                lf.flush()
                chunk_rc, last_err = _stream_one(job, cmd, lf)
                job["done"] = i
                if chunk_rc != 0:
                    job["errors"] += 1
                    rc = chunk_rc
                    job["rc"] = chunk_rc
                    job["error"] = f"exit {chunk_rc}: {last_err[:280]}" if last_err else f"exit {chunk_rc}"
            if job["errors"] == 0 and rc == 0:
                job["rc"] = 0
        if job["stop_requested"]:
            job["state"] = "stopped"
        elif job["kind"] in ("restore", "maintenance") and job["rc"] not in (None, 0):
            job["state"] = "failed"
        else:
            job["state"] = "done"
    except Exception as exc:  # noqa: BLE001 - a job failure must never take the thread down hard
        job["state"] = "failed"
        job["error"] = str(exc)[:500]
        log.exception("job %s (%s) failed", job["job_id"], job["kind"])
    finally:
        job["finished"] = time.time()
        job["_stream"] = None
        _prune_history()
        audit.audit("generated" if job["dry"] else "executed", AUDIT_ACTION.get(job["kind"], job["kind"]),
                    repo=job["repo"],
                    summary={**job["detail"], "batches": job["total"], "errors": job["errors"],
                             "stopped": job["stop_requested"]},
                    rc=job["rc"] if job["rc"] is not None else rc,
                    duration_s=job["finished"] - job["started"], log_file=job["log_path"])
        log.warning("JOB %s %s repo=%s done=%d/%d errors=%d",
                    job["state"], job["kind"], job["repo"], job["done"], job["total"], job["errors"])
        if job["on_done"]:
            try:
                job["on_done"](job)
            except Exception:  # noqa: BLE001 - a reporting callback must not break the job
                log.exception("job %s on_done callback failed", job["job_id"])


def _prune_history() -> None:
    with _JOBS_LOCK:
        finished = sorted((j for j in _JOBS.values() if j["state"] != "running"),
                          key=lambda j: j["finished"] or 0)
        while len(finished) > HISTORY_JOBS:
            old = finished.pop(0)
            _JOBS.pop(old["job_id"], None)


def stop_job(job_id: str) -> dict:
    """Ask a running job to stop: request the stop, then end the CURRENT kopia process (the
    Ctrl+Break event when deliverable, TerminateProcess otherwise). Chunked jobs then also
    refuse to start the next chunk. Stopping an already-finished job is a no-op."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise JobRefused(f"unknown job: {job_id}")
    if job["state"] != "running":
        return {"ok": False, "state": job["state"]}
    job["stop_requested"] = True
    proc = (job.get("_stream") or {}).get("proc")
    how = "flagged"
    if proc is not None and proc.poll() is None:
        if _send_ctrl_break(proc):
            how = "event"  # force timer armed inside; kopia gets its grace window
        else:
            _terminate(proc)
            how = "terminated"
    log.warning("JOB STOP %s %s how=%s", job_id, job["kind"], how)
    return {"ok": True, "how": how, "state": job["state"]}


def _send_ctrl_break(proc) -> bool:
    """Try to deliver a real CTRL_BREAK to the child's own console - the graceful path that
    kopia's create/mount/migrate handlers respond to. Returns False when undeliverable, which
    is ALWAYS under pythonw (AttachConsole fails with err 6; verified 2026-09-09), so the
    caller terminates instead. Wrapped so the console dance can never take the server down."""
    import sys
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.FreeConsole()  # no-op when we have none (pythonw)
        ok = False
        if k.AttachConsole(int(proc.pid)):
            k.SetConsoleCtrlHandler(None, True)  # ignore the event ourselves
            ok = bool(k.GenerateConsoleCtrlEvent(1, 0))  # 1 = CTRL_BREAK_EVENT, 0 = that console
            k.SetConsoleCtrlHandler(None, False)
            k.FreeConsole()
            k.AttachConsole(-1)  # back to our parent's console (dev mode); fails harmlessly under pythonw
        else:
            k.AttachConsole(-1)
        if ok and proc.poll() is None:
            t = threading.Timer(EVENT_FORCE_AFTER, lambda: _terminate(proc))
            t.daemon = True
            t.start()
        return ok
    except Exception:  # noqa: BLE001 - the graceful path must never break Stop
        return False


def _terminate(proc) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()  # TerminateProcess on Windows
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
    except Exception:  # noqa: BLE001 - already gone
        pass


def maintenance_running(repo: str | None = None) -> bool:
    """True while a maintenance job runs (optionally scoped to one repo). The nightly scheduler
    checks this before starting a heavy pass: user actions outrank indexing."""
    with _JOBS_LOCK:
        return any(j["state"] == "running" and j["kind"] == "maintenance"
                   and (repo is None or j["repo"] == repo) for j in _JOBS.values())


def _serialize(job: dict) -> dict:
    out = {k: v for k, v in job.items()
           if k not in ("buffer", "_stream", "on_done", "detail")}
    out["elapsed"] = round(max(0.0, (job["finished"] or time.time()) - (job["started"] or job["created"])), 1)
    return out


def job_status(job_id: str, after: int = 0) -> dict | None:
    """One job's state plus the lines AFTER cursor ``after``. ``skipped`` counts lines that
    fell out of the ring buffer which the caller has never seen (they are in the log file)."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        buf = list(job["buffer"])
        base = job["line_count"] - len(buf)
        start = max(int(after), base)
        out = _serialize(job)
        out["lines"] = buf[start - base:]
        out["cursor"] = job["line_count"]
        out["skipped"] = max(0, base - max(int(after), 0))
        return out


def list_jobs() -> list[dict]:
    """Every known job, newest first - the header chip reads running entries from here."""
    with _JOBS_LOCK:
        return [_serialize(j) for j in
                sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)]
