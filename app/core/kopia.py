"""Kopia subprocess boundary.

Most calls are read-only and protected by the same allowlist as
``kopiatoolkit.py``. Mount is allowed as an explicit operational action because it
does not mutate the repository. Restore is command-preview only in v1.
"""

from __future__ import annotations

import os
import string
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from config import DATA_DIR, REPOS

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


def _base_command(repo: str, args: list[str]) -> list[str]:
    if repo not in REPOS:
        raise ValueError(f"unknown repo: {repo}")
    return ["kopia"] + REPOS[repo]["args"] + args + ["--no-progress"]


def kopia(repo: str, args: list[str], ro: bool = True) -> KopiaResult:
    """Run kopia via subprocess arg-lists only.

    Passing paths as args preserves UNC/backslash strings and avoids the shell
    escaping bug documented in the project memory.
    """

    global KOPIA_CALLS, KOPIA_TIME
    if ro:
        check_readonly(args)
    full = _base_command(repo, args)
    start = time.time()
    res = subprocess.run(
        full,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash under pythonw
    )
    elapsed = time.time() - start
    KOPIA_CALLS += 1
    KOPIA_TIME += elapsed
    return KopiaResult(args=full, returncode=res.returncode, stdout=res.stdout, stderr=res.stderr, elapsed=elapsed)


def kopia_json(repo: str, args: list[str]) -> object:
    import json

    res = kopia(repo, args)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"kopia failed: {' '.join(args)}")
    return json.loads(res.stdout) if res.stdout.strip() else None


_MOUNTS: dict[int, dict] = {}  # pid -> {repo, snapshot_id, mountpoint, proc, started}


def mount_snapshot(repo: str, snapshot_id: str, mountpoint: str) -> KopiaProcess:
    """Start a Kopia mount process and return the immediate command result.

    Kopia mount is repo-read-only, but it is intentionally outside the general
    RO allowlist so destructive verbs never creep in through this boundary.
    """

    args = ["mount", snapshot_id, mountpoint]
    full = _base_command(repo, args)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        full,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    _MOUNTS[proc.pid] = {
        "repo": repo, "snapshot_id": snapshot_id, "mountpoint": mountpoint,
        "proc": proc, "started": time.time(),
    }
    return KopiaProcess(args=full, pid=proc.pid)


def list_mounts() -> list[dict]:
    """Active mounts started by this process (drops any whose process has exited)."""
    out = []
    for pid, m in list(_MOUNTS.items()):
        if m["proc"].poll() is not None:  # process exited -> unmounted
            _MOUNTS.pop(pid, None)
            continue
        out.append({"pid": pid, "repo": m["repo"], "snapshot_id": m["snapshot_id"],
                    "mountpoint": m["mountpoint"], "started": m["started"]})
    return out


