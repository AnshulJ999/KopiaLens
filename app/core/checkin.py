"""Daily check-in: update check + optional anonymous usage stats, plus the weekly revoked-licence list.

One request a day at most, only when one of the two settings is on. What is sent is documented in
docs/usage-stats.md - keep the two in step. Never sent by a demo copy or the web demo.
"""
from __future__ import annotations

import json
import os
import platform
import re
import secrets
import sys
import threading
import time
import urllib.request

from config import APP_VERSION, CHECKIN_URL, COMPILED, DATA_DIR, DEMO_INSTANCE, REPOS, is_demo

from .applog import get_logger

log = get_logger("checkin")

STATE_FILE = os.path.join(DATA_DIR, "checkin.json")
CHECK_INTERVAL = 24 * 3600
FIRST_CHECK_DELAY = 60          # let startup finish first
LOOP_SLEEP = 3600               # re-evaluated hourly: the settings can change at runtime
RETRY_AFTER_FAILURE = 6 * 3600
TIMEOUT = 10
VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")

_lock = threading.Lock()
# Names only, since the last check-in: which screens and actions were used. Never counts or paths.
_used: set[str] = set()
_opened = False

# Request path prefix -> feature name. First match wins, so longer prefixes come first.
_FEATURES = (
    ("/api/search/export", "csv_export"), ("/api/search/breakdown", "storage_breakdown"),
    ("/api/search", "search"), ("/api/dashboard", "dashboard"), ("/api/snapshots", "snapshots"),
    ("/api/tree", "tree"), ("/api/churn", "churn"), ("/api/simulate", "simulate"),
    ("/api/stats", "repository"), ("/api/policies", "policies"), ("/api/excluded", "excluded"),
    ("/api/logs", "logs"), ("/api/backblaze", "backblaze"), ("/api/prune", "prune"),
    ("/api/delete", "delete"), ("/api/restore", "restore"), ("/api/diagnostics", "diagnostics"),
)


def _env_off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _kl() -> dict:
    from ..settings import load_settings
    return load_settings().get("kopialens", {}) or {}


def updates_enabled() -> bool:
    return not _env_off("KOPIALENS_NO_UPDATE_CHECK") and bool(_kl().get("update_check", True))


def stats_enabled() -> bool:
    return not _env_off("KOPIALENS_NO_USAGE_STATS") and bool(_kl().get("usage_stats", True))


def note_request(path: str) -> None:
    """Called for each successful API request (the timing middleware)."""
    global _opened
    if path.startswith("/api/shell"):        # the tray's polling is not the app being used
        return
    with _lock:
        _opened = True
        for prefix, name in _FEATURES:
            if path.startswith(prefix):
                _used.add(name)
                break


def note_feature(name: str) -> None:
    """A feature the request path can't name (a job kind, for example)."""
    with _lock:
        _used.add(name)


def _read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(**changes) -> dict:
    with _lock:
        state = _read_state()
        state.update(changes)
        tmp = STATE_FILE + ".tmp"
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_FILE)
        return state


def install_id() -> str:
    """A random id for this install, created on first use. Not derived from the machine or user."""
    iid = _read_state().get("install_id")
    if isinstance(iid, str) and re.fullmatch(r"[0-9a-f]{32}", iid):
        return iid
    return _write_state(install_id=secrets.token_hex(16), install_created_at=time.time())["install_id"]


STATS_AFTER = 24 * 3600


def stats_ready() -> bool:
    """Usage stats start once the install id is a day old and the data folder survives a restart,
    so a container recreated without a volume (a new id each time) is never counted."""
    from .host import data_persistent
    if not data_persistent():
        return False
    install_id()
    created = _read_state().get("install_created_at")
    if not isinstance(created, (int, float)):
        created = _write_state(install_created_at=time.time())["install_created_at"]
    return time.time() - created >= STATS_AFTER


def install_type() -> str:
    if os.path.exists("/.dockerenv"):
        return "docker"
    if not COMPILED:
        return "source"
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def _os_name() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def _tier() -> str:
    from ..licensing import licence
    st = licence.status()
    if st.get("state") != "active":
        return "free"
    plan = str(st.get("plan") or "pro").lower()
    return plan if re.fullmatch(r"[a-z0-9_-]{1,30}", plan) else "pro"


