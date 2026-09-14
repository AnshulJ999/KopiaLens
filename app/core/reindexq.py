"""Index update after deletes: wait for a quiet period, then run one incremental pass.

Each finished removal pushes the repo's due time out by ``reindex_after_delete_delay`` seconds,
so back-to-back deletes cost one pass. A pass that cannot start (another repo is indexing) stays
queued and retries; a pass of that repo that starts after the request satisfies it.
"""

from __future__ import annotations

import json
import os
import threading
import time

from config import DATA_DIR

from ..settings import load_settings
from .applog import get_logger

log = get_logger("reindexq")

DEFAULT_DELAY = 300
_TICK = 10.0

_PENDING: dict[str, dict] = {}   # repo -> {"requested": ts, "due": ts}
_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None


def _path() -> str:
    return os.path.join(DATA_DIR, "reindex_pending.json")


def _save() -> None:
    """Called under _LOCK. A restart must not drop a queued update."""
    try:
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_PENDING, fh)
        os.replace(tmp, _path())
    except OSError as exc:
        log.warning("could not save the index update queue: %s", exc)


def load() -> None:
    """App startup: resume updates queued before the last exit."""
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    with _LOCK:
        for repo, rec in (data or {}).items():
            if isinstance(rec, dict) and "requested" in rec and "due" in rec:
                _PENDING[str(repo)] = {"requested": float(rec["requested"]), "due": float(rec["due"])}
    if _PENDING:
        log.info("resuming %d queued index update(s)", len(_PENDING))
        _ensure_thread()


def delay() -> int:
    try:
        return max(0, int(load_settings().get("kopialens", {}).get(
            "reindex_after_delete_delay", DEFAULT_DELAY)))
    except Exception:  # noqa: BLE001
        return DEFAULT_DELAY


def request(repo: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _LOCK:
        rec = _PENDING.setdefault(repo, {"requested": now})
        rec["requested"] = now
        rec["due"] = now + delay()
        _save()
    _ensure_thread()


def touch(repo: str, now: float | None = None) -> None:
    """A new delete started: an already-queued pass waits for it too."""
    now = time.time() if now is None else now
    with _LOCK:
        rec = _PENDING.get(repo)
        if rec:
            rec["due"] = max(rec["due"], now + delay())
            _save()


def pending() -> list[dict]:
    with _LOCK:
        return [{"repo": r, "requested": v["requested"], "due": v["due"]} for r, v in _PENDING.items()]


def run_now(repo: str) -> dict:
    with _LOCK:
        rec = _PENDING.get(repo)
        if rec:
            rec["due"] = 0
    return _try_start(repo, force=True)


def _try_start(repo: str, force: bool = False) -> dict:
    from . import jobs, nightly
    with _LOCK:
        rec = _PENDING.get(repo)
    if not rec:
        return {"started": False, "reason": "nothing queued"}
    st = nightly.state(repo)
    if st.get("started") and st["started"] >= rec["requested"] and not st.get("running"):
        with _LOCK:
            _PENDING.pop(repo, None)        # a pass already ran after the delete
            _save()
        return {"started": False, "reason": "already up to date"}
    if not force and time.time() < rec["due"]:
        return {"started": False, "reason": "waiting"}
    busy = any(j["state"] == "running" and j["mutating"] and j["repo"] == repo for j in jobs.list_jobs())
    if busy:
        return {"started": False, "reason": "a delete is still running"}
    from ..api.actions import _start_index_run   # lazy: the api layer imports core
    r = _start_index_run(repo)
    if r.get("started"):
        with _LOCK:
            _PENDING.pop(repo, None)
            _save()
        log.info("repo=%s index update started after deletes", repo)
    return r


def _loop() -> None:
    while True:
        time.sleep(_TICK)
        for rec in pending():
            try:
                _try_start(rec["repo"])
            except Exception:  # noqa: BLE001 - one bad tick must not kill the queue
                log.exception("repo=%s queued index update failed to start", rec["repo"])


def _ensure_thread() -> None:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_loop, name="reindexq", daemon=True)
        _THREAD.start()
