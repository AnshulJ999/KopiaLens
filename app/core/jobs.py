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

Policy writes run as Python steps (callables in the command list), optionally followed by a
delete's kopia commands; a failed step holds the delete.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque

from config import DATA_DIR

from . import audit
from .applog import get_logger
from .kopia import (
    _parse_delete_line,
    _parse_remove_line,
    _quote_arg,
    _repo_args,
    kopia_job_stream,
    maintenance_run_command,
    remove_files_chunked_commands,
    restore_command_full,
    snapshot_create_command,
    snapshot_delete_commands,
    snapshot_pin_command,
)

log = get_logger("jobs")

BUFFER_LINES = 2000        # in-memory ring; the full raw output always lands in the log file
HISTORY_JOBS = 50          # finished jobs kept in memory (their log files persist regardless)
HISTORY_FILE_LINES = 1000  # on-disk history cap: a restart must not blank the Activity view
EVENT_FORCE_AFTER = 300.0  # seconds a delivered Ctrl+Break gets before Stop forces the issue

KINDS = {
    "remove_files": {"label": "Removing files", "mutating": True},
    "snapshot_delete": {"label": "Deleting snapshots", "mutating": True},
    "maintenance": {"label": "Running maintenance", "mutating": True},
    "restore": {"label": "Restoring", "mutating": False},
    "policy_write": {"label": "Saving policies", "mutating": False},
    "snapshot_create": {"label": "Taking a snapshot", "mutating": False},
    "snapshot_pin": {"label": "Changing pins", "mutating": False},
    # Local-only (): rewrites KopiaLens's own index, never the kopia repository. Mutating
    # so it cannot run beside another write job or an index pass - both touch the same manifest.
    "encryption_switch": {"label": "Switching encryption", "mutating": True},
}

AUDIT_ACTION = {
    "remove_files": "remove-files",
    "snapshot_delete": "snapshot-delete",
    "maintenance": "maintenance",
    "restore": "restore",
    "policy_write": "policy-write",
    "snapshot_create": "snapshot-create",
    "snapshot_pin": "snapshot-pin",
    "encryption_switch": "encryption-switch",
}


def say(job: dict, lf, text: str) -> None:
    """One output line from a Python step (policy writes), same shape as kopia's lines."""
    job["buffer"].append(time.strftime("%H:%M:%S ") + text)
    job["line_count"] += 1
    lf.write(text + "\n")
    lf.flush()

# Dry-run jobs build the same summary the synchronous dry-run endpoints report; the parsers
# are shared with them (one home in kopia.py, fed per streamed line here).
DRY_PARSERS = {
    "remove_files": _parse_remove_line,
    "snapshot_delete": _parse_delete_line,
}

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()


def _history_path() -> str:
    return os.path.join(DATA_DIR, "jobs_history.jsonl")