def build_payload(include_stats: bool) -> dict:
    payload = {"schema": 1, "version": APP_VERSION, "install_type": install_type(), "os": _os_name()}
    if include_stats:
        from . import repos as repos_mod
        kopia = [r for r, c in REPOS.items() if c.get("kind", "kopia") == "kopia" and not is_demo(r)]
        storages = sorted({repos_mod.storage_info(r).get("type") or "unknown" for r in kopia})
        with _lock:
            used, opened = sorted(_used), _opened
        payload.update(
            install_id=install_id(),
            arch=platform.machine().lower(),
            tier=_tier(),
            repos=len(kopia),
            storage_types=storages,
            backblaze=any(c.get("kind") == "backblaze" for c in REPOS.values()),
            opened=opened,
            features_used=used,
        )
    return payload


def _post(payload: dict) -> dict | None:
    req = urllib.request.Request(CHECKIN_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"KopiaLens/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if resp.status != 200:
            return None
        data = json.loads(resp.read(65536))
    return data if isinstance(data, dict) else None


def _clean_reply(data: dict) -> dict:
    def ver(v):
        return v if isinstance(v, str) and VERSION_RE.match(v) else None
    by_major = data.get("latest_by_major")
    return {
        "latest": ver(data.get("latest")),
        "url": data.get("url") if isinstance(data.get("url"), str) else None,
        "notice": data.get("notice") if isinstance(data.get("notice"), str) else None,
        "latest_by_major": {str(k): ver(v) for k, v in by_major.items() if ver(v) and str(k).isdigit()}
                           if isinstance(by_major, dict) else {},
        "checked_at": time.time(),
    }


def check_now() -> bool:
    """One check-in when either setting is on. True on success."""
    global _opened
    want_updates, want_stats = updates_enabled(), stats_enabled() and stats_ready()
    if not (want_updates or want_stats) or DEMO_INSTANCE:
        return False
    try:
        data = _post(build_payload(want_stats))
    except Exception as exc:  # noqa: BLE001 - offline is normal
        log.debug("check-in failed: %s", exc)
        data = None
    now = time.time()
    if data is None:
        _write_state(retry_at=now + RETRY_AFTER_FAILURE)
        return False
    _write_state(last_checkin_at=now, retry_at=0, update_info=_clean_reply(data))
    if want_stats:
        with _lock:
            _used.clear()
            _opened = False
    return True


def _major(version: str | None) -> int | None:
    try:
        return int(str(version).split(".")[0])
    except (TypeError, ValueError):
        return None


def _newer(a: str | None, b: str) -> bool:
    def key(v):
        return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", v or "0"))
    return bool(a) and key(a) > key(b)


def status() -> dict:
    """What the UI needs: is an update out, where to get it, and whether the licence covers it."""
    enabled = updates_enabled()
    state = _read_state()
    info = state.get("update_info") if isinstance(state.get("update_info"), dict) else {}
    latest = info.get("latest")
    available = bool(enabled and _newer(latest, APP_VERSION))
    out = {
        "version": APP_VERSION,
        "checks_enabled": enabled,
        "stats_enabled": stats_enabled(),
        "available": available,
        "latest": latest if available else None,
        "url": info.get("url") if available else None,
        "notice": info.get("notice") if enabled else None,
        "checked_at": info.get("checked_at"),
        "licence": None,
    }
    if available:
        from ..licensing import licence
        upd = getattr(licence.current(), "updates_through", None)
        if upd is not None and (_major(latest) or 0) > upd:
            covered = (info.get("latest_by_major") or {}).get(str(upd))
            out["licence"] = {"covers_through": upd,
                              "latest_covered": covered if _newer(covered, APP_VERSION) else None}
    return out


def _due() -> bool:
    state = _read_state()
    now = time.time()
    return now - (state.get("last_checkin_at") or 0) >= CHECK_INTERVAL and now >= (state.get("retry_at") or 0)


def _loop() -> None:
    time.sleep(FIRST_CHECK_DELAY)
    while True:
        try:
            if _due():
                check_now()
        except Exception as exc:  # noqa: BLE001 - never takes the app down
            log.debug("check-in loop error: %s", exc)
        try:
            from ..licensing import revoked
            revoked.refresh_if_due()
        except Exception as exc:  # noqa: BLE001
            log.debug("revoked-licence refresh error: %s", exc)
        time.sleep(LOOP_SLEEP)


def start() -> None:
    if DEMO_INSTANCE or sys.platform == "emscripten":
        return
    threading.Thread(target=_loop, name="checkin", daemon=True).start()
