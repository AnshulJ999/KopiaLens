"""Kopia subprocess boundary.

Most calls are read-only and protected by the same allowlist as
``kopiatoolkit.py``. Mount is allowed as an explicit operational action because it
does not mutate the repository. Destructive verbs (remove-files, snapshot delete,
restore) execute through dedicated functions that log + audit every run; their
dry-run counterparts run the same commands without the mutating flag.
"""

from __future__ import annotations

import os
import queue
import re
import string
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from config import DATA_DIR, REPOS

from . import audit
from .applog import get_logger

log = get_logger("kopia")

ALLOWED = {
    ("repository", "status"),
    ("content", "list"),
    ("content", "show"),
    ("content", "stats"),
    ("snapshot", "list"),
    ("diff",),
    ("ls",),
    ("list",),
    ("policy", "list"),
    ("policy", "show"),
    ("policy", "export"),  # read-only on the repo (--to-file writes a local file we control)
    ("blob", "stats"),
    ("manifest", "list"),
    ("manifest", "show"),
    ("maintenance", "info"),  # read-only ("maintenance run" stays refused - it's not allowlisted)
    ("cache", "info"),
    ("show",),
}

MOUNT_ALLOWED = {("mount",)}

KOPIA_CALLS = 0
KOPIA_TIME = 0.0


@dataclass
class KopiaResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


@dataclass
class KopiaProcess:
    args: list[str]
    pid: int


def _tokens(args: Iterable[str]) -> list[str]:
    return [a for a in args if not str(a).startswith("-")]


def check_readonly(args: list[str]) -> None:
    toks = _tokens(args)
    if not toks:
        raise PermissionError("empty kopia command")
    if (toks[0],) in ALLOWED or tuple(toks[:2]) in ALLOWED:
        return
    raise PermissionError(f"REFUSED non-readonly kopia command: {' '.join(toks[:2])}")


def _repo_args(repo: str) -> list[str]:
    """Validated CLI args for a repo. EVERY kopia command builder must start here.

    A non-kopia repo carries NO args (the Backblaze one is a local SQLite index), so `repo="bz"`
    does not error on its own - it silently produces a valid command against the DEFAULT kopia
    repository. On `snapshot delete` or `restore` that is the wrong repository.

    Guarding only `_base_command` was NOT enough and the first attempt at this got it wrong: nine
    builders concatenate the args themselves, and they include every destructive path (snapshot
    delete x2, remove-files x2, restore x2, pin, diff, maintenance). Grep for `REPOS[` before
    assuming a new builder is covered - the type system will not tell you.
    """
    if repo not in REPOS:
        raise ValueError(f"unknown repo: {repo}")
    if REPOS[repo].get("kind", "kopia") != "kopia":
        raise ValueError(f"repo {repo!r} is not a kopia repo - refusing to build a kopia command")
    return list(REPOS[repo]["args"])


def _base_command(repo: str, args: list[str], progress: bool = False) -> list[str]:
    """``progress=True`` omits --no-progress: the job streamer exists to show kopia's live
    output, so it must not ask kopia to be quiet. Every other caller keeps the flag."""
    return ["kopia"] + _repo_args(repo) + args + ([] if progress else ["--no-progress"])


DEFAULT_TIMEOUT = 3600.0  # generous: `content list -l` on a large repo legitimately runs minutes
TIMEOUT_RC = -9           # distinct from any kopia exit code, so callers can tell a timeout apart


def _timeout() -> float:
    """Ceiling on one kopia call. 0/negative disables it (there is no good reason to, but a
    user with a genuinely slower repo should not have to patch code)."""
    try:
        from ..settings import load_settings
        return float(load_settings().get("kopialens", {}).get("kopia_timeout", DEFAULT_TIMEOUT))
    except Exception:  # noqa: BLE001 - a settings read must never break a kopia call
        return DEFAULT_TIMEOUT


def kopia(repo: str, args: list[str], ro: bool = True, timeout: float | None = None) -> KopiaResult:
    """Run kopia via subprocess arg-lists only.

    Passing paths as args preserves UNC/backslash strings and avoids the shell
    escaping bug documented in the project memory.

    A hung kopia used to keep the calling request or background job alive forever; the call is
    now bounded and a timeout comes back as a failed result (rc=TIMEOUT_RC) rather than an
    exception, so every existing returncode check handles it.
    """

    global KOPIA_CALLS, KOPIA_TIME
    if ro:
        check_readonly(args)
    full = _base_command(repo, args)
    limit = _timeout() if timeout is None else timeout
    start = time.time()
    try:
        res = subprocess.run(
            full,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limit if limit and limit > 0 else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash under pythonw
        )
    except subprocess.TimeoutExpired as exc:  # run() has already killed the child
        elapsed = time.time() - start
        KOPIA_CALLS += 1
        KOPIA_TIME += elapsed
        log.error("kopia timed out after %.0fs: %s", elapsed, " ".join(full[:4]))
        return KopiaResult(args=full, returncode=TIMEOUT_RC, stdout=_as_text(exc.stdout),
                           stderr=f"kopia timed out after {elapsed:.0f}s", elapsed=elapsed)
    elapsed = time.time() - start
    KOPIA_CALLS += 1
    KOPIA_TIME += elapsed
    return KopiaResult(args=full, returncode=res.returncode, stdout=res.stdout, stderr=res.stderr, elapsed=elapsed)


