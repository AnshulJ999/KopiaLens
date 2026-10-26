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
import re
import threading
import time
import uuid
from collections import deque

from config import DATA_DIR, is_demo

from . import audit, filecrypto
from .applog import get_logger
from .kopia import (
    _kopia_exe,
    _parse_delete_line,
    _parse_remove_line,
    _quote_arg,
    _repo_args,
    kopia_job_stream,
    maintenance_run_command,
    prune_commands,
    remove_files_chunked_commands,
    restore_command_full,
    snapshot_create_command,
    snapshot_delete_commands,
    snapshot_estimate_command,
    snapshot_pin_command,
    snapshot_verify_command,
    source_delete_commands,
    move_history_command,
)

log = get_logger("jobs")

BUFFER_LINES = 2000        # in-memory ring; the full raw output always lands in the log file
HISTORY_JOBS = 50          # finished jobs kept in memory (their log files persist regardless)
HISTORY_FILE_LINES = 1000  # on-disk history cap: a restart must not blank the Activity view
EVENT_FORCE_AFTER = 300.0  # seconds a graceful stop (Ctrl+Break / SIGTERM) gets before Stop kills

KINDS = {
    "remove_files": {"label": "Removing files", "mutating": True},
    "prune_versions": {"label": "Pruning versions", "mutating": True},
    "snapshot_delete": {"label": "Deleting snapshots", "mutating": True},
    "maintenance": {"label": "Running maintenance", "mutating": True},
    "restore": {"label": "Restoring", "mutating": False},
    "policy_write": {"label": "Saving policies", "mutating": False},
    "snapshot_create": {"label": "Taking a snapshot", "mutating": False},
    "snapshot_pin": {"label": "Changing pins", "mutating": False},
    "snapshot_estimate": {"label": "Estimating a snapshot", "mutating": False},
    "source_delete": {"label": "Deleting a source", "mutating": True},
    "move_history": {"label": "Merging snapshot histories", "mutating": True},
    # Read-only: reads every directory and, optionally, a share of the files.
    "snapshot_verify": {"label": "Verifying backups", "mutating": False},
    # Local-only (): rewrites KopiaLens's own files, never the kopia repository.
    # Install-wide level change: every repo's generation, the file layer, Backblaze. Mutating,
    # but tolerant - a busy repo is deferred and reported rather than failing the job.
    "encryption_level": {"label": "Changing encryption level", "mutating": True},
    # Read-only but heavy: lists every blob in storage. Started only by the user, never on a timer.
    "blob_stats": {"label": "Reading blob stats", "mutating": False},
}

AUDIT_ACTION = {
    "remove_files": "remove-files",
    "prune_versions": "prune-versions",
    "snapshot_delete": "snapshot-delete",
    "maintenance": "maintenance",
    "restore": "restore",
    "policy_write": "policy-write",
    "snapshot_create": "snapshot-create",
    "snapshot_pin": "snapshot-pin",
    "snapshot_estimate": "snapshot-estimate",
    "source_delete": "source-delete",
    "move_history": "move-history",
    "snapshot_verify": "snapshot-verify",
    "encryption_level": "encryption-level",
    "blob_stats": "blob-stats",
}


def say(job: dict, lf, text: str) -> None:
    """One output line from a Python step (policy writes), same shape as kopia's lines."""
    job["buffer"].append(time.strftime("%H:%M:%S ") + text)
    job["line_count"] += 1
    lf.write(text + "\n")
    lf.flush()


def job_lines(job: dict) -> list[str]:
    """A job's buffered output lines without their time stamps."""
    return [re.sub(r"^\d\d:\d\d:\d\d ", "", ln) for ln in job["buffer"]]

