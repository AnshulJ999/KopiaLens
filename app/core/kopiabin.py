"""Locate the kopia program - the ONE place that decides which binary runs.

Before this module every kopia command was built with the bare string ``kopia``: no configured
path, no existence check, no useful failure - and a KopiaUI user HAS kopia but not necessarily on
PATH (KopiaUI bundles it under its own install folder). Resolution order:

  1. ``KOPIALENS_KOPIA`` env (a test/Docker override)
  2. ``kopialens.kopia_path`` in settings (the Settings control)
  3. PATH
  4. the usual install locations for this OS
  5. the binary KopiaUI bundles
  6. a copy shipped next to a packaged KopiaLens

Filesystem reads only (no spawn) and cached in memory; ``invalidate()`` after the setting changes.
``version()`` spawns once and is DISPLAY ONLY - nothing gates on a kopia version
(``TESTED_VERSION`` is what this release was verified against).

``exe()`` falls back to ``"kopia"`` when nothing resolves, so a missing binary fails the same way
it always did - except the failure now carries a message a user can act on.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading

from .applog import get_logger

log = get_logger("kopiabin")

ENV_OVERRIDE = "KOPIALENS_KOPIA"
TESTED_VERSION = "0.23.1"   # the build this release is verified against; display only, never a gate
EXE_NAME = "kopia.exe" if os.name == "nt" else "kopia"

_lock = threading.Lock()
_version_lock = threading.Lock()
_resolved: str | None = None
_source = ""
_version: str | None = None
_version_read = False


def _env(name: str) -> str:
    return os.environ.get(name) or ""


def _kopiaui_bundles() -> list[str]:
    """kopia.exe inside a KopiaUI install (the bundle path is ``resources/server/``)."""
    out: list[str] = []
    if os.name == "nt":
        for base in (_env("ProgramFiles"), _env("ProgramFiles(x86)"), _env("LOCALAPPDATA")):
            if base:
                out.append(os.path.join(base, "KopiaUI", "resources", "server", EXE_NAME))
        if _env("LOCALAPPDATA"):
            out.append(os.path.join(_env("LOCALAPPDATA"), "Programs", "KopiaUI", "resources",
                                    "server", EXE_NAME))
    elif sys.platform == "darwin":
        out.append("/Applications/KopiaUI.app/Contents/Resources/server/kopia")
        out.append(os.path.expanduser("~/Applications/KopiaUI.app/Contents/Resources/server/kopia"))
    else:
        # electron-builder's deb/rpm install under /opt; an AppImage mount point cannot be
        # predicted, so PATH and the config-dir installs are the practical answers there.
        out.append("/opt/KopiaUI/resources/server/kopia")
    return out


def _usual_locations() -> list[str]:
    if os.name == "nt":
        return [
            os.path.join(_env("ProgramFiles"), "kopia", EXE_NAME),
            os.path.join(_env("LOCALAPPDATA"), "Microsoft", "WinGet", "Links", EXE_NAME),
            os.path.join(os.path.expanduser("~"), "scoop", "shims", EXE_NAME),
            os.path.join(_env("ProgramData"), "chocolatey", "bin", EXE_NAME),
            os.path.join(os.path.expanduser("~"), "go", "bin", EXE_NAME),
        ]
    if sys.platform == "darwin":
        return ["/opt/homebrew/bin/kopia", "/usr/local/bin/kopia",
                os.path.expanduser("~/.local/bin/kopia")]
    return ["/usr/bin/kopia", "/usr/local/bin/kopia", "/snap/bin/kopia",
            os.path.expanduser("~/.local/bin/kopia"), "/opt/kopia/kopia"]


def _bundled_copies() -> list[str]:
    """A kopia shipped next to a packaged KopiaLens (Nuitka standalone, Docker image, ...)."""
    out: list[str] = []
    roots: list[str] = []
    for raw in (sys.executable, sys.argv[0] if sys.argv else ""):
        if raw:
            roots.append(os.path.dirname(os.path.abspath(raw)))
    for root in roots:
        if root and root not in out:
            out.append(root)
    paths: list[str] = []
    for root in out:
        paths.append(os.path.join(root, EXE_NAME))
        paths.append(os.path.join(root, "kopia", EXE_NAME))
        paths.append(os.path.join(root, "resources", "kopia", EXE_NAME))
    return paths


def inventory() -> list[dict]:
    """Every kopia program actually present on this machine, with its version.

    Explicit request only (one spawn per candidate), for the Settings "Kopia program" row and the
    one-time "a different kopia was found" data. The resolved one is marked `in_use`; `newest`
    flags the highest version seen, so a caller can say "KopiaUI uses 0.24, KopiaLens uses 0.23"
    without deciding anything here."""
    resolved = resolve()
    out = []
    for path, src in candidates():
        try:
            if not os.path.isfile(path):
                continue
        except OSError:
            continue
        entry = {"path": path, "source": src, "version": "",
                 "in_use": path == resolved, "newest": False}
        try:
            res = subprocess.run(
                [path, "--version"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=20, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            entry["version"] = _first_token((res.stdout or res.stderr or "").strip())
        except Exception:  # noqa: BLE001 - one unreadable candidate must not hide the others
            pass
        out.append(entry)

    def _ver(entry: dict) -> tuple:
        parts = str(entry["version"]).split(".")
        return tuple(int(p) if p.isdigit() else 0 for p in (parts + ["0", "0", "0"])[:3])

    if out and max(_ver(e) for e in out) > (0, 0, 0):
        max(out, key=_ver)["newest"] = True
    return out


def explicit_setting() -> str:
    """The path saved in settings, if any (lazy import - settings reads config at import time)."""
    try:
        from ..settings import load_settings
        return str(load_settings().get("kopialens", {}).get("kopia_path") or "")
    except Exception:  # noqa: BLE001 - a settings problem must not break resolution
        return ""


def candidates() -> list[tuple[str, str]]:
    """(path, source) pairs in the order they are tried. Existence is checked by ``resolve()``;
    this list is also what the Settings screen shows when nothing works.

    The BUNDLED copy comes first (after an explicit choice): a release pins the kopia it was tested
    with, so "works out of the box" does not depend on what a user happens to have installed. The
    machine's own copies stay in the list - a power user can select one, and Settings can point out
    that KopiaUI ships a different version."""
    out: list[tuple[str, str]] = []

    def add(path: str, source: str) -> None:
        path = (path or "").strip()
        if path and all(p != path for p, _ in out):
            out.append((path, source))

    add(os.environ.get(ENV_OVERRIDE) or "", "environment")
    add(explicit_setting(), "setting")
    for path in _bundled_copies():
        add(path, "bundled")
    found = shutil.which("kopia")
    if found:
        add(found, "PATH")
    for path in _usual_locations():
        add(path, "install location")
    for path in _kopiaui_bundles():
        add(path, "KopiaUI")
    return out


def resolve(force: bool = False) -> str | None:
    """The kopia executable to use, or None when nothing on this machine has it. Cached."""
    global _resolved, _source
    with _lock:
        if _resolved is not None and not force:
            return _resolved
        _resolved, _source = None, ""
        for path, source in candidates():
            try:
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    _resolved, _source = path, source
                    break
            except OSError:
                continue
        if _resolved:
            log.info("kopia binary: %s (from %s)", _resolved, _source)
        else:
            log.warning("kopia binary not found - searched %d locations (PATH, install locations, "
                        "KopiaUI bundle)", len(candidates()))
        return _resolved


def exe() -> str:
    """The command prefix for every kopia call. Falls back to the bare name so a missing binary
    fails loudly at spawn time instead of silently changing behaviour."""
    return resolve() or "kopia"


def source() -> str:
    resolve()
    return _source


def _first_token(text: str) -> str:
    """`kopia --version` prints `<version> build: <sha> from: kopia/kopia` - the version is the
    first token, which is what a screen shows and what a comparison wants."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line.split()[0][:40] if line else ""