def _as_text(value) -> str:
    if not value:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def kopia_lines(repo: str, args: list[str], ro: bool = True, timeout: float | None = None):
    """Stream a kopia command's stdout one line at a time.

    ``kopia()`` buffers the whole output in memory, which is nothing for `snapshot list` and
    ~500 MB+ for `content list -l` on a large repo - and the caller then usually holds a second
    copy from splitlines(). This yields lines as they arrive so neither copy exists.

    Raises RuntimeError if kopia exits non-zero, so a partial read can never be mistaken for a
    complete one. The child is always waited on / killed.

    The timeout is a WATCHDOG that kills the child, not a check inside the read loop: iterating
    ``proc.stdout`` blocks until a line arrives, so a clock check in the loop body never runs
    against the failure it exists for - a child that goes silent forever. stderr is drained on
    its own thread for the same reason (an unread pipe fills at ~64 KB and blocks the child).
    """
    if ro:
        check_readonly(args)
    full = _base_command(repo, args)
    limit = _timeout() if timeout is None else timeout
    start = time.time()
    proc = subprocess.Popen(
        full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    timed_out = threading.Event()
    errbuf: list[str] = []

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - already gone
            pass

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                if len(errbuf) < 200:
                    errbuf.append(line)
        except Exception:  # noqa: BLE001 - closed underneath us
            pass

    watchdog = threading.Timer(limit, _kill_on_timeout) if limit and limit > 0 else None
    if watchdog:
        watchdog.daemon = True
        watchdog.start()
    drainer = threading.Thread(target=_drain_stderr, daemon=True)
    drainer.start()

    try:
        for line in proc.stdout:
            yield line.rstrip("\n")
        proc.stdout.close()
        rc = proc.wait(timeout=60)
        if timed_out.is_set():
            raise RuntimeError(f"kopia timed out after {limit:.0f}s: {' '.join(full[:4])}")
        if rc != 0:
            err = "".join(errbuf).strip()[:300]
            raise RuntimeError(f"kopia exited {rc}: {err or ' '.join(full[:4])}")
    finally:
        global KOPIA_CALLS, KOPIA_TIME
        KOPIA_CALLS += 1
        KOPIA_TIME += time.time() - start
        if watchdog:
            watchdog.cancel()
        if proc.poll() is None:      # generator closed early, or we raised
            proc.kill()
            proc.wait(timeout=30)
        drainer.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


def kopia_job_stream(repo: str, args: list[str], result: dict):
    """Stream BOTH pipes of one long kopia command for the job runner (core/jobs.py).

    Yields ``(stream, line)`` tuples where stream is "stdout" or "stderr" and lines are raw
    (no timestamps - the job layer stamps them). remove-files writes everything to stderr, so
    a stdout-only stream would make a working job look hung (the silence trap).

    No timeout on purpose: a full maintenance run legitimately takes hours and ``kopia_timeout``
    would kill it mid-run. Ending a job early is the caller's Stop, which terminates
    ``result["proc"]``; the stream then ends at EOF like any other exit. Both pipes are pumped
    on threads (an unread pipe fills at ~64 KB and blocks the child), ``result["returncode"]``
    is set once the exit code is known, and the finally always waits/closes so no child or
    handle leaks. Mutating by design - job commands are deliberately outside the RO allowlist.
    """
    # progress=True: a job exists to stream kopia's live output (per-snapshot detail, progress
    # updates); --no-progress would ask kopia to be quiet.
    full = _base_command(repo, args, progress=True)
    # CREATE_NEW_PROCESS_GROUP so a console event, if one ever becomes deliverable, can be
    # addressed to the child; CREATE_NO_WINDOW is the standing pythonw rule.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
    )
    result["proc"] = proc
    lines: queue.Queue = queue.Queue()
    start = time.time()

    def _pump(name: str, stream) -> None:
        try:
            for line in stream:
                # Pipe iteration splits on \n only; kopia's progress bar updates arrive \r-
                # separated, so split those too - each update becomes its own line instead of
                # an embedded control char in the stream.
                for part in line.rstrip("\r\n").split("\r"):
                    if part:
                        lines.put((name, part))
        except Exception:  # noqa: BLE001 - pipe closed underneath us (stop path)
            pass
        finally:
            lines.put(None)

    pumps = [threading.Thread(target=_pump, args=(n, s), daemon=True)
             for n, s in (("stdout", proc.stdout), ("stderr", proc.stderr))]
    for t in pumps:
        t.start()
    try:
        ended = 0
        while ended < len(pumps):
            item = lines.get()
            if item is None:
                ended += 1
                continue
            yield item
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:  # pipes closed but the child lingers - force it
            proc.kill()
            rc = proc.wait(timeout=30)
        result["returncode"] = rc
    finally:
        global KOPIA_CALLS, KOPIA_TIME
        KOPIA_CALLS += 1
        KOPIA_TIME += time.time() - start
        if proc.poll() is None:  # generator abandoned early - do not orphan the child
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 - already gone
                pass
        try:
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


def kopia_json(repo: str, args: list[str]) -> object:
    import json

    res = kopia(repo, args)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"kopia failed: {' '.join(args)}")
    return json.loads(res.stdout) if res.stdout.strip() else None


class MountError(RuntimeError):
    """kopia mount exited before the mount came up."""


def mount_snapshot(repo: str, snapshot_id: str, mountpoint: str, info: dict | None = None,
                   wait: float = 8.0) -> KopiaProcess:
    """Start a kopia mount process, registered in ``core.mounts``.

    Kopia mount is repo-read-only, but it is intentionally outside the general
    RO allowlist so destructive verbs never creep in through this boundary. Waits up to ``wait``
    seconds for the mountpoint to appear; a process that exits first raises MountError with
    kopia's own message.
    """
    from . import mounts

    args = ["mount", snapshot_id, mountpoint]
    full = _base_command(repo, args)
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"mount_{time.strftime('%Y%m%d_%H%M%S')}.log")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
        proc = subprocess.Popen(full, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=creationflags)
    deadline = time.time() + wait
    while time.time() < deadline and proc.poll() is None and not os.path.exists(mountpoint):
        time.sleep(0.25)
    if proc.poll() is not None:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail = [ln.strip() for ln in fh if ln.strip()][-3:]
        except OSError:
            tail = []
        raise MountError(" ".join(tail) or f"kopia mount exited with {proc.returncode}")
    mounts.add(proc, {"repo": repo, "snapshot_id": snapshot_id, "mountpoint": mountpoint,
                      "log": log_path, **(info or {})})
    audit.audit("mount", "mount", repo=repo, summary={"snapshot_id": snapshot_id,
                                                      "mountpoint": mountpoint})
    return KopiaProcess(args=full, pid=proc.pid)


def list_mounts() -> list[dict]:
    """Mounts started by KopiaLens that are still running (survives app restarts)."""
    from . import mounts
    return mounts.active()


