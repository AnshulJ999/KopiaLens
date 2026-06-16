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

from config import REPOS

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


def remove_files_command(repo: str, object_ids: list[str] | None = None, filename: str | None = None,
                         source: str | None = None, commit: bool = False) -> list[str]:
    """Build a ``kopia snapshot fix remove-files`` command (NEVER executed here). Removes a file's
    references from ALL snapshots (rewrites the manifests without it) so GC can free its content -
    without deleting the snapshots. ``--commit`` applies it; without it kopia only previews."""
    cmd = ["kopia"] + REPOS[repo]["args"] + ["snapshot", "fix", "remove-files"]
    for oid in object_ids or []:
        cmd += ["--object-id", oid]
    if filename:
        cmd += ["--filename", filename]
    if source:
        cmd += ["--source", source]
    if commit:
        cmd.append("--commit")
    return cmd


def remove_files_execute(repo: str, object_ids: list[str] | None = None, filename: str | None = None,
                         source: str | None = None) -> dict:
    """ACTUALLY run remove-files with --commit (mutating: rewrites snapshot manifests). Logged.
    Space is reclaimed only after a later ``maintenance run --full``."""
    if not object_ids and not filename:
        return {"returncode": 1, "stdout": "", "stderr": "nothing specified (object_ids or filename required)"}
    full = remove_files_command(repo, object_ids, filename, source, commit=True) + ["--no-progress"]
    log.warning("EXECUTING remove-files repo=%s filename=%r object_ids=%d source=%r",
                repo, filename, len(object_ids or []), source)
    res = subprocess.run(
        full, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.warning("remove-files repo=%s rc=%d stderr=%s", repo, res.returncode, (res.stderr or "").strip()[:300])
    return {"returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr, "command": full}


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