# Dry-run jobs build the same summary the synchronous dry-run endpoints report; the parsers
# are shared with them (one home in kopia.py, fed per streamed line here).
DRY_PARSERS = {
    "remove_files": _parse_remove_line,
    "prune_versions": _parse_remove_line,
    "snapshot_delete": _parse_delete_line,
    "source_delete": _parse_delete_line,
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
        "log_path": job["log_path"], "counters": job.get("counters") or {},
    }
    try:
        line = json.dumps(rec, ensure_ascii=False)
        with _HISTORY_LOCK:
            path = _history_path()
            filecrypto.append_line(path, line)
            if os.path.getsize(path) > HISTORY_FILE_LINES * 140:  # heuristic lines cap
                lines = filecrypto.read_lines(path)
                if len(lines) > HISTORY_FILE_LINES:
                    filecrypto.write_lines(path, lines[-HISTORY_FILE_LINES:])
    except Exception as exc:  # noqa: BLE001 - history is best-effort, the job already ran
        log.warning("job history write failed: %s", exc)


def read_job_history(limit: int = 200) -> list[dict]:
    """The last ``limit`` finished jobs from disk, newest first - the part of Activity that
    survives restarts."""
    try:
        with _HISTORY_LOCK:
            lines = filecrypto.read_lines(_history_path())
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max(1, limit):]:
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
    elif kind == "prune_versions":
        items = params.get("items") or []
        cmds = prune_commands(repo, [it for it in items if it.get("obj")], commit=not dry)
        whole = [i for it in items if not it.get("obj") for i in it.get("snapshot_ids") or []]
        if whole:  # the folder is the source's root there: delete those snapshots
            dels = snapshot_delete_commands(repo, whole, batch=True)
            cmds += [c[:-1] for c in dels] if dry else dels
        meta = {"versions": len(items), "path": params.get("full_path") or "",
                "snapshots": sum(len(it.get("snapshot_ids") or []) for it in items), "dry": dry}
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
    elif kind == "blob_stats":
        cmds, meta = [[_kopia_exe()] + _repo_args(repo) + ["blob", "stats"]], {"dry": False}
    elif kind == "snapshot_pin":
        ids = params.get("snapshot_ids") or []
        name = params.get("pin_name") or "do-not-delete"
        cmds = [snapshot_pin_command(repo, ids, remove=bool(params.get("remove")), pin_name=name,
                                     rename_from=params.get("rename_from") or "")] if ids else []
        meta = {"snapshots": len(ids), "remove": bool(params.get("remove")), "dry": False,
                "pin": name, "rename_from": params.get("rename_from") or ""}
    elif kind == "snapshot_estimate":
        cmds, meta = [snapshot_estimate_command(repo, params["path"])], {"path": params["path"], "dry": False}
    elif kind == "source_delete":
        cmds = source_delete_commands(repo, params["source"], bool(params.get("delete_policy")), dry)
        meta = {"source": params["source"], "policy": bool(params.get("delete_policy")), "dry": dry}
    elif kind == "move_history":
        cmds = [move_history_command(repo, params["src"], params["dst"], dry)]
        meta = {"src": params["src"], "dst": params["dst"], "dry": dry}
    elif kind == "snapshot_verify":
        cmds = [snapshot_verify_command(repo, params.get("sources") or [], float(params.get("percent") or 0))]
        meta = {"sources": len(params.get("sources") or []), "percent": float(params.get("percent") or 0), "dry": False}
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
    elif kind == "encryption_level":
        # One Python step: move the whole install to a level (index generations, the file
        # layer, Backblaze) with the same verified rewrites the per-repo switch uses.
        level = str(params.get("level") or "").lower()
        if level not in ("off", "index", "full"):
            raise JobRefused(f"unknown encryption level: {level or '(none)'}")

        def _level_step(job: dict, lf) -> tuple[int, str]:
            from ..index import keyswitch
            say_ = lambda text: say(job, lf, text)          # noqa: E731
            try:
                out = keyswitch.apply_level(level, say=say_)
                job["level"] = out
                if out.get("deferred"):
                    say_(f"deferred: {', '.join(out['deferred'])} - run it again to finish")
                return 0, ""
            except Exception as exc:  # noqa: BLE001 - the runner reports it like any other step
                say_(f"failed: {exc}")
                return 1, str(exc)[:280]

        return [_level_step], {"level": level, "dry": False}
    else:
        raise JobRefused(f"unknown job kind: {kind}")
    # The builders return PASTEABLE commands: [kopia] + _repo_args(repo) + subcommand. The
    # streamer consumes BARE args via _base_command, which re-adds the kopia prefix AND the repo
    # args. Stripping only the binary left the repo args in, and _base_command added them a
    # second time - invisible on repo 1 (empty args list), but repo 2 ran with its --config-file
    # twice. Strip the exact prefix; a mismatch means a builder changed shape and would double
    # args, so it fails loudly instead of silently corrupting the command.
    prefix = [_kopia_exe()] + _repo_args(repo)
    stripped = []
    for c in cmds:
        if c[:len(prefix)] != prefix:
            raise RuntimeError(f"job builder output lacks the kopia+repo-args prefix: {c[:4]}")
        stripped.append(c[len(prefix):])
    if kind == "snapshot_estimate":
        from . import engine  # lazy: engine reads the job history
        # KopiaLens's own server reports live counts; the command line only names folders.
        if engine.running(repo):
            stripped = [_server_estimate(repo, params["path"], stripped[0])]
    if kind == "snapshot_create" and params.get("via_server"):
        from . import engine
        # Snapshot now on a repository KopiaLens runs: its own server takes it, the job follows it.
        if engine.running(repo):
            stripped = [_server_snapshot(repo, params["source"], stripped[0])]
    return stripped, meta