def version(force: bool = False) -> str | None:
    """The kopia version (e.g. "0.23.1"), read ONCE per process. Display only - nothing gates on
    it. None when the binary cannot be run."""
    global _version, _version_read
    if _version_read and not force:
        return _version
    with _version_lock:                       # one spawn even under concurrent first calls
        if _version_read and not force:
            return _version
        path = resolve()
        _version, text = None, ""
        if path:
            try:
                res = subprocess.run(
                    [path, "--version"], capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=20, stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                text = (res.stdout or res.stderr or "").strip()
            except Exception as exc:  # noqa: BLE001 - a version read must never raise
                log.warning("kopia --version failed: %s", exc)
        _version = _first_token(text) or None
        _version_read = True
    return _version


def invalidate() -> None:
    """Drop the cached resolution + version (after the setting is written, or a path test)."""
    global _resolved, _source, _version, _version_read
    with _lock:
        _resolved, _source, _version, _version_read = None, "", None, False


def check(path: str) -> dict:
    """Validate one explicit path without touching the saved setting. It must run AND report a
    version-looking string, so picking any random executable is refused here rather than turning
    into a pile of failing kopia calls later."""
    path = (path or "").strip()
    out = {"path": path, "ok": False, "version": None, "detail": ""}
    if not path:
        out["detail"] = "Enter the full path to the kopia program."
        return out
    if not os.path.isfile(path):
        out["detail"] = "That file does not exist."
        return out
    try:
        res = subprocess.run(
            [path, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        out["detail"] = f"That file could not be run: {exc}"
        return out
    text = (res.stdout or res.stderr or "").strip()
    if res.returncode != 0 or not text:
        out["detail"] = (text or f"Exited with {res.returncode}")[:200]
        return out
    token = _first_token(text)
    if not re.match(r"^\d+\.\d+", token):
        out["detail"] = "That file did not report a kopia version - is it the kopia program?"
        return out
    out["ok"] = True
    out["version"] = token
    return out


def info(with_version: bool = True, with_candidates: bool = False) -> dict:
    """What ``/api/repos`` and the Settings screen show: resolved path, where it came from, and
    (once read) the version. ``with_version=False`` never spawns; ``with_candidates`` adds the
    full search list - off by default because /api/repos is polled and building that list reads
    the settings file. It is always included when the binary is missing: that is the message.
    ``inventory()`` (every kopia found, with versions) is a separate call - it spawns per copy."""
    path = resolve()
    ver = version() if (with_version and path) else _version
    searched = [{"path": p, "source": s} for p, s in candidates()] if (with_candidates or not path) else []
    return {
        "available": bool(path),
        "path": path or "",
        "source": source(),
        "version": ver or "",
        "tested": TESTED_VERSION,
        "bundled": bool(path) and source() == "bundled",
        "searched": searched,
        "detail": "" if path else ("Kopia was not found. Install Kopia or KopiaUI, or set the "
                                   "path to the kopia program in Settings."),
    }