def unmount(pid: int) -> bool:
    """Stop the mount process and remove the drive mapping it leaves behind."""
    from . import mounts
    return mounts.unmount(pid)


def restore_command(repo: str, source: str, destination: str, overwrite: bool = False) -> list[str]:
    """Return a restore command preview; v1 does not execute restores."""

    args = ["kopia"] + _repo_args(repo) + ["restore", source, destination]
    if overwrite:
        args.append("--overwrite")
    return args


# IDs per `kopia snapshot delete` call. A single call with thousands of 32-hex ids would exceed the
# Windows CreateProcess command-line limit (~32k chars); 200 ids ~= 6.6k chars, comfortably under.
_SNAP_DELETE_CHUNK = 200


def maintenance_run_command(repo: str) -> list[str]:
    """The `kopia [args] maintenance run --full` command for a repo (space is reclaimed by this, not
    by the snapshot delete itself). Generation only."""
    return ["kopia"] + _repo_args(repo) + ["maintenance", "run", "--full"]


def snapshot_delete_commands(repo: str, snapshot_ids: list[str], batch: bool = False) -> list[list[str]]:
    """Generate, never execute, repo-destructive delete commands (the ``--delete`` flag is what
    actually deletes; without it kopia only previews). kopia treats each id as one snapshot manifest
    (verified: it deletes only that manifest, not other snapshots sharing the same root).
    ``batch=False`` (default) = one command per id - REQUIRED by callers that align commands 1:1 with
    a per-snapshot ``items`` list (the purge-plan UI). ``batch=True`` = <=200 ids per command, for a
    short/directly-runnable bulk-delete script (the snapshot-delete preview)."""
    prefix = ["kopia"] + _repo_args(repo) + ["snapshot", "delete"]
    if batch:
        return [
            prefix + snapshot_ids[i:i + _SNAP_DELETE_CHUNK] + ["--delete"]
            for i in range(0, len(snapshot_ids), _SNAP_DELETE_CHUNK)
        ]
    return [prefix + [sid, "--delete"] for sid in snapshot_ids]