def _count(n: int, one: str, many: str | None = None) -> str:
    """``_count(1, "backup")`` -> "1 backup", ``_count(3, "backup")`` -> "3 backups"."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _default_label(kind: str, params: dict, meta: dict) -> str:
    if kind == "remove_files":
        src = params.get("source")
        scope = ("everywhere" if not src
                 else _count(len(src) if isinstance(src, list) else 1, "source"))
        what = "name" if params.get("flag") == "--filename" else "version"
        return f"{_count(meta['items'], what)} - {scope}" + (" - dry run" if meta["dry"] else "")
    if kind == "snapshot_delete":
        return _count(meta["snapshots"], "snapshot") + (" - dry run" if meta["dry"] else "")
    if kind == "prune_versions":
        return (f"{_count(meta['versions'], 'version')} from {_count(meta['snapshots'], 'snapshot')}"
                + (" - dry run" if meta["dry"] else ""))
    if kind == "maintenance":
        return ("full maintenance" if meta["full"] else "quick maintenance") + (
            ", safety none" if meta.get("safety") == "none" else "")
    if kind == "restore":
        target = meta["target"]
        return f"to {target}"
    if kind == "snapshot_create":
        return meta["path"]
    if kind == "snapshot_pin":
        if meta.get("rename_from"):
            return f"rename pin {meta['rename_from']} -> {meta['pin']} on {_count(meta['snapshots'], 'snapshot')}"
        return f"{'unpin' if meta['remove'] else 'pin'} {_count(meta['snapshots'], 'snapshot')}"
    if kind == "snapshot_estimate":
        return meta["path"]
    if kind == "source_delete":
        return meta["source"] + (" and its policy" if meta["policy"] else "") + (" - dry run" if meta["dry"] else "")
    if kind == "move_history":
        return f"{meta['src']} -> {meta['dst']}" + (" - dry run" if meta["dry"] else "")
    if kind == "snapshot_verify":
        what = _count(meta["sources"], "source") if meta["sources"] else "all sources"
        return what + (f", reading {meta['percent']:g}% of files" if meta["percent"] else "")
    if kind == "policy_write":
        n = meta["writes"]
        base = f"{n} polic{'y' if n == 1 else 'ies'}"
        return base + (", then delete" if meta.get("then") else "")
    if kind == "encryption_level":
        return f"encryption level -> {meta.get('level')}"
    if kind == "blob_stats":
        return "blob stats"
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
        "progress_pct": None, "progress_text": "",   # a snapshot's live percent and time left
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
    from . import checkin
    checkin.note_feature(kind)
    if (KINDS[kind]["mutating"] or meta.get("then")) and not meta.get("dry"):
        from . import nightly  # lazy: avoids an import cycle at module load
        if nightly.is_running(repo):
            raise JobRefused(
                f"Repo {repo} is being indexed right now - start after the index pass finishes.")
    pasteable = [c if callable(c) else [_kopia_exe()] + _repo_args(repo) + c for c in cmds]
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
    if job["mutating"] and kind != "encryption_level" and not is_demo(repo):
        # An encryption rewrite is not a delete: the index's CONTENT is unchanged, so an
        # incremental pass would burn a nightly for nothing.
        from . import reindexq  # lazy: reindexq reads the job registry
        reindexq.touch(repo)
    threading.Thread(target=_run, args=(job, cmds), daemon=True).start()
    log.warning("JOB START %s repo=%s %s", kind, repo, job["label"])
    return job


def kopia_log_files(job: dict) -> list[str]:
    """kopia's own log files for a job's commands, in run order (``--log-file X.log`` makes kopia
    write ``X.0.log``)."""
    stem = job["log_path"][:-4] + "-kopia-"
    folder = os.path.dirname(stem)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    base = os.path.basename(stem)
    found = [n for n in names if n.startswith(base) and n.endswith(".log")]
    return [os.path.join(folder, n) for n in sorted(found, key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])]


_KOPIA_LOG_SETTLE = 15   # seconds after a job ends before its kopia log is rewritten (a viewer's last poll)


def encrypt_kopia_logs(job: dict | None = None) -> None:
    """At the Full level, kopia's own job logs (kopia writes them in the clear) are encrypted once
    their job has ended: ``job``'s, or with no job every one whose job isn't running (a job that
    died before it could)."""
    if not filecrypto.available():
        return
    if job is not None:
        paths = kopia_log_files(job)
    else:
        from .applog import JOB_LOG_DIR
        with _JOBS_LOCK:
            live = tuple(os.path.basename(j["log_path"][:-4]) + "-kopia-"
                         for j in _JOBS.values() if j["state"] == "running")
        try:
            names = os.listdir(JOB_LOG_DIR)
        except OSError:
            return
        paths = [os.path.join(JOB_LOG_DIR, n) for n in names
                 if "-kopia-" in n and n.endswith(".log") and not n.startswith(live)]
    for path in paths:
        filecrypto.encrypt_line_file(path)


def _run_args(job: dict, index: int) -> list[str]:
    """Flags added when a job's command runs (not in the pasteable command): kopia's full log in the
    job's own log folder, and the snapshot as JSON so its numbers can fill the task's counters."""
    if is_demo(job["repo"]):
        return []
    extra = ["--log-file", f"{job['log_path'][:-4]}-kopia-{index}.log", "--file-log-level=info"]
    if job["kind"] == "snapshot_create":
        # kopia prints its progress line only to a terminal unless asked.
        extra += ["--json", "--progress", "--progress-update-interval=1s"]
    return extra


def _snapshot_counters(job: dict, manifest: dict) -> dict:
    """The task panel's Counter / Value table for a snapshot taken on the command line: the
    snapshot's totals plus kopia's run stats (read back from the listing; --json leaves them out)."""
    summ = (manifest.get("rootEntry") or {}).get("summ") or {}
    stats = {}
    path = (manifest.get("source") or {}).get("path") or ""
    try:
        from .kopia import kopia as _kopia
        res = _kopia(job["repo"], ["snapshot", "list", path, "--json"])
        if res.returncode == 0:
            hit = next((m for m in json.loads(res.stdout or "[]") if m.get("id") == manifest.get("id")), None)
            stats = (hit or {}).get("stats") or {}
    except Exception as exc:  # noqa: BLE001 - the counters are a bonus; the snapshot is already saved
        log.warning("snapshot stats for %s: %s", manifest.get("id"), exc)
    def c(value, units="", level=""):
        return {"value": int(value or 0), "units": units, "level": level}
    # Both count the same failed entries (the root summary's numFailed totals the whole tree).
    errors = int(summ.get("numFailed") or stats.get("errorCount") or 0)
    out = {"Size": c(summ.get("size"), "bytes"), "Files": c(summ.get("files")), "Folders": c(summ.get("dirs"))}
    if stats:
        out.update({
            "Hashed Files": c(stats.get("nonCachedFiles")), "Cached Files": c(stats.get("cachedFiles")),
            "Excluded Files": c(stats.get("excludedFileCount")), "Excluded Directories": c(stats.get("excludedDirCount")),
            "Excluded Bytes": c(stats.get("excludedTotalSize"), "bytes"),
            "Ignored Errors": c(stats.get("ignoredErrorCount"), level="warning"),
        })
    out["Errors"] = c(errors, level="error")
    return out


_PROGRESS_RE = re.compile(
    r"^\s*\S?\s*(\d+) hashing, (\d+) hashed \(([^)]*)\), (\d+) cached \(([^)]*)\), uploaded ([^,(]+?)"
    r"(?: \((\d+) fatal errors\))?(?: \((\d+) errors ignored\))?"
    r"(?:, estimated ([^(]+?) \(([\d.]+)%\) (.+?) left)?(?:, estimating\.\.\.)?\s*$")
_UNITS = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}


def _kopia_bytes(text: str) -> int:
    """kopia's own size text ("9.2 MB", "1.5 GiB") back to bytes."""
    m = re.match(r"\s*([\d.]+)\s*([KMGTPE]?)(i?)B", text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * (1024 if m.group(3) else 1000) ** _UNITS[m.group(2)])