def _record_history(job: dict) -> None:
    """ Append the finished job to the on-disk history so Activity survives a restart
    (the in-memory registry is lost every exit; the log files alone are not a list). Capped by a
    rewrite at HISTORY_FILE_LINES - old records fall off the end. Best-effort."""
    rec = {
        "job_id": job["job_id"], "kind": job["kind"], "kind_label": job["kind_label"],
        "repo": job["repo"], "label": job["label"], "dry": job["dry"],
        "mutating": job["mutating"], "state": job["state"], "rc": job["rc"],
        "error": job["error"], "detail": job.get("detail") or {},
        "created": job["created"], "started": job["started"], "finished": job["finished"],
        "elapsed": round(max(0.0, (job["finished"] or job["created"])
                             - (job["started"] or job["created"])), 1),
        "total": job["total"], "done": job["done"], "errors": job["errors"],
        "log_path": job["log_path"],
    }
    try:
        line = json.dumps(rec, ensure_ascii=False)
        with _HISTORY_LOCK:
            os.makedirs(os.path.dirname(_history_path()), exist_ok=True)
            with open(_history_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if os.path.getsize(_history_path()) > HISTORY_FILE_LINES * 140:  # heuristic lines cap
                with open(_history_path(), encoding="utf-8") as fh:
                    lines = fh.readlines()
                if len(lines) > HISTORY_FILE_LINES:
                    tmp = _history_path() + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.writelines(lines[-HISTORY_FILE_LINES:])
                    os.replace(tmp, _history_path())
    except Exception as exc:  # noqa: BLE001 - history is best-effort, the job already ran
        log.warning("job history write failed: %s", exc)


def read_job_history(limit: int = 200) -> list[dict]:
    """The last ``limit`` finished jobs from disk, newest first - the part of Activity that
    survives restarts."""
    try:
        with _HISTORY_LOCK:
            with open(_history_path(), encoding="utf-8") as fh:
                lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001 - one damaged line must not hide the rest
            pass
    out.sort(key=lambda j: j.get("created") or 0, reverse=True)
    return out


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
        if params.get("resolved"):
            meta["resolved"] = params["resolved"]      # preview vs filter
        if params.get("index_changed"):
            meta["index_changed"] = True
        if params.get("note"):
            meta["note"] = params["note"]
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
        full = bool(params.get("full", True))
        if not full:
            cmd = cmd[:-1]  # drop --full -> quick maintenance
        safety = "none" if params.get("safety") == "none" else "full"
        if safety == "none":
            cmd = cmd + ["--safety=none"]
        cmds, meta = [cmd], {"full": full, "safety": safety, "dry": False,
                             "measure_path": params.get("measure_path"),
                             "size_before": params.get("size_before")}
    elif kind == "restore":
        items = params.get("items") or [{"source_id": params["source_id"], "target": params["target"]}]
        cmds = [restore_command_full(repo, it["source_id"], it["target"], params.get("options"))
                for it in items]
        meta = {"source_id": items[0]["source_id"], "target": params.get("label_target") or items[0]["target"],
                "items": len(items), "dry": False}
    elif kind == "snapshot_create":
        cmds = [snapshot_create_command(repo, params["path"], params.get("description") or "")]
        meta = {"path": params["path"], "dry": False}
    elif kind == "snapshot_pin":
        ids = params.get("snapshot_ids") or []
        cmds = [snapshot_pin_command(repo, ids, remove=bool(params.get("remove")))] if ids else []
        meta = {"snapshots": len(ids), "remove": bool(params.get("remove")), "dry": False}
    elif kind == "policy_write":
        # Python steps (one per policy), then optionally the delete's kopia commands.
        steps = list(params.get("steps") or [])
        then = params.get("then")
        then_cmds: list[list[str]] = []
        meta = {"writes": len(steps), "dry": False, "then": then["kind"] if then else None}
        if then:
            then_cmds, then_meta = _build_commands(then["kind"], repo, then["params"])
            meta["then_detail"] = then_meta
        return steps + then_cmds, meta
    elif kind == "encryption_switch":
        # One Python step, no kopia command: rewrite the generation and the side caches, then
        # report what they were. Returns early so the kopia-prefix check below is skipped.
        on = bool(params.get("on"))

        def _switch_step(job: dict, lf) -> tuple[int, str]:
            from ..index import keyswitch
            say_ = lambda text: say(job, lf, text)          # noqa: E731
            try:
                out = keyswitch.switch_repo(repo, on, say=say_)
                out["caches"] = keyswitch.switch_caches(on, say=say_)
                job["keyswitch"] = out
                if out.get("note"):
                    say_(out["note"])
                return 0, ""
            except Exception as exc:  # noqa: BLE001 - the runner reports it like any other step
                say_(f"failed: {exc}")
                return 1, str(exc)[:280]

        return [_switch_step], {"on": on, "dry": False}
    else:
        raise JobRefused(f"unknown job kind: {kind}")
    # The builders return PASTEABLE commands: ["kopia"] + _repo_args(repo) + subcommand. The
    # streamer consumes BARE args via _base_command, which re-adds "kopia" AND the repo args.
    # Stripping only "kopia" left the repo args in, and _base_command added them a second time -
    # invisible on repo 1 (empty args list), but repo 2 ran with its --config-file twice. Strip
    # the exact prefix; a mismatch means a builder changed shape and would double args, so it
    # fails loudly instead of silently corrupting the command.
    prefix = ["kopia"] + _repo_args(repo)
    stripped = []
    for c in cmds:
        if c[:len(prefix)] != prefix:
            raise RuntimeError(f"job builder output lacks the kopia+repo-args prefix: {c[:4]}")
        stripped.append(c[len(prefix):])
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
        return ("full maintenance" if meta["full"] else "quick maintenance") + (
            ", safety none" if meta.get("safety") == "none" else "")
    if kind == "restore":
        target = meta["target"]
        return f"to {target}"
    if kind == "snapshot_create":
        return meta["path"]
    if kind == "snapshot_pin":
        return f"{'unpin' if meta['remove'] else 'pin'} {meta['snapshots']} snapshot(s)"
    if kind == "policy_write":
        n = meta["writes"]
        base = f"{n} polic{'y' if n == 1 else 'ies'}"
        return base + (", then delete" if meta.get("then") else "")
    if kind == "encryption_switch":
        return "encrypting the index" if meta.get("on") else "removing encryption"
    return kind


def _new_job(kind: str, repo: str, label: str, meta: dict, cmds: list[list[str]],
             pasteable: list[list[str]], on_done) -> dict:
    job_id = uuid.uuid4().hex[:12]
    ts = time.strftime("%Y%m%d_%H%M%S")
    from .applog import JOB_LOG_DIR, ensure_dirs
    ensure_dirs()
    log_dir = JOB_LOG_DIR
    return {
        "job_id": job_id, "kind": kind, "repo": repo,
        "kind_label": KINDS[kind]["label"] if not meta.get("then") else "Excluding and deleting",
        "label": label,
        "mutating": (KINDS[kind]["mutating"] or bool(meta.get("then"))) and not meta.get("dry"),
        "dry": bool(meta.get("dry")),
        "results": [], "held": False, "target": meta.get("target") if kind == "restore" else None,
        "detail": meta, "state": "running", "rc": None, "error": None,
        "created": time.time(), "started": None, "finished": None,
        "total": len(cmds), "done": 0, "errors": 0,
        "line_count": 0, "buffer": deque(maxlen=BUFFER_LINES),
        "log_path": os.path.join(log_dir, f"job_{kind}_repo{repo}_{ts}_{job_id}.log"),
        # Pasteable form (kopia + repo args + the command): what the runner executes, and what
        # Copy command must hand the user - the bare `cmds` would not be runnable.
        "commands_text": "\n".join(" ".join(_quote_arg(a) for a in c) for c in pasteable
                                   if not callable(c)),
        "dry_summary": {},  # incremental dry-run summary (entries/snapshots/final_line)
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
    if (KINDS[kind]["mutating"] or meta.get("then")) and not meta.get("dry"):
        from . import nightly  # lazy: avoids an import cycle at module load
        if nightly.is_running(repo):
            raise JobRefused(
                f"Repo {repo} is being indexed right now - start after the index pass finishes.")
    pasteable = [c if callable(c) else ["kopia"] + _repo_args(repo) + c for c in cmds]
    job = _new_job(kind, repo, label or _default_label(kind, params, meta), meta, cmds,
                   pasteable, on_done)
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
    if job["mutating"]:
        from . import reindexq  # lazy: reindexq reads the job registry
        reindexq.touch(repo)
    threading.Thread(target=_run, args=(job, cmds), daemon=True).start()
    log.warning("JOB START %s repo=%s %s", kind, repo, job["label"])
    return job


def _stream_one(job: dict, cmd: list[str], lf) -> tuple[int, str]:
    """Run one command, stamping every line into the ring buffer and appending the raw line
    to the job log. Returns (rc, last stderr line) - the last line feeds the error text."""
    result: dict = {}
    job["_stream"] = result  # Stop reaches the live child the moment it exists - a command
    last_err = ""            # that goes quiet before its first line must still be stoppable
    parser = DRY_PARSERS.get(job["kind"]) if job["dry"] else None
    for stream, line in kopia_job_stream(job["repo"], cmd, result):
        if not line:
            continue
        job["buffer"].append(time.strftime("%H:%M:%S ") + line)
        job["line_count"] += 1
        lf.write(line + "\n")
        if parser is not None:
            parser(line, job["dry_summary"])
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
                if job["kind"] == "policy_write" and job["errors"] and not callable(cmd):
                    # A rule write failed: the delete waits for the user's decision.
                    job["held"] = True
                    say(job, lf, "A rule was not added. The delete did not run.")
                    break
                lf.write(f"\n--- command {i}/{len(cmds)} ---\n")
                lf.flush()
                if callable(cmd):
                    chunk_rc, last_err = cmd(job, lf)
                else:
                    chunk_rc, last_err = _stream_one(job, cmd, lf)
                job["done"] = i
                if chunk_rc != 0:
                    job["errors"] += 1
                    rc = chunk_rc
                    job["rc"] = chunk_rc
                    job["error"] = f"exit {chunk_rc}: {last_err[:280]}" if last_err else f"exit {chunk_rc}"
            if job["errors"] == 0 and rc == 0:
                job["rc"] = 0
            if job["kind"] == "maintenance" and not job["stop_requested"]:
                _record_freed(job, lf)
        if job["stop_requested"]:
            job["state"] = "stopped"
        elif job["kind"] in ("restore", "maintenance", "policy_write",
                             "encryption_switch") and job["rc"] not in (None, 0):
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
        _record_history(job)
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


def folder_size(path: str) -> int | None:
    """Folder size from the disk lookup; None when it cannot answer in time."""
    from . import disklookup
    try:
        return disklookup.recursive_size(path)
    except Exception:  # noqa: BLE001 - the freed figure is optional
        return None


def _record_freed(job: dict, lf) -> None:
    """Repository folder size before and after a maintenance run (filesystem repos)."""
    detail = job.get("detail") or {}
    path, before = detail.get("measure_path"), detail.get("size_before")
    if not path or before is None:
        return
    time.sleep(3)  # Everything picks deletions up from the change journal
    after = folder_size(path)
    if after is None:
        return
    job["freed"] = {"before": before, "after": after, "freed": max(0, before - after)}
    say(job, lf, f"Repository folder: {before:,} -> {after:,} bytes")


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


def terminate_all() -> None:
    """App shutdown: end every running job's kopia child. A restart must not orphan a
    running action the registry no longer knows about - same terminate semantics as Stop
    (the child dies; the log keeps whatever it wrote)."""
    with _JOBS_LOCK:
        running = [j for j in _JOBS.values() if j["state"] == "running"]
    for job in running:
        job["stop_requested"] = True
        proc = (job.get("_stream") or {}).get("proc")
        if proc is not None and proc.poll() is None:
            _terminate(proc)
        log.warning("JOB SHUTDOWN %s %s repo=%s terminated", job["job_id"], job["kind"], job["repo"])


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
    if job["dry_summary"]:
        out["summary"] = dict(job["dry_summary"])
    # The meta rides the record's "detail", which is not serialized; the note is the one piece the
    # UI must see (e.g. "the index changed since your preview").
    note = (job.get("detail") or {}).get("note")
    if note:
        out["note"] = note
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


HISTORY_TAIL_LINES = 400


def history_status(job_id: str, after: int = 0) -> dict | None:
    """A finished job from before the last restart: its saved record plus the tail of its log."""
    rec = next((r for r in read_job_history(limit=HISTORY_FILE_LINES) if r.get("job_id") == job_id), None)
    if rec is None:
        return None
    lines: list[str] = []
    try:
        with open(rec.get("log_path") or "", encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()][-HISTORY_TAIL_LINES:]
    except OSError:
        pass
    lines = [("--:--:-- " + ln) for ln in lines]   # the panel expects a time prefix
    return {**rec, "results": [], "held": False, "freed": None, "target": None, "commands_text": "",
            "from_history": True, "line_count": len(lines), "cursor": len(lines), "skipped": 0,
            "lines": lines[max(0, int(after)):]}


def list_jobs() -> list[dict]:
    """Every known job, newest first - the header chip reads running entries from here."""
    with _JOBS_LOCK:
        return [_serialize(j) for j in
                sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)]
