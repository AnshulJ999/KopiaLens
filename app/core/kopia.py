"""Kopia subprocess boundary.

Most calls are read-only and protected by the same allowlist as
``kopiatoolkit.py``. Mount is allowed as an explicit operational action because it
does not mutate the repository. Restore is command-preview only in v1.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Iterable

from config import REPOS

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
    return KopiaProcess(args=full, pid=proc.pid)


def restore_command(repo: str, source: str, destination: str, overwrite: bool = False) -> list[str]:
    """Return a restore command preview; v1 does not execute restores."""

    args = ["kopia"] + REPOS[repo]["args"] + ["restore", source, destination]
    if overwrite:
        args.append("--overwrite")
    return args


def snapshot_delete_commands(repo: str, snapshot_ids: list[str]) -> list[list[str]]:
    """Generate, never execute, repo-destructive delete commands."""

    return [["kopia"] + REPOS[repo]["args"] + ["snapshot", "delete", sid] for sid in snapshot_ids]
