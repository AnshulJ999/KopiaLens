"""Data-folder version + the upgrade routine ().

One version stamp for the whole data folder, a registry of steps that move a folder from N to N+1,
and a refusal to touch a folder written by a NEWER KopiaLens.

Why a folder-level stamp when the index already carries one: most of the folder has no version at
all - `settings.json`, the side caches, the small state files, policy backups. A format change in
one of those used to be silently ignored or misread (the 2026-09-17 60-second folder panel was
exactly that). The index keeps its OWN stamp (`app/index/upgrade.py`), because a generation can be
migrated per repo without touching the folder version.

Rule: **migrate by default; rebuild only what is cheap and loses nothing.**
So a step must rewrite in place. A step that would need new data from kopia is not a folder step at
all - it belongs to the index's own migration, which may declare a rebuild.

Step contract (what a registered step may assume):
* it runs at most once per folder, guarded by the version it upgrades FROM;
* every file it writes goes through `atomic_json` / `atomic_bytes` (tmp + replace), so a crash
  leaves the old file intact and the step simply runs again next start;
* it returns a small report dict (counts, what it changed) - the routine logs it and keeps it in
  the status for the UI;
* it never deletes: superseded information is archived or left alone.

Adding a step (how A2's encryption conversion plugs in): write `def my_step(ctx) -> dict`, call
`register(from_version=2, fn=my_step, name="...", description="...")` at import time, and add your
module to `_STEP_MODULES` below so the routine imports it. Bump `DATA_VERSION` by one.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time

from config import DATA_DIR, SETTINGS_FILE

from .applog import get_logger

log = get_logger("dataupgrade")

# The folder format THIS app writes. Bump it in the release that changes a file layout, together
# with a registered step that migrates the previous version.
DATA_VERSION = 2

VERSION_FILE = os.path.join(DATA_DIR, "data_version.json")

# Settings keys that were REMOVED from the app. The routine drops them from a user's file so a
# hand-edited or long-lived settings.json cannot accumulate keys nothing reads. Add a pair
# ("block", "key") in the release that removes the key - this list is version-independent on
# purpose: a dead key costs nothing to skip, so it needs no folder-version dance.
DEAD_SETTINGS_KEYS: tuple[tuple[str, str], ...] = ()

# Modules that register upgrade steps at import time (a step needs the app's own code, so the
# registry cannot know them statically - this is the ONE line a new step's author edits).
_STEP_MODULES: tuple[str, ...] = ("app.core.encryption_upgrade",)

_STEPS: dict[int, dict] = {}
_LOCK = threading.Lock()
_STATE: dict = {"running": False, "last": None, "error": ""}


def register(from_version: int, fn, name: str, description: str = "") -> None:
    """Register the step that moves a folder from `from_version` to `from_version + 1`."""
    with _LOCK:
        _STEPS[int(from_version)] = {"fn": fn, "name": name, "description": description}


def _load_steps() -> None:
    for module in _STEP_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - a broken step module must not stop the app
            log.error("upgrade step module %s could not be loaded: %s", module, exc)


def steps() -> list[dict]:
    _load_steps()
    with _LOCK:
        return [{"from": v, "to": v + 1, "name": s["name"], "description": s["description"]}
                for v, s in sorted(_STEPS.items())]


def read_version() -> dict:
    """The folder's version. A folder with no stamp predates versioning and IS version 1 (the
    layout this app wrote before the stamp existed)."""
    try:
        with open(VERSION_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        version = int(data.get("version"))
        return {"version": version, "at": float(data.get("at") or 0), "unversioned": False}
    except (OSError, ValueError, TypeError):
        return {"version": 1, "at": 0.0, "unversioned": True}


def _write_version(version: int) -> None:
    atomic_json(VERSION_FILE, {"version": int(version), "app": DATA_VERSION, "at": time.time()})


def atomic_json(path: str, data: object) -> None:
    """Write JSON atomically (tmp + fsync + replace). The step contract's only writer."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_bytes(path: str, data: bytes) -> None:
    """Write bytes atomically. Same contract as `atomic_json`, for non-JSON files."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def prune_dead_settings() -> list[str]:
    """Drop keys the app no longer reads. Returns what it removed (empty = nothing to do).
    Reads the FILE as written, so nothing a user never chose gets stamped back."""
    if not DEAD_SETTINGS_KEYS:
        return []
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    removed = []
    for block, key in DEAD_SETTINGS_KEYS:
        target = data.get(block) if block else data
        if isinstance(target, dict) and key in target:
            target.pop(key, None)
            removed.append(f"{block}.{key}" if block else key)
    if removed:
        atomic_json(SETTINGS_FILE, data)
        log.warning("settings: removed dead key(s) %s", ", ".join(removed))
    return removed


def status() -> dict:
    """What the UI needs: the folder's version, the app's, whether the folder is NEWER than this
    app (refused), which steps are pending, and how the last run went."""
    _load_steps()          # a pending step must be visible before the startup thread imports them
    info = read_version()
    with _LOCK:
        known = sorted(_STEPS.items())
        state = dict(_STATE)
    pending = [{"from": v, "to": v + 1, "name": s["name"]}
               for v, s in known if info["version"] < v + 1 <= DATA_VERSION]
    newer = info["version"] > DATA_VERSION
    return {
        "version": info["version"],
        "app_version": DATA_VERSION,
        "unversioned": info["unversioned"],
        "newer": newer,              # the folder came from a newer KopiaLens: do not touch it
        "pending": pending,
        "running": bool(state.get("running")),
        "last": state.get("last"),
        "error": state.get("error") or "",
        "detail": (f"This data folder was written by a newer KopiaLens (version {info['version']}; "
                   f"this app writes {DATA_VERSION}). Nothing will be migrated and the version "
                   f"stamp is left untouched; use the newer app, or point KopiaLens at a different "
                   f"data folder.") if newer else "",
    }


def run_pending() -> dict:
    """Run every registered step whose target version is above the folder's. Each step is guarded
    and the version advances only after it completes, so a failed step is retried on the next
    start instead of being skipped. Never raises: the app keeps running either way."""
    info = read_version()
    if info["version"] > DATA_VERSION:
        log.error("data folder is from a NEWER KopiaLens (v%s vs v%s) - no migrations are run and "
                  "the version is left untouched", info["version"], DATA_VERSION)
        return {"version": info["version"], "ran": [], "newer": True}
    _load_steps()
    if info["unversioned"]:
        # A pre-version folder IS this version's layout; stamp it so the next release's steps know
        # where to start from.
        _write_version(info["version"])
        log.info("data folder stamped at version %s (it predates the version stamp)", info["version"])
    ran = []
    with _LOCK:
        _STATE.update({"running": True, "error": ""})
    try:
        version = info["version"]
        while version < DATA_VERSION:
            with _LOCK:
                step = _STEPS.get(version)
            if step is None:
                log.warning("no upgrade step registered for data version %s -> %s; the folder is "
                            "left at %s", version, version + 1, version)
                break
            t0 = time.time()
            try:
                report = step["fn"]() or {}
            except Exception as exc:  # noqa: BLE001 - a failed step must not brick the app
                log.error("data upgrade step %r (v%s -> v%s) failed: %s", step["name"], version,
                          version + 1, exc, exc_info=True)
                with _LOCK:
                    _STATE["error"] = f"{step['name']}: {exc}"
                break
            version += 1
            _write_version(version)
            elapsed = round(time.time() - t0, 2)
            ran.append({"from": version - 1, "to": version, "name": step["name"],
                        "seconds": elapsed, "report": report})
            log.warning("data upgrade step %r done (v%s -> v%s, %.2fs) %s", step["name"],
                        version - 1, version, elapsed, report or "")
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["last"] = ran[-1] if ran else None
    removed = prune_dead_settings()
    if removed:
        ran.append({"from": None, "to": None, "name": "settings-cleanup",
                    "report": {"removed": removed}, "seconds": 0.0})
    return {"version": read_version()["version"], "ran": ran, "newer": False}


def _background() -> None:
    try:
        out = run_pending()
        if out.get("ran"):
            log.info("data folder upgrade finished: %s", [r["name"] for r in out["ran"]])
    except Exception as exc:  # noqa: BLE001 - the routine must never take the app down
        log.error("data folder upgrade failed: %s", exc, exc_info=True)


def start_background() -> None:
    """Called from the app's startup. The newer-folder refusal is decided here (instantly) and the
    steps themselves run in the background - a step that rewrites a large file must not delay the
    server coming up."""
    info = read_version()
    if info["version"] > DATA_VERSION:
        # "Refuse" is scoped to the MIGRATION: nothing is migrated and the version stamp is left
        # alone. The rest of the app still writes its own files (settings, logs, side caches) - the
        # user-facing banner is's, and `status()['newer']` is what it reads.
        log.error("REFUSING to migrate: the data folder is from a newer KopiaLens (v%s vs v%s) - "
                  "no step runs and the version stamp is untouched. Use the newer app.", info["version"],
                  DATA_VERSION)
        return
    threading.Thread(target=_background, name="data-upgrade", daemon=True).start()