def _live_progress(job: dict, line: str) -> bool:
    """A snapshot's progress line: live counters, percent and time left. True when it was one."""
    m = _PROGRESS_RE.match(line)
    if not m:
        return False
    def c(value, units="", level=""):
        return {"value": int(value or 0), "units": units, "level": level}
    # kopia server's own counter names (snapshot/upload/upload_progress.go), plus what only the
    # command line reports.
    hashed, cached = _kopia_bytes(m.group(3)), _kopia_bytes(m.group(5))
    counters = {
        "Hashing Files": c(m.group(1)), "Hashed Files": c(m.group(2)), "Hashed Bytes": c(hashed, "bytes"),
        "Cached Files": c(m.group(4)), "Cached Bytes": c(cached, "bytes"),
        "Processed Files": c(int(m.group(2) or 0) + int(m.group(4) or 0)),
        "Processed Bytes": c(hashed + cached, "bytes"),
        "Uploaded Bytes": c(_kopia_bytes(m.group(6)), "bytes"),
        "Errors": c(m.group(7), level="error"), "Ignored Errors": c(m.group(8), level="warning"),
    }
    if m.group(9):
        counters["Estimated Bytes"] = c(_kopia_bytes(m.group(9)), "bytes")
        job["progress_pct"] = min(100.0, float(m.group(10)))
        job["progress_text"] = f"{m.group(10)}% · {m.group(11)} left"
    job["counters"] = counters
    job["_last_progress"] = line.strip()
    return True


