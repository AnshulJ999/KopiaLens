"""Recorded kopia replies for demo repos.

A demo repo has no kopia behind it. ``app.core.demo`` runs the read commands screens need once
against the real repo, fakes every name in the output and saves it here; the kopia wrapper
answers a demo repo from this file and never starts a process.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading

from config import DATA_DIR, HERE

# Shown in commands generated for a demo repo. It does not exist, and kopia.py refuses any
# command that carries it.
UNAVAILABLE = "Not available in the demo."
# Stand-ins for this PC's own folders in a demo instance's replies, in this OS's path style.
if os.name == "nt":
    DISPLAY_CONFIG = r"C:\Users\demo\AppData\Roaming\kopia\repository.config"
    _DISPLAY = ((DATA_DIR, r"C:\Users\demo\AppData\Local\KopiaLens"),
                (HERE, r"C:\Program Files\KopiaLens"),
                (os.path.expanduser("~"), r"C:\Users\demo"))
else:
    DISPLAY_CONFIG = "/home/demo/.config/kopia/repository.config"
    _DISPLAY = ((DATA_DIR, "/home/demo/.local/share/kopialens"),
                (HERE, "/opt/kopialens"),
                (os.path.expanduser("~"), "/home/demo"))

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}


def machine_paths() -> list[str]:
    """This PC's own folders in every spelling a reply can carry (raw, escaped once or twice,
    forward slashes), lowercased, longest first."""
    return [p.lower() for p, _d in _variants()]


@functools.lru_cache(maxsize=1)
def _variants() -> tuple[tuple[str, str], ...]:
    out = []
    for real, shown in sorted(_DISPLAY, key=lambda p: -len(p[0])):
        real = os.path.normpath(real)
        if len(real) <= 3:   # never rewrite a bare drive
            continue
        # Backslashes x4 (a path in an error message, then JSON), x2 (JSON), x1, and forward.
        for n in (4, 2, 1):
            out.append((real.replace("\\", "\\" * n), shown.replace("\\", "\\" * n)))
        out.append((real.replace("\\", "/"), shown.replace("\\", "/")))
    return tuple(dict.fromkeys(out))


def scrub(body: bytes) -> bytes:
    """Replace this PC's own folders with stand-ins (a demo instance's replies)."""
    for real, shown in _variants():
        body = re.sub(re.escape(real.encode()), lambda _m, s=shown.encode(): s, body, flags=re.I)
    return body


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
