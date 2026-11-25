"""Index resume: an index run saves what it read from kopia so a stopped or killed run continues.

Two save points, both in ``_staging/``:

1. During the tree read: the build's own scratch DuckDB (``build.duckdb``) plus ``resume.json``.
2. When the read is finished: the merged tree and the object id dictionary as Parquet in
   ``tree_saved/``, which the next run loads like a published tree and reads only what is new.

Either belongs to one published index (``base_of``): a save made on top of another generation is
dropped. kopia objects never change, so folders read earlier stay correct after new snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

from ..core import filecrypto
from ..core.applog import get_logger
from ..core import atomicfile

log = get_logger("store")

FORMAT = 2
MAX_ATTEMPTS = 3        # resumes in a row that did not finish before the saved state is dropped
TREE_FILES = ("tdir", "tent", "tpart", "cid")
_BASE_ARTIFACTS = ("tdir", "tent", "tpart", "cid", "src", "dir", "anc", "snap", "isnaps")


def base_of(prev: dict | None) -> str | None:
    """The published index a run builds on: its tree, dictionaries and id counters."""
    if prev is None:
        return None
    art = prev.get("artifacts") or {}
    key = {name: art.get(name) for name in _BASE_ARTIFACTS}
    key["counters"] = prev.get("counters")
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def path(staging: str) -> str:
    return os.path.join(staging, "resume.json")


def stop_flag(repo: str) -> str:
    """Written by KopiaLens to ask a running index to save and stop."""
    from config import DATA_DIR
    return os.path.join(DATA_DIR, f"index_stop_repo{repo}.flag")


def stop_requested(repo: str) -> bool:
    return os.path.exists(stop_flag(repo))


def request_stop(repo: str) -> None:
    try:
        with open(stop_flag(repo), "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
    except OSError as exc:
        log.warning("repo=%s could not write the stop request: %s", repo, exc)


def clear_stop(repo: str) -> None:
    try:
        os.remove(stop_flag(repo))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("repo=%s could not clear the stop request: %s", repo, exc)


def _read(p: str) -> dict:
    text = filecrypto.read_text(p)          # encrypted at the Full level, like the other state files
    if text is None:
        raise ValueError("cannot be read or decrypted")
    state = json.loads(text)
    if not isinstance(state, dict):
        raise ValueError("not a saved state")
    return state


def _rejected(state: dict, repo: str, encrypted: bool, base: str | None) -> str:
    if state.get("format") != FORMAT:
        return "saved by another version"
    if str(state.get("repo")) != str(repo):
        return "saved for another repository"
    if bool(state.get("encrypted")) != bool(encrypted):
        return "the encryption setting changed"
    if state.get("base") != base:
        return "the index it continued has changed since"
    if int(state.get("attempts") or 0) >= MAX_ATTEMPTS:
        return f"{MAX_ATTEMPTS} resumes in a row did not finish"
    return ""


def load(staging: str, repo: str, encrypted: bool, base: str | None = None) -> dict | None:
    """Save point 1 when it belongs to this build, else None (and it is dropped)."""
    p = path(staging)
    if not os.path.exists(p):
        return None
    try:
        state = _read(p)
    except ValueError as exc:
        log.warning("repo=%s saved index progress unreadable (%s) - starting over", repo, exc)
        clear(staging)
        return None
    why = _rejected(state, repo, encrypted, base)
    if not why and not os.path.exists(os.path.join(staging, "build.duckdb")):
        why = "its working file is missing"
    if why:
        log.warning("repo=%s saved index progress not used (%s)", repo, why)
        clear(staging)
        return None
    state["attempts"] = int(state.get("attempts") or 0) + 1
    _write(p, state)
    return state


def save(staging: str, state: dict) -> None:
    _write(path(staging), {**state, "format": FORMAT, "saved_at": time.time()})


def clear(staging: str) -> None:
    try:
        os.remove(path(staging))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove saved index progress: %s", exc)


# ---- save point 2: the finished read -------------------------------------------------------

def tree_dir(staging: str) -> str:
    return os.path.join(staging, "tree_saved")


def _stamp(d: str) -> str:
    return os.path.join(d, "stamp.json")


def _settle(staging: str) -> None:
    """Finish a save that was cut off between its two renames."""
    final = tree_dir(staging)
    if os.path.exists(_stamp(final)):
        return
    for leftover in (final + ".tmp", final + ".old"):
        if os.path.exists(_stamp(leftover)):
            shutil.rmtree(final, ignore_errors=True)
            atomicfile.replace(leftover, final)
            return


def save_tree(staging: str, write, state: dict) -> dict[str, str]:
    """Write save point 2: ``write(name, path)`` copies one table out, the stamp goes last, then the
    folder replaces the previous save in two renames. Save point 1 is dropped once this is on disk.
    Returns the saved file of each table."""
    final = tree_dir(staging)
    tmp, old = final + ".tmp", final + ".old"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for name in TREE_FILES:
        write(name, os.path.join(tmp, f"{name}.parquet"))
    _write(_stamp(tmp), {**state, "format": FORMAT, "saved_at": time.time()})
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(final):
        atomicfile.replace(final, old)
    atomicfile.replace(tmp, final)
    shutil.rmtree(old, ignore_errors=True)
    clear(staging)
    return {name: os.path.join(final, f"{name}.parquet") for name in TREE_FILES}


def tree_saved_at(staging: str) -> float:
    try:
        return float(_read(_stamp(tree_dir(staging))).get("saved_at") or 0)
    except (OSError, ValueError):
        return 0.0


def load_tree(staging: str, repo: str, encrypted: bool, base: str | None) -> dict | None:
    """Save point 2 when it belongs to this build (with ``files``: table -> saved file), else None."""
    _settle(staging)
    p = _stamp(tree_dir(staging))
    if not os.path.exists(p):
        return None
    try:
        state = _read(p)
    except ValueError as exc:
        log.warning("repo=%s saved tree read unreadable (%s) - reading it again", repo, exc)
        clear_tree(staging)
        return None
    files = {name: os.path.join(tree_dir(staging), f"{name}.parquet") for name in TREE_FILES}
    why = _rejected(state, repo, encrypted, base)
    if not why and not all(os.path.exists(f) for f in files.values()):
        why = "a saved file is missing"
    if why:
        log.warning("repo=%s saved tree read not used (%s)", repo, why)
        clear_tree(staging)
        return None
    state["attempts"] = int(state.get("attempts") or 0) + 1
    _write(p, state)
    return {**state, "files": files}


def unattempt_tree(staging: str) -> None:
    """A stop is not a failed resume: give back the attempt the run took."""
    p = _stamp(tree_dir(staging))
    try:
        state = _read(p)
        state["attempts"] = max(0, int(state.get("attempts") or 0) - 1)
        _write(p, state)
    except (OSError, ValueError):
        pass


def clear_tree(staging: str) -> None:
    final = tree_dir(staging)
    for d in (final, final + ".tmp", final + ".old"):
        shutil.rmtree(d, ignore_errors=True)


def _write(p: str, state: dict) -> None:
    if not filecrypto.write_text(p, json.dumps(state)):
        raise OSError("the saved index progress could not be written")