def _kopia_size(n: int) -> str:
    """kopia's own size text (units.BytesString, decimal)."""
    f, i = float(n or 0), 0
    while f >= 1000 and i < 6:
        f, i = f / 1000, i + 1
    return f"{(f'{f:.1f}').rstrip('0').rstrip('.')} {['', 'K', 'M', 'G', 'T', 'P', 'E'][i]}B"


def _server_estimate(repo: str, path: str, cli_cmd: list[str]):
    """An estimate run by KopiaLens's own kopia server: live counters while it scans, then the same
    summary lines the command line prints. Falls back to the command line if the server can't."""
    def run(job: dict, lf) -> tuple[int, str]:
        from . import engine
        srv = engine.running(repo)
        try:
            r = srv.ui.post("/api/v1/estimate", json={"root": path, "maxExamplesPerBucket": 3})
            r.raise_for_status()
            tid = r.json()["id"]
        except Exception as exc:  # noqa: BLE001 - the command line still works
            log.warning("server estimate for %s: %s - using the command line", path, exc)
            return _stream_one(job, cli_cmd, lf)
        engine.hide_task(repo, srv.tag, tid)          # the job is the task; don't list it twice
        cancelled, t = False, {}
        while True:
            if job["stop_requested"] and not cancelled:
                cancelled = True
                try:
                    srv.ui.post(f"/api/v1/tasks/{tid}/cancel", json={})
                except Exception:  # noqa: BLE001 - the server may already be gone
                    break
            try:
                t = srv.ui.get(f"/api/v1/tasks/{tid}").json()
            except Exception as exc:  # noqa: BLE001
                return 1, f"KopiaLens's kopia server stopped answering: {exc}"
            job["counters"] = {k: {"value": int(v.get("value") or 0), "units": v.get("units") or "",
                                   "level": v.get("level") or ""} for k, v in (t.get("counters") or {}).items()}
            if t.get("endTime"):
                break
            time.sleep(0.5)
        try:
            entries = (srv.ui.get(f"/api/v1/tasks/{tid}/logs").json() or {}).get("logs") or []
        except Exception:  # noqa: BLE001
            entries = []
        c = {k: v["value"] for k, v in job["counters"].items()}
        lines = [engine._log_text(e).split(" ", 1)[-1] for e in entries]   # the job adds its own time
        if (t.get("status") or "").upper() == "SUCCESS":
            lines += [f"Snapshot includes {c.get('Files', 0)} file(s), total size {_kopia_size(c.get('Bytes', 0))}",
                      f"Snapshot excludes {c.get('Excluded Files', 0)} file(s), total size {_kopia_size(c.get('Excluded Bytes', 0))}"
                      if c.get("Excluded Files") else "Snapshot excludes no files.",
                      f"Snapshot excludes {c.get('Excluded Directories', 0)} directories."
                      if c.get("Excluded Directories") else "Snapshot excludes no directories."]
            if c.get("Errors"):
                lines.append(f"Encountered {c['Errors']} error(s).")
        for line in lines:
            job["buffer"].append(time.strftime("%H:%M:%S ") + line)
            job["line_count"] += 1
            lf.write(line + "\n")
        lf.flush()
        ok = (t.get("status") or "").upper() == "SUCCESS"
        return (0, "") if ok else (1, t.get("errorMessage") or "The estimate was cancelled.")
    return run


