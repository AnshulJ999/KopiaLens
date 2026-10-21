"""Daily check-in: update check + optional anonymous usage stats, plus the weekly revoked-licence list.

One request a day at most, only when one of the two settings is on. What is sent is documented in
docs/usage-stats.md - keep the two in step. Never sent by a demo copy or the web demo.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import secrets
import sys
import threading
import time
import urllib.request

from appversion import order_key
from config import APP_VERSION, CHECKIN_URL, COMPILED, DATA_DIR, DEMO_INSTANCE, REPO_URL, REPOS, is_demo, repo_record

from .applog import get_logger

log = get_logger("checkin")

STATE_FILE = os.path.join(DATA_DIR, "checkin.json")
CHECK_INTERVAL = 24 * 3600
FIRST_CHECK_DELAY = 60          # let startup finish first
LOOP_SLEEP = 3600               # re-evaluated hourly: the settings can change at runtime
RETRY_AFTER_FAILURE = 6 * 3600
TIMEOUT = 10
VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")
# The updater's public key: the same text as plugins.updater.pubkey in shell/src-tauri/tauri.conf.json.
UPDATE_PUBKEY = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEVCM0Q0QjhFQTBBN0EyNEQKUldSTm9xZWdqa3M5NndIQ0pzUnhwaUpaaW1VZ0gvVFduVE9UT0hCeDFiU1RJSDUvbUlCcGQyMXcK"

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


def channel() -> str:
    """The update channel: "beta" also offers beta versions; anything else is "stable"."""
    return "beta" if _kl().get("update_channel") == "beta" else "stable"


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
    from . import basepath
    if basepath.ingress_enabled():          # set only by the Home Assistant add-on
        return "ha_addon"
    if os.path.exists("/.dockerenv"):
        return "docker"
    if not COMPILED:
        return "source"
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def _os_name() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def _tier() -> str:
    """The plan as a short label: "Pro · Yearly" -> pro-yearly; a Polar subscription adds -sub."""
    from ..licensing import licence
    st = licence.status()
    if st.get("state") != "active":
        return "free"
    tier = re.sub(r"[^a-z0-9]+", "-", str(st.get("plan") or "pro").lower()).strip("-") or "pro"
    if st.get("source") == "polar" and st.get("renewable"):
        tier += "-sub"
    return tier[:30]


_GB = 1024 ** 3
_INDEX_CLASSES = ((0.1 * _GB, "under_100mb"), (_GB, "100mb_1gb"), (5 * _GB, "1_5gb"), (20 * _GB, "5_20gb"))
_MEMORY_CLASSES = ((4 * _GB, "under_4gb"), (8 * _GB, "4_8gb"), (16 * _GB, "8_16gb"))


def _size_class(size: int, classes: tuple, top: str) -> str:
    return next((name for limit, name in classes if size < limit), top)


def _index_bytes(repos: list[str]) -> int:
    """KopiaLens's own index on disk (the repoN folders), never anything from the backups."""
    from ..index import store
    total = 0
    for r in repos:
        for root, _dirs, files in os.walk(store.repo_dir(r)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    return total


def _stats_state() -> str:
    """Why usage stats are or aren't sent: on, off, or waiting (first day, or a data folder that isn't kept)."""
    if not stats_enabled():
        return "off"
    return "on" if stats_ready() else "waiting"


def build_payload(include_stats: bool) -> dict:
    payload = {"schema": 2, "version": APP_VERSION, "install_type": install_type(), "os": _os_name(),
               "channel": channel()}
    if not include_stats:
        payload["stats"] = _stats_state()
        if payload["stats"] == "off" and _read_state().get("stats_last") is True:
            payload["stats_turned_off"] = True
        return payload
    from ..api.onboarding import needed as setup_needed
    from ..index import store
    from . import host, kopiabin
    from . import repos as repos_mod
    kopia = [r for r, c in list(REPOS.items()) if c.get("kind", "kopia") == "kopia" and not is_demo(r)]
    storages = sorted({repos_mod.storage_info(r).get("type") or "unknown" for r in kopia})
    with _lock:
        used, opened = sorted(_used), _opened
    kopia_info = kopiabin.info()
    ram = host.ram_bytes()
    payload.update(
        install_id=install_id(),
        arch=platform.machine().lower(),
        tier=_tier(),
        repos=len(kopia),
        storage_types=storages,
        backblaze=any(c.get("kind") == "backblaze" for c in list(REPOS.values())),
        opened=opened,
        features_used=used,
        setup="not_yet" if setup_needed() else "finished",
        first_index=any(os.path.exists(store.manifest_path(r)) for r in kopia),
        kopia_version=kopia_info.get("version") or "unknown",
        kopia_bundled=bool(kopia_info.get("bundled")),
        own_server=any(repo_record(r).get("engine") == "kopialens" for r in kopia),
        index_class=_size_class(_index_bytes(kopia), _INDEX_CLASSES, "over_20gb"),
        memory_class=_size_class(ram, _MEMORY_CLASSES, "over_16gb") if ram else "unknown",
    )
    failed_at = _read_state().get("update_failed_at")
    if isinstance(failed_at, str) and failed_at:
        payload["update_failed_at"] = failed_at
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


def signed(data: bytes, signature: str, pubkey: str | None = None) -> bool:
    """True when ``signature`` (a minisign .sig file, base64, as `tauri signer sign` writes it) is a
    valid signature of ``data`` by the updater key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pubkey = UPDATE_PUBKEY if pubkey is None else pubkey
    try:
        pk = base64.b64decode(base64.b64decode(pubkey).decode("utf-8").splitlines()[1])
        lines = base64.b64decode(signature).decode("utf-8").splitlines()
        sig, comment, global_sig = base64.b64decode(lines[1]), lines[2], base64.b64decode(lines[3])
    except (ValueError, IndexError, TypeError, UnicodeDecodeError):
        return False
    prefix = "trusted comment: "
    if len(pk) != 42 or pk[:2] != b"Ed" or len(sig) != 74 or sig[2:10] != pk[2:10] or not comment.startswith(prefix):
        return False
    # "ED" signs a BLAKE2b-512 hash of the data (minisign's default); "Ed" signs the data itself.
    message = {b"ED": hashlib.blake2b(data, digest_size=64).digest(), b"Ed": data}.get(sig[:2])
    if message is None:
        return False
    key = Ed25519PublicKey.from_public_bytes(pk[10:])
    try:
        key.verify(sig[10:], message)
        key.verify(global_sig, sig[10:] + comment[len(prefix):].encode("utf-8"))
    except InvalidSignature:
        return False
    return True


def _notice(data: dict) -> str | None:
    """The reply's notice, only when signed offline with the updater key: the Worker holds no key,
    so whoever controls the domain or the Worker cannot put text in front of users."""
    notice, sig = data.get("notice"), data.get("notice_sig")
    if not isinstance(notice, str) or not notice:
        return None
    if isinstance(sig, str) and signed(notice.encode("utf-8"), sig):
        return notice
    log.warning("check-in notice ignored: not signed with the updater key")
    return None


def release_url(version: str) -> str:
    """Built here, never taken from the reply, so a notice can't point at someone else's download."""
    return f"{REPO_URL}/releases/tag/v{version}"


def _clean_reply(data: dict) -> dict:
    def ver(v):
        return v if isinstance(v, str) and VERSION_RE.match(v) else None
    by_major = data.get("latest_by_major")
    return {
        "latest": ver(data.get("latest")),
        "notice": _notice(data),
        "latest_by_major": {str(k): ver(v) for k, v in by_major.items() if ver(v) and str(k).isdigit()}
                           if isinstance(by_major, dict) else {},
        "checked_at": time.time(),
    }


def check_now(manual: bool = False) -> bool:
    """One check-in when either setting is on. ``manual`` (Check now) always checks for updates:
    the setting only switches the daily automatic check. True on success."""
    global _opened
    want_updates, want_stats = updates_enabled() or manual, stats_enabled() and stats_ready()
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
    _write_state(last_checkin_at=now, retry_at=0, stats_last=stats_enabled(),
                 update_info={**_clean_reply(data), "manual": manual, "channel": channel()})
    if want_stats:
        _write_state(update_failed_at=None)
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
    return bool(a) and order_key(a) > order_key(b)


def status() -> dict:
    """What the UI needs: is an update out, where to get it, and whether the licence covers it."""
    enabled = updates_enabled()
    state = _read_state()
    info = state.get("update_info") if isinstance(state.get("update_info"), dict) else {}
    # An answer for the other channel no longer applies (the user switched since the last check).
    if info.get("channel", "stable") != channel():
        info = {}
    latest = info.get("latest")
    # With automatic checks off, only an answer the user asked for (Check now) is shown.
    shown = enabled or bool(info.get("manual"))
    available = bool(shown and _newer(latest, APP_VERSION))
    out = {
        "version": APP_VERSION,
        "channel": channel(),
        "checks_enabled": enabled,
        "stats_enabled": stats_enabled(),
        "available": available,
        "latest": latest if available else None,
        "url": release_url(latest) if available else None,
        "notice": info.get("notice") if shown else None,
        "checked_at": info.get("checked_at"),
        "licence": None,
        "can_install": available and in_app_install(),
        "install": dict(_install) if _install.get("state") else None,
    }
    if available:
        from ..licensing import licence
        upd = getattr(licence.current(), "updates_through", None)
        if upd is not None and (_major(latest) or 0) > upd:
            covered = (info.get("latest_by_major") or {}).get(str(upd))
            out["licence"] = {"covers_through": upd,
                              "latest_covered": covered if _newer(covered, APP_VERSION) else None}
    return out


# In-app installs: the desktop app sets KOPIALENS_IN_APP_UPDATES for an installed copy that can update
# itself (Tauri's updater), asks for the request through /api/shell/state and reports progress back.
_install: dict = {}
_install_requested = False


def in_app_install() -> bool:
    return os.environ.get("KOPIALENS_IN_APP_UPDATES") == "1"


def request_install() -> None:
    global _install_requested
    _install_requested = True


def install_requested() -> bool:
    return _install_requested


def set_install_progress(state: str, pct: int | None = None, error: str | None = None) -> None:
    """The desktop app's progress: waiting (for indexing or jobs), checking, downloading, installing,
    failed. Taking it up ends the request."""
    global _install_requested
    _install_requested = False
    if state == "failed":   # the step it stopped at goes out once with the next usage stats
        step = _install.get("state")
        _write_state(update_failed_at=step if step in ("waiting", "checking", "downloading", "installing") else "unknown")
    _install.clear()
    _install.update(state=state, pct=pct, error=error, at=time.time())


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