def unmount(pid: int) -> bool:
    """Unmount by terminating the foreground mount process that holds it."""
    m = _MOUNTS.get(pid)
    if not m:
        return False
    proc = m["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _MOUNTS.pop(pid, None)
    return True


def restore_command(repo: str, source: str, destination: str, overwrite: bool = False) -> list[str]:
    """Return a restore command preview; v1 does not execute restores."""

    args = ["kopia"] + REPOS[repo]["args"] + ["restore", source, destination]
    if overwrite:
        args.append("--overwrite")
    return args


def snapshot_delete_commands(repo: str, snapshot_ids: list[str]) -> list[list[str]]:
    """Generate, never execute, repo-destructive delete commands (the ``--delete`` flag is what
    actually deletes; without it kopia only previews)."""

    return [
        ["kopia"] + REPOS[repo]["args"] + ["snapshot", "delete", sid, "--delete"]
        for sid in snapshot_ids
    ]


def snapshot_delete_execute(repo: str, snapshot_ids: list[str]) -> dict:
    """ACTUALLY delete snapshots (one ``kopia snapshot delete <ids...> --delete``). This is the
    only mutating kopia call in the app; it bypasses the read-only allowlist deliberately and is
    logged. Space is reclaimed only after a later ``maintenance run --full`` (kopia's own step)."""
    if not snapshot_ids:
        return {"deleted": 0, "returncode": 0, "stdout": "", "stderr": "no ids"}
    full = ["kopia"] + REPOS[repo]["args"] + ["snapshot", "delete", *snapshot_ids, "--delete", "--no-progress"]
    log.warning("EXECUTING snapshot delete repo=%s count=%d ids=%s", repo, len(snapshot_ids), ",".join(snapshot_ids))
    res = subprocess.run(
        full, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.warning("snapshot delete repo=%s rc=%d stderr=%s", repo, res.returncode, (res.stderr or "").strip()[:300])
    return {
        "deleted": len(snapshot_ids) if res.returncode == 0 else 0,
        "returncode": res.returncode,
        "stdout": res.stdout, "stderr": res.stderr,
        "command": full,
    }


def remove_files_command(repo: str, object_ids: list[str] | None = None, filenames: list[str] | None = None,
                         source: str | list[str] | None = None, commit: bool = False) -> list[str]:
    """Build a ``kopia snapshot fix remove-files`` command (NEVER executed here). Removes file
    references from ALL snapshots (rewrites the manifests without them) so GC can free their
    content - without deleting the snapshots. --object-id, --filename AND --source are all
    repeatable: ``source`` may be a single string or a LIST (scope to several sources in ONE
    command - object-ids are content-exact so cross-source mismatches are harmless no-ops, and
    listing only the affected sources avoids the slow whole-repo scan). ``--commit`` applies it."""
    cmd = ["kopia"] + REPOS[repo]["args"] + ["snapshot", "fix", "remove-files"]
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
    res = subprocess.run(
        full, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.warning("remove-files repo=%s rc=%d stderr=%s", repo, res.returncode, (res.stderr or "").strip()[:300])
    return {"returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr, "command": full}


def _quote_arg(a: str) -> str:
    """Quote an arg for a pasteable / .bat command line (paths with spaces would otherwise split)."""
    return f'"{a}"' if (not a or " " in a) else a


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
    base = ["kopia"] + REPOS[repo]["args"] + ["snapshot", "fix", "remove-files"]
    src_args = _source_args(source)
    tail = ["--commit"] if commit else []
    fixed_len = sum(len(_quote_arg(a)) + 1 for a in base + src_args + tail) + 16  # safety margin
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


def remove_files_save_bat(commands: list[list[str]], bat_path: str, log_path: str) -> str:
    """Write the chunked commands to a .bat the user can run + watch. Each batch appends kopia's
    own output to ``log_path`` so the run is fully inspectable."""
    n = len(commands)
    lines = [
        "@echo off", "setlocal", f'set "LOG={log_path}"',
        'echo KopiaLens chunked remove-files - %DATE% %TIME%> "%LOG%"',
        f'echo {n} batch(es). kopia output appends to "%LOG%".',
        'echo Aborting (Ctrl+C) between batches is safe - rewrites are per-snapshot atomic.',
        "echo.",
    ]
    for i, cmd in enumerate(commands, 1):
        s = " ".join(_quote_arg(a) for a in cmd)
        lines.append(f'echo [batch {i}/{n}] %TIME%')
        lines.append(f'echo === batch {i}/{n} ===>> "%LOG%"')
        lines.append(f'{s}>> "%LOG%" 2>&1')
    lines += ["echo.", 'echo Done. Now run:  kopia maintenance run --full   to reclaim the space.',
              'echo Done.>> "%LOG%"', "pause"]
    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines))
    return bat_path


def remove_files_bat_text(commands: list[list[str]], log_name: str) -> str:
    """Return the .bat text (for browser download — no server file). It logs kopia output to
    ``log_name`` NEXT TO the .bat (`%~dp0`) so the run is self-contained + inspectable."""
    n = len(commands)
    lines = [
        "@echo off", "setlocal", f'set "LOG=%~dp0{log_name}"',
        'echo KopiaLens chunked remove-files - %DATE% %TIME%> "%LOG%"',
        f'echo {n} batch(es). kopia output appends to "%LOG%".',
        'echo Aborting (Ctrl+C) between batches is safe - rewrites are per-snapshot atomic.', "echo.",
    ]
    for i, cmd in enumerate(commands, 1):
        s = " ".join(_quote_arg(a) for a in cmd)
        lines.append(f'echo [batch {i}/{n}] %TIME%')
        lines.append(f'echo === batch {i}/{n} ===>> "%LOG%"')
        lines.append(f'{s}>> "%LOG%" 2>&1')
    lines += ["echo.", 'echo Done. Now run:  kopia maintenance run --full   to reclaim the space.', "pause"]
    return "\r\n".join(lines)


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
    return job


def snapshot_pin_command(repo: str, ids: list[str], remove: bool = False,
                         pin_name: str = "do-not-delete") -> list[str]:
    """Pin (protect from retention deletion) or unpin snapshots. Command-gen only."""
    flag = "--remove" if remove else "--pins"
    return ["kopia"] + REPOS[repo]["args"] + ["snapshot", "pin", *ids, flag, pin_name]


def snapshot_diff_command(repo: str, id1: str, id2: str) -> list[str]:
    return ["kopia"] + REPOS[repo]["args"] + ["diff", id1, id2]


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
    "ignore_permission_errors": ("--ignore-permission-errors", True),
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
    cmd = ["kopia"] + REPOS[repo]["args"] + ["restore", source_id, target]
    for key, (flag, when) in _RESTORE_FLAG_MAP.items():
        if bool(opts.get(key)) == when:
            cmd.append(flag)
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
    res = subprocess.run(
        full, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.warning("restore repo=%s rc=%d stderr=%s", repo, res.returncode, (res.stderr or "").strip()[:300])
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
    for letter in reversed(string.ascii_uppercase[4:]):  # Z..E (skip A-D)
        root = f"{letter}:\\"
        if not os.path.exists(root):
            return f"{letter}:"
    return None


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
                     browse: bool = True) -> KopiaProcess:
    """Mount a snapshot, then (in the background) wait for the filesystem to become ready and open
    Explorer at the requested in-mount path. Returns immediately with the mount process."""
    proc = mount_snapshot(repo, snapshot_id, mountpoint)
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