def _server_snapshot(repo: str, source: str, cli_cmd: list[str]):
    """Snapshot now through KopiaLens's own kopia server: trigger it, find the server's task (the
    trigger returns no id) and follow it - counters, log lines, Stop. Tasks lists the server's task,
    not this job. Falls back to the command line if the server can't start it."""
    def _snap_tasks(srv) -> list[dict]:
        tasks = (srv.ui.get("/api/v1/tasks").json() or {}).get("tasks") or []
        # kopia names a snapshot task "<user@host:path> at <time>" (internal/server/server.go).
        return [t for t in tasks if t.get("kind") == "Snapshot"
                and str(t.get("description") or "").startswith(source + " at ")]

    def run(job: dict, lf) -> tuple[int, str]:
        from . import engine
        srv = engine.running(repo)
        try:
            before = _snap_tasks(srv)
            live = next((t for t in before if not t.get("endTime")), None)
            if live is None:
                engine.trigger_snapshot(repo, source)
                seen = {t.get("id") for t in before}
                deadline = time.time() + 30
                while live is None and time.time() < deadline:   # after a Stop too: found, it is cancelled
                    time.sleep(0.5)
                    live = next((t for t in _snap_tasks(srv) if t.get("id") not in seen), None)
            if live is None:
                return 1, "KopiaLens's kopia server didn't start the snapshot."
            tid = live["id"]
        except Exception as exc:  # noqa: BLE001 - the command line still works
            log.warning("server snapshot for %s: %s - using the command line", source, exc)
            return _stream_one(job, cli_cmd, lf)
        job["server_task"] = job["detail"]["server_task"] = f"engine:{repo}:{srv.tag}.{tid}"
        cancelled, t, seen = False, {}, []
        while True:
            if job["stop_requested"] and not cancelled:
                cancelled = True
                try:
                    srv.ui.post(f"/api/v1/tasks/{tid}/cancel", json={})
                except Exception:  # noqa: BLE001 - the server may already be gone
                    break
            try:
                t = srv.ui.get(f"/api/v1/tasks/{tid}").json()
                entries = (srv.ui.get(f"/api/v1/tasks/{tid}/logs").json() or {}).get("logs") or []
            except Exception as exc:  # noqa: BLE001
                return 1, f"KopiaLens's kopia server stopped answering: {exc}"
            job["counters"] = {k: {"value": int(v.get("value") or 0), "units": v.get("units") or "",
                                   "level": v.get("level") or ""} for k, v in (t.get("counters") or {}).items()}
            new, gap = engine._new_log_entries(seen, entries)   # kopia keeps the newest 1000 lines
            seen = entries
            lines = (["… kopia dropped some log lines before KopiaLens could read them"] if gap else []) + \
                [engine._log_text(e).split(" ", 1)[-1] for e in new]
            for line in lines:
                job["buffer"].append(time.strftime("%H:%M:%S ") + line)
                job["line_count"] += 1
                lf.write(line + "\n")
            lf.flush()
            if t.get("endTime"):
                break
            time.sleep(1)
        ok = (t.get("status") or "").upper() == "SUCCESS"
        return (0, "") if ok else (1, t.get("errorMessage") or "The snapshot was cancelled.")
    return run


