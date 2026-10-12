"""Index resume: a first index saves its progress so a stopped or killed run continues later.

The saved state is the build's own scratch DuckDB (``_staging/build.duckdb``) plus ``resume.json``
next to it. kopia objects never change, so folders read earlier stay correct even after new
snapshots. 
"""

from __future__ import annotations

import json
import os
import time

from ..core.applog import get_logger

log = get_logger("store")

FORMAT = 1
MAX_ATTEMPTS = 3        # resumes in a row that did not finish before the saved state is dropped


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


def load(staging: str, repo: str, encrypted: bool) -> dict | None:
    """The saved state when it belongs to this build, else None (and it is dropped)."""
    p = path(staging)
    try:
        with open(p, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("repo=%s saved index progress unreadable (%s) - starting over", repo, exc)
        clear(staging)
        return None
    why = ""
    if state.get("format") != FORMAT:
        why = "saved by another version"
    elif str(state.get("repo")) != str(repo):
        why = "saved for another repository"
    elif bool(state.get("encrypted")) != bool(encrypted):
        why = "the encryption setting changed"
    elif not os.path.exists(os.path.join(staging, "build.duckdb")):
        why = "its working file is missing"
    elif int(state.get("attempts") or 0) >= MAX_ATTEMPTS:
        why = f"{MAX_ATTEMPTS} resumes in a row did not finish"
    if why:
        log.warning("repo=%s saved index progress not used (%s) - starting over", repo, why)
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


def _write(p: str, state: dict) -> None:
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
