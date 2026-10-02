"""Recorded kopia replies for demo repos.

A demo repo has no kopia behind it. ``app.core.demo`` runs the read commands screens need once
against the real repo, fakes every name in the output and saves it here; the kopia wrapper
answers a demo repo from this file and never starts a process.
"""

from __future__ import annotations

import json
import os
import threading

from config import DATA_DIR

# Shown in commands generated for a demo repo. It does not exist, and kopia.py refuses any
# command that carries it.
DISPLAY_CONFIG = r"C:\Users\demo\AppData\Roaming\kopia\repository.config"
UNAVAILABLE = "Not available in the demo."

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}


def replies_path(repo: str, base: str | None = None) -> str:
    return os.path.join(base or DATA_DIR, f"demo_replies_repo{repo}.json")


def key(args: list[str]) -> str:
    """Canonical lookup key: ``policy export`` writes to a caller-chosen temp file, so the file
    arguments are not part of what was asked."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--to-file":
            skip = True
            continue
        if a in ("--overwrite", "--no-progress"):
            continue
        out.append(str(a))
    return json.dumps(out)


def _load(repo: str) -> dict:
    with _LOCK:
        if repo not in _CACHE:
            try:
                with open(replies_path(repo), encoding="utf-8") as fh:
                    _CACHE[repo] = json.load(fh)
            except (OSError, ValueError):
                _CACHE[repo] = {}
        return _CACHE[repo]


def reply(repo: str, args: list[str]) -> tuple[int, str, str]:
    """(returncode, stdout, stderr) for one recorded command."""
    rec = _load(repo).get(key(args))
    if rec is None:
        return 1, "", UNAVAILABLE
    out = rec.get("out", "")
    if "--to-file" in args:
        target = args[args.index("--to-file") + 1]
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(out)
        out = ""
    return int(rec.get("rc", 0)), out, rec.get("err", "")