def _stream_one(job: dict, cmd: list[str], lf) -> tuple[int, str]:
    """Run one command, stamping every line into the ring buffer and appending the raw line
    to the job log. Returns (rc, last stderr line) - the last line feeds the error text."""
    result: dict = {"extra_args": _run_args(job, job.get("_cmd_index") or 1)}
    job["_stream"] = result  # Stop reaches the live child the moment it exists - a command
    last_err = ""            # that goes quiet before its first line must still be stoppable
    parser = DRY_PARSERS.get(job["kind"]) if job["dry"] else None
    for stream, line in kopia_job_stream(job["repo"], cmd, result):
        if not line:
            continue
        if job["kind"] == "snapshot_create" and stream == "stdout" and line.startswith("{"):
            try:                                   # --json: the saved snapshot, one line
                manifest = json.loads(line)
                job["snapshot_id"] = manifest.get("id")
                live = {k: v for k, v in (job.get("counters") or {}).items()
                        if k in ("Hashed Bytes", "Cached Bytes", "Uploaded Bytes")}
                job["counters"] = {**live, **_snapshot_counters(job, manifest)}
                job["progress_pct"], job["progress_text"] = 100.0, ""
                line =f"Saved snapshot {manifest.get('id')}"
            except ValueError:
                pass
        if job["kind"] == "snapshot_create" and stream == "stderr" and _live_progress(job, line):
            continue                               # counters, not output lines (one per second)
        if job["kind"] == "snapshot_estimate" and stream == "stderr" and line.startswith("Analyzing ") and line.endswith("..."):
            job["_folders"] = job.get("_folders", 0) + 1     # one line per folder: progress, not output
            name = line[10:-3]                               # relative to the folder; "." is the folder itself
            if name in ("", "."):
                name = (job.get("detail") or {}).get("path") or "the folder"
            job["progress_text"] = f"Scanning {name} · {job['_folders']:,} folders"
            continue
        job["buffer"].append(time.strftime("%H:%M:%S ") + line)
        job["line_count"] += 1
        lf.write(line + "\n")
        if parser is not None:
            parser(line, job["dry_summary"])
        if stream == "stderr" and line.strip():
            last_err = line
    if job.get("_last_progress"):
        lf.write(job.pop("_last_progress") + "\n")   # the final counts, once
    lf.flush()
    return int(result.get("returncode") or 0), last_err