def snapshot_delete_execute(repo: str, snapshot_ids: list[str]) -> dict:
    """ACTUALLY delete snapshots, CHUNKED into <=200-id ``kopia snapshot delete ... --delete`` calls.
    Chunking is REQUIRED at scale - a single call with thousands of ids overflows the Windows
    command-line limit and fails. Synchronous, per-chunk; a partial failure still commits the earlier
    chunks (snapshot manifests are independent), and the aggregate returncode is non-zero if ANY chunk
    failed. Only mutating kopia call; bypasses the RO allowlist deliberately; logged. Space is reclaimed
    only after a later ``maintenance run --full``. (For very large bulk deletes the standalone
    Kopia-Delete.py is preferable - this call blocks for the whole run.)"""
    if not snapshot_ids:
        return {"deleted": 0, "returncode": 0, "stdout": "", "stderr": "no ids", "command": None, "chunks": 0}
    prefix = ["kopia"] + _repo_args(repo) + ["snapshot", "delete"]
    chunks = [snapshot_ids[i:i + _SNAP_DELETE_CHUNK] for i in range(0, len(snapshot_ids), _SNAP_DELETE_CHUNK)]
    log.warning("EXECUTING snapshot delete repo=%s count=%d chunks=%d", repo, len(snapshot_ids), len(chunks))
    deleted, rc, outs, errs, first_cmd = 0, 0, [], [], None
    for ci, chunk in enumerate(chunks, 1):
        full = prefix + chunk + ["--delete", "--no-progress"]
        if first_cmd is None:
            first_cmd = full
        res = subprocess.run(
            full, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if res.returncode == 0:
            deleted += len(chunk)
        else:
            rc = res.returncode
            err = (res.stderr or "").strip()[:300]
            errs.append(err)
            log.warning("snapshot delete repo=%s chunk %d/%d rc=%d stderr=%s", repo, ci, len(chunks), res.returncode, err)
        if res.stdout:
            outs.append(res.stdout)
    log.warning("snapshot delete repo=%s done deleted=%d/%d rc=%d", repo, deleted, len(snapshot_ids), rc)
    audit.audit("executed", "snapshot-delete", repo=repo,
                summary={"ids": len(snapshot_ids), "deleted": deleted, "chunks": len(chunks)},
                rc=rc)
    return {
        "deleted": deleted,
        "returncode": rc,
        "stdout": "\n".join(outs),
        "stderr": "\n".join(e for e in errs if e),
        "command": first_cmd,
        "chunks": len(chunks),
    }


def remove_files_command(repo: str, object_ids: list[str] | None = None, filenames: list[str] | None = None,
                         source: str | list[str] | None = None, commit: bool = False) -> list[str]:
    """Build a ``kopia snapshot fix remove-files`` command (NEVER executed here). Removes file
    references from ALL snapshots (rewrites the manifests without them) so GC can free their
    content - without deleting the snapshots. --object-id, --filename AND --source are all
    repeatable: ``source`` may be a single string or a LIST (scope to several sources in ONE
    command - object-ids are content-exact so cross-source mismatches are harmless no-ops, and
    listing only the affected sources avoids the slow whole-repo scan). ``--commit`` applies it."""
    cmd = ["kopia"] + _repo_args(repo) + ["snapshot", "fix", "remove-files"]
    for oid in object_ids or []:
        cmd += ["--object-id", oid]
    for fn in filenames or []:
        cmd += ["--filename", fn]
    for s in (source if isinstance(source, list) else ([source] if source else [])):
        cmd += ["--source", s]
    if commit:
        cmd.append("--commit")
    return cmd


def remove_files_execute(repo: str, object_ids: list[str] | None = None, filenames: list[str] | None = None,
                         source: str | None = None) -> dict:
    """ACTUALLY run remove-files with --commit (mutating: rewrites snapshot manifests). Logged.
    Space is reclaimed only after a later ``maintenance run --full``."""
    if not object_ids and not filenames:
        return {"returncode": 1, "stdout": "", "stderr": "nothing specified (object_ids or filenames required)"}
    full = remove_files_command(repo, object_ids, filenames, source, commit=True) + ["--no-progress"]
    log.warning("EXECUTING remove-files repo=%s filenames=%d object_ids=%d source=%r",
                repo, len(filenames or []), len(object_ids or []), source)
    t0 = time.time()
    res = subprocess.run(
        full, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.warning("remove-files repo=%s rc=%d stderr=%s", repo, res.returncode, (res.stderr or "").strip()[:300])
    audit.audit("executed", "remove-files", repo=repo,
                summary={"filenames": len(filenames or []), "object_ids": len(object_ids or []),
                         "source": source},
                rc=res.returncode, duration_s=time.time() - t0)
    return {"returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr, "command": full}


def _quote_arg(a: str) -> str:
    """Quote an arg for a pasteable / .bat command line (paths with spaces would otherwise split)."""
    return f'"{a}"' if (not a or " " in a) else a


def _bat_line(cmd: list[str]) -> str:
    """Render a command for a .bat file. Doubles every % - in BATCH context cmd.exe expands
    %...% even inside double quotes and DROPS an unmatched %, silently mangling filenames like
    'Backup 100%.txt' into a non-matching string (kopia would then quietly remove nothing)."""
    return " ".join(_quote_arg(a) for a in cmd).replace("%", "%%")


def _source_args(source: str | list[str] | None) -> list[str]:
    return [x for s in (source if isinstance(source, list) else ([source] if source else []))
            for x in ("--source", s)]


def remove_files_chunked_commands(repo: str, items: list[str], source: str | list[str] | None = None,
                                  commit: bool = False, max_chars: int = 8000,
                                  flag: str = "--object-id") -> list[list[str]]:
    """Split items (object-ids OR filenames, per ``flag``) across MULTIPLE remove-files commands so
    each command line stays under a safe limit (default 8000 - works in a .bat / cmd.exe line too,
    not just CreateProcess's 32k). Biggest-files-first order is preserved (caller pre-sorts). Each
    command is --source-scoped to the folder's own sources; ``source=None`` = whole-repo sweep."""
    base = ["kopia"] + _repo_args(repo) + ["snapshot", "fix", "remove-files"]
    src_args = _source_args(source)
    tail = ["--commit"] if commit else []
    fixed_len = sum(len(_quote_arg(a)) + 1 for a in base + src_args + tail) + 16  # safety margin
    if fixed_len + 2000 > max_chars:  # scope list alone nearly fills the line budget
        log.warning("remove-files fixed prefix is %d chars (%d sources) vs max_chars=%d - "
                    "generated commands may exceed the target line length", fixed_len,
                    len(src_args) // 2, max_chars)
    budget = max(2000, max_chars - fixed_len)
    cmds: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for it in items:
        add = len(flag) + 1 + len(_quote_arg(it)) + 1
        if cur and cur_len + add > budget:
            cmds.append(base + [x for v in cur for x in (flag, v)] + src_args + tail)
            cur, cur_len = [], 0
        cur.append(it)
        cur_len += add
    if cur:
        cmds.append(base + [x for v in cur for x in (flag, v)] + src_args + tail)
    return cmds


def remove_log_path(name: str) -> str:
    d = os.path.join(DATA_DIR, "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _remove_bat_lines(commands: list[list[str]], log_setter: str) -> list[str]:
    """The generated remove-files .bat, shared by the server-file and browser-download variants.

    Design constraints: a downloaded .bat must run on a bare Windows box, so this uses cmd
    built-ins ONLY - no PowerShell, no Python, no tee. ``remove-files`` writes all its
    per-snapshot detail to STDERR and nothing to stdout, so every batch is redirected with
    ``2>&1`` or the console shows nothing at all.

    Each batch goes to a scratch file first, which is then summarised on screen and appended to
    the full log. Without that, a run looks frozen for minutes at a time: the batches are already
    small (max_chars=8000 at the callers), but one batch still walks every snapshot in scope.

    ``find`` and ``findstr`` are called by FULL PATH. Bare ``find`` picks up GNU find from Git
    for Windows / Cygwin / GnuWin32 if any of those is ahead of System32 on the user's PATH, and
    GNU find with those arguments starts recursively scanning the drive instead of counting lines
    - the script looks hung. Verified by running a generated .bat on this machine.

    NO ``enabledelayedexpansion``. With it on, cmd.exe treats everything BETWEEN two ``!``
    characters as a variable reference and deletes the whole span - quotes and flags included,
    not just the ``!`` - corrupting filenames, ``--source`` paths and the kopia config path alike
    (reproduced: ``echo "Backup!.txt" and "Wow!! Two"`` printed ``"Backup Two"``). Plain ``%VAR%``
    works for every counter here because each read sits on its own top-level line, parsed fresh.
    Batch counters are pre-zeroed per batch: an empty slice leaves the ``for /f`` loops unset, and
    an unset var in ``%VAR%`` expansion stays literal text where ``!VAR!`` printed empty.
    """
    n = len(commands)
    find = "%SystemRoot%\\System32\\find.exe"
    findstr = "%SystemRoot%\\System32\\findstr.exe"
    problems = '/I /C:"error" /C:"failed" /C:"unable"'
    lines = [
        "@echo off",
        "setlocal",
        log_setter,
        'set "SLICE=%TEMP%\\kopialens_remove_batch_%RANDOM%.txt"',
        "set /a KL_FAILED=0",
        "set /a KL_REMOVED=0",
        "set /a KL_ERRORS=0",
        'echo KopiaLens chunked remove-files - %DATE% %TIME%> "%LOG%"',
        f'echo KopiaLens chunked remove-files: {n} batch(es).',
        'echo Full kopia output is written to "%LOG%".',
        "echo Aborting (Ctrl+C) between batches is safe - rewrites are per-snapshot atomic.",
        "echo.",
    ]
    for i, cmd in enumerate(commands, 1):
        s = _bat_line(cmd)  # %-escaped for batch context (filenames containing % stay intact)
        lines += [
            f"echo [batch {i}/{n}] %TIME% - working (this walks every snapshot in scope)...",
            f'echo === batch {i}/{n} ===>> "%LOG%"',
            f'{s}> "%SLICE%" 2>&1',
            "set KL_RC=%ERRORLEVEL%",
            'type "%SLICE%" >> "%LOG%"',
            'set "KL_LINES=0"',
            'set "KL_RM=0"',
            'set "KL_ER=0"',
            f'for /f %%C in (\'{find} /c /v "" ^< "%SLICE%"\') do set KL_LINES=%%C',
            f'for /f %%C in (\'{findstr} /I /C:"removed" /C:"will remove" "%SLICE%" ^| {find} /c /v ""\') do set KL_RM=%%C',
            f'for /f %%C in (\'{findstr} {problems} "%SLICE%" ^| {find} /c /v ""\') do set KL_ER=%%C',
            "set /a KL_REMOVED+=KL_RM",
            "set /a KL_ERRORS+=KL_ER",
            f'echo     batch {i}/{n} done: %KL_RM% removal line(s), %KL_ER% problem line(s), %KL_LINES% log line(s)',
            'if not "%KL_ER%"=="0" (',
            f'  echo     ^> problems reported in batch {i}/{n}:',
            f'  {findstr} {problems} "%SLICE%"',
            ")",
            'if not "%KL_RC%"=="0" (',
            "  set /a KL_FAILED+=1",
            f'  echo     ^> WARNING: batch {i}/{n} exited with code %KL_RC%',
            ")",
        ]
    lines += [
        "echo.",
        "echo ================ summary ================",
        f"echo Batches run     : {n}    failed: %KL_FAILED%",
        "echo Removal lines   : %KL_REMOVED%",
        "echo Problem lines   : %KL_ERRORS%",
        'echo Full log        : "%LOG%"',
        'echo Summary: batches=%KL_FAILED% failed, removal lines=%KL_REMOVED%, problem lines=%KL_ERRORS%>> "%LOG%"',
        "echo.",
        "echo Space is NOT reclaimed yet. Now run:  kopia maintenance run --full",
        'if exist "%SLICE%" del "%SLICE%" >nul 2>&1',
        "pause",
    ]
    return lines


def remove_files_save_bat(commands: list[list[str]], bat_path: str, log_path: str) -> str:
    """Write the chunked commands to a .bat the user can run + watch. Each batch appends kopia's
    own output to ``log_path`` so the run is fully inspectable."""
    lines = _remove_bat_lines(commands, f'set "LOG={log_path}"')
    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines))
    return bat_path


def remove_files_bat_text(commands: list[list[str]], log_name: str) -> str:
    """Return the .bat text (for browser download - no server file). It logs kopia output to
    ``log_name`` NEXT TO the .bat (`%~dp0`) so the run is self-contained + inspectable."""
    return "\r\n".join(_remove_bat_lines(commands, f'set "LOG=%~dp0{log_name}"'))


_REMOVE_JOBS: dict[str, dict] = {}


def remove_files_chunked_execute(repo: str, items: list[str], source: str | list[str] | None,
                                 job_id: str, log_path: str, flag: str = "--object-id") -> dict:
    """Run the chunked remove-files commands sequentially (--commit), appending kopia output to
    ``log_path`` and updating ``_REMOVE_JOBS[job_id]`` for progress polling. ``flag`` selects
    object-id vs filename. Mutating + logged. Space frees only after ``maintenance run --full``."""
    cmds = remove_files_chunked_commands(repo, items, source, commit=True, max_chars=32000, flag=flag)
    job = _REMOVE_JOBS.setdefault(job_id, {})
    job.update({"total": len(cmds), "done": 0, "running": True, "errors": 0,
                "log": log_path, "objects": len(items), "rc": 0})
    log.warning("EXECUTING chunked remove-files repo=%s flag=%s items=%d batches=%d source=%r",
                repo, flag, len(items), len(cmds), source)
    t0 = time.time()
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
            lf.write(f"=== chunked remove-files repo={repo} {flag} items={len(items)} batches={len(cmds)} ===\n")
            lf.flush()
            for i, cmd in enumerate(cmds, 1):
                lf.write(f"\n--- batch {i}/{len(cmds)} ---\n"); lf.flush()
                res = subprocess.run(
                    cmd + ["--no-progress"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                lf.write(res.stdout or "")
                if res.stderr:
                    lf.write("\n[stderr]\n" + res.stderr)
                lf.flush()
                job["done"] = i
                if res.returncode != 0:
                    job["errors"] += 1
                    job["rc"] = res.returncode
            lf.write("\n=== done. Run: kopia maintenance run --full to reclaim. ===\n")
    except Exception as exc:  # noqa: BLE001
        job["error"] = str(exc)
        log.error("chunked remove-files repo=%s failed: %s", repo, exc)
    finally:
        job["running"] = False
    audit.audit("executed", "remove-files-chunked", repo=repo,
                summary={"items": len(items), "batches": len(cmds), "errors": job.get("errors", 0),
                         "flag": flag, "source": source},
                rc=job.get("rc", 0), duration_s=time.time() - t0, log_file=log_path)
    return job


_DRY_OUTPUT_CAP = 256 * 1024  # bytes of kopia output carried in an API response


def _run_dry_commands(repo: str, cmds: list[list[str]], kind: str, expect_flag: str,
                      parse_line=None) -> dict:
    """Run preview-only kopia commands sequentially and capture their output.

    The commands MUST have been built without the mutating flag (``--commit`` for
    remove-files, ``--delete`` for snapshot delete); ``expect_flag`` is asserted absent
    so a builder change can never turn the dry-run button into a real deletion.
    Full output also lands in a log file under data/logs.

    Two reviewed traps live here (2026-09-09): the output cap only trims what the RESPONSE
    carries - every command still runs, or later batches would silently never execute and
    the summary would undercount while rc stays 0. And subprocess failures are NOT covered
    by the log-write guard: a missing kopia must fail the run, not return a clean empty
    success the UI would read as "dry run checked". ``parse_line(line, acc)`` builds the
    summary (entries / snapshots / final_line) incrementally, so the job runner reuses the
    same parser per streamed line.
    """
    for c in cmds:
        if expect_flag in c:
            raise PermissionError(f"dry-run command carries the mutating flag {expect_flag}")
    limit = _timeout()
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_path = remove_log_path(f"dry_run_{kind}_repo{repo}_{ts}.log")
    rc, truncated, error = 0, False, ""
    out: list[str] = []
    acc: dict = {}
    t0 = time.time()
    try:
        lf = open(log_path, "a", encoding="utf-8", errors="replace")
        lf.write(f"=== kopia {kind} dry run repo={repo} batches={len(cmds)} ===\n")
        lf.flush()
    except Exception as exc:  # noqa: BLE001 - a log-write failure must not kill the dry run
        log.warning("dry run %s repo=%s log open failed: %s", kind, repo, exc)
        lf = None

    def _log(text: str) -> None:
        if lf is None:
            return
        try:
            lf.write(text)
            lf.flush()
        except Exception as exc:  # noqa: BLE001 - the run itself matters more than its log
            log.warning("dry run %s repo=%s log write failed: %s", kind, repo, exc)

    for i, cmd in enumerate(cmds, 1):
        _log(f"\n--- batch {i}/{len(cmds)} ---\n")
        try:
            res = subprocess.run(
                cmd + ["--no-progress"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=limit,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            rc = TIMEOUT_RC
            error = f"kopia timed out after {limit:.0f}s"
            _log(_as_text(exc.stdout))
            out.append(error)
            break
        except OSError as exc:  # FileNotFoundError lives here: kopia missing from PATH
            rc = 1
            error = f"kopia could not be started: {exc}"
            _log(error + "\n")
            out.append(error)
            break  # every remaining batch would fail identically
        batch = res.stdout or ""
        if res.stderr.strip():
            batch += "\n[stderr]\n" + res.stderr
        _log(batch)
        if not truncated:
            out.append(batch)
            if sum(len(s) for s in out) > _DRY_OUTPUT_CAP:
                truncated = True  # the response trims; the remaining batches still run
        if parse_line is not None:
            for ln in batch.splitlines():
                parse_line(ln, acc)
        if res.returncode != 0:
            rc = res.returncode
    if lf is not None:
        try:
            lf.close()
        except Exception:  # noqa: BLE001
            pass
    elapsed = time.time() - t0
    r = {"returncode": rc, "output": "\n".join(out)[:_DRY_OUTPUT_CAP], "truncated": truncated,
         "log": log_path, "elapsed": round(elapsed, 1), "commands": len(cmds)}
    if error:
        r["error"] = error
    r.update(acc)  # entries / snapshots / final_line when a parser was given
    return r


def _parse_remove_line(line: str, acc: dict) -> dict:
    """Incremental remove-files dry-run summary - one kopia output line at a time, so the
    same parser serves the synchronous dry run (per batch) and the streaming job (per line)."""
    for ln in line.splitlines():
        if "will remove file" in ln:
            acc["entries"] = acc.get("entries", 0) + 1
        if "snapshot manifests were not updated" in ln or "Fixed and committed" in ln:
            acc["final_line"] = ln.strip()
        m = re.search(r"Fixed (\d+) snapshots", ln)
        if m:
            # kopia prints one "Fixed N snapshots" line per batch; a later batch's 0 used to
            # overwrite the count and read the whole run as 0.
            acc["snapshots"] = acc.get("snapshots", 0) + int(m.group(1))
    return acc


def _parse_delete_line(line: str, acc: dict) -> dict:
    for ln in line.splitlines():
        if "Would delete" in ln:
            acc["snapshots"] = acc.get("snapshots", 0) + 1
            acc["final_line"] = f"Would delete {acc['snapshots']} snapshot(s)"
    return acc


def _parse_remove_dry(text: str) -> dict:
    """Whole-text form of the remove-files summary (tests + one-off callers)."""
    acc: dict = {"entries": 0, "snapshots": 0, "final_line": ""}
    for ln in text.splitlines():
        _parse_remove_line(ln, acc)
    return acc


def _parse_delete_dry(text: str) -> dict:
    """Whole-text form of the snapshot-delete summary (tests + one-off callers)."""
    acc: dict = {"snapshots": 0, "final_line": ""}
    for ln in text.splitlines():
        _parse_delete_line(ln, acc)
    return acc


def remove_files_dry_run(repo: str, object_ids: list[str] | None = None,
                         filenames: list[str] | None = None,
                         source: str | list[str] | None = None) -> dict:
    """RUN remove-files without --commit: kopia walks every snapshot in scope and reports
    exactly what it would rewrite, changing nothing. Chunked at 32k like the executor so
    large sets work. Preview-only; logged."""
    if not object_ids and not filenames:
        return {"returncode": 1, "output": "", "error": "nothing specified (object_ids or filenames required)"}
    cmds = remove_files_chunked_commands(repo, object_ids or filenames or [],
                                         source=source, commit=False, max_chars=32000,
                                         flag="--filename" if filenames else "--object-id")
    log.info("DRY RUN remove-files repo=%s filenames=%d object_ids=%d source=%r batches=%d",
             repo, len(filenames or []), len(object_ids or []), source, len(cmds))
    r = _run_dry_commands(repo, cmds, "remove-files", "--commit", parse_line=_parse_remove_line)
    audit.audit("dry-run", "remove-files", repo=repo,
                summary={"filenames": len(filenames or []), "object_ids": len(object_ids or []),
                         "source": source, "entries": r.get("entries"),
                         "snapshots": r.get("snapshots")},
                rc=r["returncode"], duration_s=r["elapsed"], log_file=r["log"])
    return r


def snapshot_delete_dry_run(repo: str, snapshot_ids: list[str]) -> dict:
    """RUN snapshot delete without --delete: kopia lists exactly what it would delete.
    Chunked like the executor (200 ids per call). Preview-only; logged."""
    if not snapshot_ids:
        return {"returncode": 1, "output": "", "error": "snapshot_ids is empty"}
    prefix = ["kopia"] + _repo_args(repo) + ["snapshot", "delete"]
    cmds = [prefix + snapshot_ids[i:i + _SNAP_DELETE_CHUNK]
            for i in range(0, len(snapshot_ids), _SNAP_DELETE_CHUNK)]
    log.info("DRY RUN snapshot delete repo=%s count=%d batches=%d", repo, len(snapshot_ids), len(cmds))
    r = _run_dry_commands(repo, cmds, "snapshot-delete", "--delete", parse_line=_parse_delete_line)
    audit.audit("dry-run", "snapshot-delete", repo=repo,
                summary={"ids": len(snapshot_ids), "would_delete": r.get("snapshots")},
                rc=r["returncode"], duration_s=r["elapsed"], log_file=r["log"])
    return r


def snapshot_pin_command(repo: str, ids: list[str], remove: bool = False,
                         pin_name: str = "do-not-delete") -> list[str]:
    """Pin (protect from retention deletion) or unpin snapshots. Command-gen only."""
    flag = "--remove" if remove else "--pins"
    return ["kopia"] + _repo_args(repo) + ["snapshot", "pin", *ids, flag, pin_name]


# ---- policy editing (/) ----------------------------------------------------
# Policy writes are fast kopia calls (seconds - not job-runner material). Every write is backed
# up first by the caller (core.policybackup, FAIL-CLOSED: no verified backup, no write); these
# functions only build the command, run it, log it loudly and audit it.

def _policy_target_args(target: str | None, global_: bool) -> list[str]:
    if global_:
        if target:
            raise ValueError("target and global_ are mutually exclusive")
        return ["--global"]
    if not target:
        raise ValueError("need a target (source key) or global_=True")
    if "\n" in target or "\r" in target:
        raise ValueError("invalid target")
    return [target]


def policy_set_command(repo: str, target: str | None = None, global_: bool = False,
                       add_ignore: list[str] | None = None,
                       remove_ignore: list[str] | None = None) -> list[str]:
    """Build `kopia policy set` for ignore-rule changes. On a target without a defined policy,
    kopia CREATES one - callers surface that via the diff (`will_create_policy`)."""
    if not add_ignore and not remove_ignore:
        raise ValueError("policy set needs at least one change (add_ignore or remove_ignore)")
    cmd = ["kopia"] + _repo_args(repo) + ["policy", "set"] + _policy_target_args(target, global_)
    for p in remove_ignore or []:
        cmd += ["--remove-ignore", p]
    for p in add_ignore or []:
        cmd += ["--add-ignore", p]
    return cmd


def _run_policy_write(repo: str, cmd: list[str], action: str, summary: dict) -> dict:
    """Run one mutating policy command synchronously. Logged + audited; the kopia timeout
    applies. Returns the raw result - the caller decides ok/fail from the returncode."""
    log.warning("EXECUTING %s repo=%s cmd=%s", action, repo, " ".join(cmd[:8]))
    t0 = time.time()
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_timeout(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rc, out, err = res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = TIMEOUT_RC, "", f"kopia timed out after {_timeout():.0f}s"
        log.error("%s repo=%s timed out", action, repo)
    except OSError as exc:  # kopia missing from PATH etc.
        rc, out, err = 1, "", f"kopia could not be started: {exc}"
        log.error("%s repo=%s could not start: %s", action, repo, exc)
    audit.audit("executed", action, repo=repo, summary=summary, rc=rc,
                duration_s=time.time() - t0)
    return {"returncode": rc, "stdout": out, "stderr": err, "command": cmd}


def policy_set_execute(repo: str, target: str | None = None, global_: bool = False,
                       add_ignore: list[str] | None = None,
                       remove_ignore: list[str] | None = None) -> dict:
    """ACTUALLY change a policy's ignore rules. Mutating; requires a verified pre-write backup
    (the save endpoint's fail-closed step - do not call directly from new code paths)."""
    cmd = policy_set_command(repo, target, global_, add_ignore, remove_ignore) + ["--no-progress"]
    summary = {"target": "global" if global_ else target,
               "add": len(add_ignore or []), "remove": len(remove_ignore or [])}
    return _run_policy_write(repo, cmd, "policy-set", summary)


def policy_delete_command(repo: str, target: str | None = None, global_: bool = False) -> list[str]:
    """Build `kopia policy delete` - removes the whole policy DEFINITION (never snapshots)."""
    return ["kopia"] + _repo_args(repo) + ["policy", "delete"] + _policy_target_args(target, global_)


def policy_delete_execute(repo: str, target: str | None = None, global_: bool = False) -> dict:
    cmd = policy_delete_command(repo, target, global_) + ["--no-progress"]
    summary = {"target": "global" if global_ else target, "delete": True}
    return _run_policy_write(repo, cmd, "policy-delete", summary)


def policy_import_command(repo: str, file_path: str, mirror: bool = False,
                          target: str | None = None, global_: bool = False) -> list[str]:
    """Build `kopia policy import --from-file <file>`. Merges into the policies the file
    defines, leaving other policies alone; `mirror=True` adds --delete-other-policies (the repo
    ends up with EXACTLY the backup's policies). A target (or `global_`) limits the import to
    that one policy."""
    if not file_path:
        raise ValueError("policy import needs a file path")
    cmd = ["kopia"] + _repo_args(repo) + ["policy", "import", "--from-file", file_path]
    if target or global_:
        if mirror:
            raise ValueError("a mirror import cannot be limited to one target")
        cmd += _policy_target_args(target, global_)
    if mirror:
        cmd.append("--delete-other-policies")
    return cmd


def policy_import_execute(repo: str, file_path: str, mirror: bool = False) -> dict:
    cmd = policy_import_command(repo, file_path, mirror) + ["--no-progress"]
    summary = {"from_file": os.path.basename(file_path), "mirror": bool(mirror)}
    return _run_policy_write(repo, cmd, "policy-import", summary)


def policy_write_rules_execute(repo: str, target: str | None, global_: bool, policy: dict,
                               add: int = 0, remove: int = 0) -> dict:
    """Write one whole policy (ignore list already in its final order) via a target-limited
    `policy import`. `policy set --add-ignore` re-sorts the list alphabetically; import stores it
    as given. Mutating; the save endpoint takes the pre-write backup first."""
    import json
    import tempfile

    key = "(global)" if global_ else target
    tmp_dir = os.path.join(DATA_DIR, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="policy_write_", suffix=".json", dir=tmp_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({key: policy}, fh)
        cmd = policy_import_command(repo, path, target=target, global_=global_) + ["--no-progress"]
        summary = {"target": "global" if global_ else target, "add": add, "remove": remove}
        return _run_policy_write(repo, cmd, "policy-set", summary)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def snapshot_diff_command(repo: str, id1: str, id2: str) -> list[str]:
    return ["kopia"] + _repo_args(repo) + ["diff", id1, id2]


def diff_execute(repo: str, id1: str, id2: str) -> str:
    """Run `kopia diff` (read-only) and return its textual output."""
    res = kopia(repo, ["diff", id1, id2])  # diff is in the RO allowlist
    return (res.stdout or res.stderr or "").strip()


_RESTORE_FLAG_MAP = {
    # option key -> (flag, emit-when-value-is)
    "skip_existing": ("--skip-existing", True),
    "ignore_errors": ("--ignore-errors", True),
    "write_files_atomically": ("--write-files-atomically", True),
    "write_sparse_files": ("--write-sparse-files", True),
    "delete_extra": ("--delete-extra", True),
    # "restore X" toggles: when FALSE we pass --skip-X
    "restore_owners": ("--skip-owners", False),
    "restore_permissions": ("--skip-permissions", False),
    "restore_times": ("--skip-times", False),
    # overwrite toggles: default kopia overwrites, so when FALSE we pass --no-overwrite-X
    "overwrite_files": ("--no-overwrite-files", False),
    "overwrite_directories": ("--no-overwrite-directories", False),
    "overwrite_symlinks": ("--no-overwrite-symlinks", False),
}


def restore_command_full(repo: str, source_id: str, target: str, options: dict | None = None) -> list[str]:
    """Build a full `kopia restore <source> <target>` command from the panel options. NEVER
    executed here. ``source_id`` is an object id (file/dir/snapshot root), optionally with a
    sub-path; target is a folder or a .zip/.tar archive."""
    opts = options or {}
    cmd = ["kopia"] + _repo_args(repo) + ["restore", source_id, target]
    for key, (flag, when) in _RESTORE_FLAG_MAP.items():
        if bool(opts.get(key)) == when:
            cmd.append(flag)
    if opts.get("ignore_permission_errors") is False:  # kopia ignores permission errors by default
        cmd.append("--no-ignore-permission-errors")
    if opts.get("parallel"):
        cmd += ["--parallel", str(int(opts["parallel"]))]
    if opts.get("shallow") not in (None, ""):
        cmd += [f"--shallow={int(opts['shallow'])}"]
    return cmd


def restore_execute(repo: str, source_id: str, target: str, options: dict | None = None) -> dict:
    """ACTUALLY run a restore (writes files/archive to ``target``). Logged. Non-destructive for a
    new folder / archive; can overwrite when the overwrite/delete-extra options are enabled."""
    full = restore_command_full(repo, source_id, target, options) + ["--no-progress"]
    log.warning("EXECUTING restore repo=%s source=%s -> %s", repo, source_id, target)
    t0 = time.time()
    res = subprocess.run(
        full, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.warning("restore repo=%s rc=%d stderr=%s", repo, res.returncode, (res.stderr or "").strip()[:300])
    opts = options or {}
    audit.audit("executed", "restore", repo=repo,
                summary={"source_id": source_id, "target": target,
                         "overwrite": bool(opts.get("overwrite_files") or opts.get("overwrite_directories")),
                         "delete_extra": bool(opts.get("delete_extra"))},
                rc=res.returncode, duration_s=time.time() - t0)
    return {"returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr, "command": full}


def pick_folder(initial: str | None = None) -> str | None:
    """Open the native Windows folder picker (on the user's desktop, since the server is local)
    and return the chosen path. Blocks until the user picks or cancels."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = 'Select restore destination'; "
        + (f"$f.SelectedPath = '{initial}'; " if initial else "")
        + "if ($f.ShowDialog() -eq 'OK') { [Console]::Out.Write($f.SelectedPath) }"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return res.stdout.strip() or None
    except Exception as exc:  # noqa: BLE001
        log.warning("folder picker failed: %s", exc)
        return None


def next_free_drive() -> str | None:
    """First free Windows drive letter (Z: down to E:) for an auto-assigned mountpoint."""
    taken = _mapped_letters()
    for letter in reversed(string.ascii_uppercase[4:]):  # Z..E (skip A-D)
        root = f"{letter}:\\"
        if not os.path.exists(root) and f"{letter}:" not in taken:
            return f"{letter}:"
    return None


def _mapped_letters() -> set[str]:
    """Letters held by any `net use` mapping, including remembered ones that are disconnected."""
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(["net", "use"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return set()
    return set(re.findall(r"(?<![\w\/])([A-Z]:)\s", out))


def open_in_explorer(target: str) -> None:
    """Open Windows Explorer at ``target`` (selecting it if it's a file). Localhost-only app, so
    launching the shell here is safe; failures are swallowed (best-effort convenience)."""
    try:
        if os.path.isdir(target):
            subprocess.Popen(["explorer", target], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            subprocess.Popen(["explorer", "/select,", target], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        log.warning("open_in_explorer failed for %s: %s", target, exc)


def mount_and_browse(repo: str, snapshot_id: str, mountpoint: str, in_mount_target: str | None = None,
                     browse: bool = True, info: dict | None = None) -> KopiaProcess:
    """Mount a snapshot, then (in the background) wait for the filesystem to become ready and open
    Explorer at the requested in-mount path."""
    proc = mount_snapshot(repo, snapshot_id, mountpoint, info={**(info or {}), "open": in_mount_target})
    if browse:
        target = in_mount_target or mountpoint

        def _wait_and_open() -> None:
            for _ in range(60):  # up to ~30s for the mount FS to appear
                if os.path.exists(mountpoint) and (in_mount_target is None or os.path.exists(target)):
                    open_in_explorer(target)
                    return
                time.sleep(0.5)
            log.warning("mount %s not ready in time; opening mountpoint root", mountpoint)
            open_in_explorer(mountpoint)

        threading.Thread(target=_wait_and_open, daemon=True).start()
    return proc