def _run(job: dict, cmds: list[list[str]]) -> None:
    """The job thread: chunk by chunk, stopping before the next chunk once stop_requested is
    set (a stop landing between the check and the spawn runs at most one extra batch -
    bounded and per-snapshot atomic)."""
    job["started"] = time.time()
    rc = 0
    try:
        with filecrypto.open_text(job["log_path"]) as lf:
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
                    job["_cmd_index"] = i
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
                             "encryption_level") and job["rc"] not in (None, 0):
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
        if filecrypto.available() and not is_demo(job["repo"]):
            threading.Timer(_KOPIA_LOG_SETTLE, encrypt_kopia_logs, args=(job,)).start()
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
    """Folder size from the disk lookup, counted now (never a remembered size); None when it
    cannot answer in time."""
    from . import disklookup
    try:
        return disklookup.recursive_size(path, fresh=True)
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
    """Ask a running job to stop: request the stop, then end the CURRENT kopia process (SIGTERM on
    Linux/macOS, Ctrl+Break on Windows when deliverable, TerminateProcess otherwise). Chunked jobs then also
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
        if _send_sigterm(proc) or _send_ctrl_break(proc):
            how = "event"  # force timer armed inside; kopia gets its grace window
        else:
            _terminate(proc)
            how = "terminated"
    log.warning("JOB STOP %s %s how=%s", job_id, job["kind"], how)
    return {"ok": True, "how": how, "state": job["state"]}


def _force_later(proc) -> None:
    t = threading.Timer(EVENT_FORCE_AFTER, lambda: _terminate(proc, force=True))
    t.daemon = True
    t.start()


def _send_sigterm(proc) -> bool:
    """Linux/macOS: SIGTERM is kopia's graceful stop (the same handler as Ctrl+C; a snapshot saves
    a checkpoint). Kill it if it is still running after the grace window."""
    import sys
    if sys.platform == "win32":
        return False
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 - already gone
        return False
    if proc.poll() is None:
        _force_later(proc)
    return True


def _send_ctrl_break(proc) -> bool:
    """Try to deliver a real CTRL_BREAK to the child's own console - the graceful path that
    kopia's create/mount/migrate handlers respond to. The event goes to kopia's own process group
    (jobs start it with CREATE_NEW_PROCESS_GROUP): group 0 would reach every process on that
    console, the server included, which then exits. Returns False when undeliverable, so the
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
            ok = bool(k.GenerateConsoleCtrlEvent(1, int(proc.pid)))  # 1 = CTRL_BREAK_EVENT, to kopia's group only
            k.SetConsoleCtrlHandler(None, False)
            k.FreeConsole()
            k.AttachConsole(-1)  # back to our parent's console (dev mode); fails harmlessly under pythonw
        else:
            k.AttachConsole(-1)
        if ok and proc.poll() is None:
            _force_later(proc)
        return ok
    except Exception:  # noqa: BLE001 - the graceful path must never break Stop
        return False


def _terminate(proc, force: bool = False) -> None:
    try:
        if proc.poll() is None:
            if force:
                proc.kill()
                return
            proc.terminate()  # TerminateProcess on Windows, SIGTERM elsewhere
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
           if k not in ("buffer", "_stream", "on_done", "detail", "_cmd_index", "_last_progress", "_folders")}
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
    path = rec.get("log_path") or ""
    if path and not os.path.isfile(path):   # a moved data folder or a mirrored demo copy: same name here
        from .applog import JOB_LOG_DIR
        path = os.path.join(JOB_LOG_DIR, os.path.basename(path))
    lines = filecrypto.read_lines(path)[-HISTORY_TAIL_LINES:] if path else []
    lines = [("--:--:-- " + ln) for ln in lines]   # the panel expects a time prefix
    return {**rec, "log_path": path, "results": [], "held": False, "freed": None, "target": None, "commands_text": "",
            "from_history": True, "line_count": len(lines), "cursor": len(lines), "skipped": 0,
            "lines": lines[max(0, int(after)):]}


def list_jobs() -> list[dict]:
    """Every known job, newest first - the header chip reads running entries from here."""
    with _JOBS_LOCK:
        return [_serialize(j) for j in
                sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)]
