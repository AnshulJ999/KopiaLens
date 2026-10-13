"""Backups of KopiaLens's own configuration files (never the kopia repositories).

Copies live in ``<config dir>/config-backups/<stamp>-<kind>/`` with an ``info.json``:
- change   the state before a burst of edits (at most one per CHANGE_GAP)
- weekly   once a week
- upgrade  before a new app version or a data-folder upgrade runs
- manual   "Back up now"
- safety   taken automatically before a restore or an import
Each kind keeps its own count (KEEP). Every successful save of settings, repos or accounts also
refreshes ``last-good/<file>``. A damaged file is moved aside and the newest good copy (last-good
first, then the backups) is put back, with a notice the UI shows until dismissed.

Stdlib only at import time: ``config.py`` calls ``recover`` while it is itself loading.
"""
from __future__ import annotations

import fnmatch
import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
import zlib

log = logging.getLogger("kopialens.configbackup")

GROUPS: dict[str, tuple[str, ...]] = {
    "settings": ("settings.json", "ui_state.json"),
    "repos": ("repos.json",),
    "accounts": ("users.json",),
    "keys": ("keystore.json", "keystore-*.kdb"),
}
DEFAULT_RESTORE = ("settings", "repos")
KEEP = {"change": 10, "weekly": 4, "upgrade": 3, "manual": 10, "safety": 5}
CHANGE_GAP = 600
WEEK = 7 * 86400
_ID = re.compile(r"^\d{8}-\d{6}(-\d+)?-(change|weekly|upgrade|manual|safety)$")
_MAX_FILE = 20 * 1024 * 1024
_IMPORT_TTL = 1800

_LOCK = threading.RLock()
_LAST_CHANGE = {"at": 0.0}
_IMPORTS: dict[str, dict] = {}


# ---------------------------------------------------------------------------- where things are

def _config():
    import config
    return config


def root() -> str:
    return os.path.join(_config().CONFIG_DIR, "config-backups")


def _state_path() -> str:
    return os.path.join(root(), "state.json")


def _target(name: str) -> str:
    """Where a backed-up file belongs on this install."""
    cfg = _config()
    if name == "settings.json":
        return cfg.SETTINGS_FILE
    if name == "repos.json":
        return cfg.REPOS_FILE
    return os.path.join(cfg.CONFIG_DIR, name)


def _group_of(name: str) -> str | None:
    for group, patterns in GROUPS.items():
        if any(fnmatch.fnmatch(name, p) for p in patterns):
            return group
    return None


def _live_files() -> dict[str, str]:
    """name -> path for every config file that exists now."""
    cfg = _config()
    out: dict[str, str] = {}
    for patterns in GROUPS.values():
        for pattern in patterns:
            if "*" in pattern:
                try:
                    for name in sorted(os.listdir(cfg.CONFIG_DIR)):
                        if fnmatch.fnmatch(name, pattern):
                            out[name] = os.path.join(cfg.CONFIG_DIR, name)
                except OSError:
                    pass
            elif os.path.isfile(_target(pattern)):
                out[pattern] = _target(pattern)
    return out


def _app_version() -> str:
    return getattr(_config(), "APP_VERSION", "")


# ---------------------------------------------------------------------------- small IO helpers

def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{secrets.token_hex(4)}"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _valid(name: str, data: bytes) -> bool:
    """A JSON config file must parse to an object; key files are taken as they are."""
    if not name.endswith(".json"):
        return bool(data)
    try:
        return isinstance(json.loads(data.decode("utf-8")), dict)
    except (UnicodeDecodeError, ValueError):
        return False


def _read_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    try:
        _atomic_write(_state_path(), json.dumps(state, indent=2).encode("utf-8"))
    except OSError as exc:
        log.warning("config backup state could not be saved: %s", exc)


# ---------------------------------------------------------------------------- backups

def _info(folder: str) -> dict | None:
    try:
        with open(os.path.join(folder, "info.json"), encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def list_backups() -> list[dict]:
    """Every backup, newest first."""
    out = []
    try:
        names = os.listdir(root())
    except OSError:
        return []
    for name in names:
        folder = os.path.join(root(), name)
        if not _ID.match(name) or not os.path.isdir(folder):
            continue
        info = _info(folder)
        if not info:
            continue
        files = sorted(f for f in os.listdir(folder) if f != "info.json")
        size = 0
        for f in files:
            try:
                size += os.path.getsize(os.path.join(folder, f))
            except OSError:
                pass
        out.append({
            "id": name, "kind": info.get("kind"), "at": info.get("at"),
            "app_version": info.get("app_version") or "", "note": info.get("note") or "",
            "files": files, "groups": sorted({g for f in files if (g := _group_of(f))}), "size": size,
        })
    out.sort(key=lambda b: (b.get("at") or 0, b["id"]), reverse=True)
    return out


def create(kind: str, note: str = "") -> dict | None:
    """Copy every config file into a new backup. Never raises; None when there is nothing to back
    up yet (a fresh install) or the copy failed."""
    if kind not in KEEP:
        raise ValueError(f"unknown backup kind: {kind}")
    with _LOCK:
        live = _live_files()
        if "settings.json" not in live and "users.json" not in live:
            return None      # a fresh install: nothing chosen by anyone yet
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name, n = f"{stamp}-{kind}", 1
        while os.path.exists(os.path.join(root(), name)):
            n += 1
            name = f"{stamp}-{n}-{kind}"
        final = os.path.join(root(), name)
        tmp = os.path.join(root(), f".tmp-{name}")
        try:
            os.makedirs(tmp, exist_ok=True)
            for fname, path in live.items():
                shutil.copy2(path, os.path.join(tmp, fname))
            info = {"kind": kind, "at": time.time(), "app_version": _app_version(), "note": note,
                    "files": sorted(live)}
            with open(os.path.join(tmp, "info.json"), "w", encoding="utf-8") as fh:
                json.dump(info, fh, indent=2)
            os.replace(tmp, final)
        except OSError as exc:
            log.warning("config backup (%s) failed: %s", kind, exc)
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        if kind == "change":
            _LAST_CHANGE["at"] = time.time()
        _prune(kind)
        log.info("config backup %s (%d file(s))", name, len(live))
        return next((b for b in list_backups() if b["id"] == name), None)


def _prune(kind: str) -> None:
    keep = KEEP[kind]
    for old in [b for b in list_backups() if b["kind"] == kind][keep:]:
        shutil.rmtree(os.path.join(root(), old["id"]), ignore_errors=True)


def remember(name: str) -> None:
    """After a successful save: keep this file as the last good copy. Never raises."""
    try:
        with open(_target(name), "rb") as fh:
            data = fh.read()
        if _valid(name, data):
            _atomic_write(os.path.join(root(), "last-good", name), data)
    except OSError as exc:
        log.debug("last good copy of %s not kept: %s", name, exc)


def before_write() -> None:
    """Called by the settings / repos / accounts writers before they write: keeps the state from
    before a burst of edits, at most one copy per CHANGE_GAP."""
    try:
        now = time.time()
        with _LOCK:
            if not _LAST_CHANGE["at"]:
                newest = next((b for b in list_backups() if b["kind"] == "change"), None)
                _LAST_CHANGE["at"] = float(newest["at"]) if newest else -1.0
            if now - _LAST_CHANGE["at"] < CHANGE_GAP:
                return
            create("change")
    except Exception as exc:  # noqa: BLE001 - a backup problem must never block a save
        log.warning("change backup failed: %s", exc)


def maybe_weekly() -> None:
    try:
        newest = next((b for b in list_backups() if b["kind"] == "weekly"), None)
        if not newest or time.time() - float(newest.get("at") or 0) >= WEEK:
            create("weekly")
    except Exception as exc:  # noqa: BLE001
        log.warning("weekly config backup failed: %s", exc)


def before_upgrade(note: str) -> None:
    """One copy before an upgrade; a second request within a minute (app version and data steps
    on the same start) reuses it."""
    try:
        newest = next((b for b in list_backups() if b["kind"] == "upgrade"), None)
        if newest and time.time() - float(newest.get("at") or 0) < 60:
            return
        create("upgrade", note=note)
    except Exception as exc:  # noqa: BLE001
        log.warning("upgrade config backup failed: %s", exc)


def on_startup() -> None:
    """Back up before the first run of a new app version, then the weekly check."""
    state = _read_state()
    version = _app_version()
    previous = state.get("last_version")
    if previous and previous != version:
        before_upgrade(f"before {previous} -> {version}")
    if previous != version:
        state["last_version"] = version
        _write_state(state)
    maybe_weekly()


# ---------------------------------------------------------------------------- restore + recovery

def _backup_folder(backup_id: str) -> str:
    if not _ID.match(backup_id or ""):
        raise ValueError("unknown backup")
    folder = os.path.join(root(), backup_id)
    if not os.path.isdir(folder):
        raise ValueError("unknown backup")
    return folder


def _put_back(files: dict[str, bytes], groups) -> list[str]:
    """Write the chosen groups' files into place. Every JSON file is checked first, so a broken
    copy never replaces a working one."""
    from .filelock import locked
    chosen = [n for n in sorted(files) if _group_of(n) in set(groups)]
    bad = [n for n in chosen if not _valid(n, files[n])]
    if bad:
        raise ValueError(f"damaged in the backup: {', '.join(bad)}")
    for name in chosen:
        path = _target(name)
        with locked(path):
            data = files[name]
            if name == "users.json":
                data = _secret_from(data, path)
            _atomic_write(path, data)
    return chosen


def _secret_from(data: bytes, current: str | None) -> bytes:
    """users.json carrying the secret that signs sign-in cookies from the file at ``current`` (this
    install's own), or none at all: an export never hands it out, and an import never replaces it.
    Without one, the account store makes a fresh secret on first read."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except ValueError:
        return data
    if not isinstance(doc, dict):
        return data
    doc.pop("secret", None)
    if current and os.path.isfile(current):
        try:
            with open(current, encoding="utf-8") as fh:
                mine = json.load(fh).get("secret")
        except (OSError, ValueError, AttributeError):
            mine = None
        if mine:
            doc["secret"] = mine
    return json.dumps(doc, indent=2).encode("utf-8")


def restore(backup_id: str, groups=DEFAULT_RESTORE) -> dict:
    folder = _backup_folder(backup_id)
    files = {}
    for name in os.listdir(folder):
        if name != "info.json" and _group_of(name):
            with open(os.path.join(folder, name), "rb") as fh:
                files[name] = fh.read()
    with _LOCK:
        safety = create("safety", note=f"before restoring {backup_id}")
        restored = _put_back(files, groups)
    log.warning("config restored from %s: %s", backup_id, ", ".join(restored) or "nothing")
    return {"restored": restored, "safety": safety["id"] if safety else None, "restart_needed": True}


def recover(name: str) -> bool:
    """A config file could not be read: keep it aside and put back the newest good copy. Returns
    True when a copy was restored. Never raises - the caller falls back to its own default."""
    try:
        path = _target(name)
        kept = None
        if os.path.exists(path):
            kept = f"{path}.damaged-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(path, kept)
        source = None
        last_good = os.path.join(root(), "last-good", name)
        candidates = [({"id": "last-good", "at": os.path.getmtime(last_good)}, last_good)
                      if os.path.exists(last_good) else None]
        candidates += [(b, os.path.join(root(), b["id"], name)) for b in list_backups()]
        for b, candidate in filter(None, candidates):
            try:
                with open(candidate, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if _valid(name, data):
                source = (b, data)
                break
        notice = {"id": secrets.token_hex(6), "at": time.time(), "file": name,
                  "kept_as": os.path.basename(kept) if kept else None}
        if source:
            _atomic_write(path, source[1])
            notice.update(kind="recovered", backup_id=source[0]["id"], backup_at=source[0]["at"])
            log.error("%s could not be read; restored the copy from %s (damaged file kept as %s)",
                      name, source[0]["id"], notice["kept_as"])
        else:
            notice.update(kind="reset")
            log.error("%s could not be read and no backup holds a good copy; starting from defaults "
                      "(damaged file kept as %s)", name, notice["kept_as"])
        with _LOCK:
            state = _read_state()
            state.setdefault("notices", []).append(notice)
            state["notices"] = state["notices"][-20:]
            _write_state(state)
        return bool(source)
    except Exception as exc:  # noqa: BLE001
        log.error("recovering %s failed: %s", name, exc)
        return False


def notices() -> list[dict]:
    return [n for n in _read_state().get("notices", []) if not n.get("dismissed")]


def dismiss(notice_id: str) -> None:
    with _LOCK:
        state = _read_state()
        for n in state.get("notices", []):
            if n.get("id") == notice_id:
                n["dismissed"] = True
        _write_state(state)


# ---------------------------------------------------------------------------- export / import

def export_zip(groups) -> bytes:
    groups = set(groups)
    buf = io.BytesIO()
    included = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in _live_files().items():
            if _group_of(name) in groups:
                if name == "users.json":
                    with open(path, "rb") as fh:
                        zf.writestr(name, _secret_from(fh.read(), None))
                else:
                    zf.write(path, name)
                included.append(name)
        zf.writestr("manifest.json", json.dumps({
            "app": "KopiaLens", "version": _app_version(), "at": time.time(),
            "groups": sorted(groups), "files": sorted(included)}, indent=2))
    return buf.getvalue()


def _summary(name: str, new: bytes) -> dict:
    """What importing this file would change, in terms a person can check."""
    try:
        incoming = json.loads(new.decode("utf-8")) if name.endswith(".json") else None
    except ValueError:
        incoming = None
    current = None
    path = _target(name)
    if name.endswith(".json") and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                current = json.load(fh)
        except (OSError, ValueError):
            current = None
    out: dict = {"file": name, "exists": os.path.exists(path)}
    if name == "settings.json" and isinstance(incoming, dict):
        cur = current if isinstance(current, dict) else {}
        changed = []
        for block in sorted(set(incoming) | set(cur)):
            a, b = cur.get(block), incoming.get(block)
            if isinstance(a, dict) or isinstance(b, dict):
                a, b = a or {}, b or {}
                changed += [f"{block}.{k}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
            elif a != b:
                changed.append(block)
        out["changed"] = changed
    elif name == "repos.json" and isinstance(incoming, dict):
        def names(store):
            return {str(r.get("id")): (r.get("name") or str(r.get("id")))
                    for r in (store or {}).get("repos", []) if isinstance(r, dict)}
        cur, new_ = names(current if isinstance(current, dict) else {}), names(incoming)
        out["added"] = sorted(new_[i] for i in set(new_) - set(cur))
        out["removed"] = sorted(cur[i] for i in set(cur) - set(new_))
        out["kept"] = sorted(new_[i] for i in set(new_) & set(cur))
    elif name == "users.json" and isinstance(incoming, dict):
        out["accounts"] = sorted(str(u.get("username")) for u in incoming.get("users", [])
                                 if isinstance(u, dict))
    return out


def import_preview(data: bytes) -> dict:
    """Read an exported file and say what importing it would change. Nothing is written."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        return _import_preview(zf)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error) as exc:
        # Damaged, password-protected or unusually compressed: none of these is our export.
        raise ValueError("not a KopiaLens settings export") from exc


def _import_preview(zf: zipfile.ZipFile) -> dict:
    files: dict[str, bytes] = {}
    with zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            raise ValueError("not a KopiaLens settings export")
        for info in zf.infolist():
            name = info.filename
            if name == "manifest.json":
                continue
            if "/" in name or "\\" in name or not _group_of(name):
                raise ValueError(f"unexpected file in the export: {name}")
            if info.file_size > _MAX_FILE:
                raise ValueError(f"{name} is too large")
            files[name] = zf.read(name)
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except ValueError as exc:
            raise ValueError("the export's manifest is damaged") from exc
    bad = [n for n in files if not _valid(n, files[n])]
    if bad:
        raise ValueError(f"damaged in the export: {', '.join(sorted(bad))}")
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _LOCK:
        for t in [t for t, v in _IMPORTS.items() if now - v["at"] > _IMPORT_TTL]:
            _IMPORTS.pop(t, None)
        while len(_IMPORTS) >= 5:
            _IMPORTS.pop(min(_IMPORTS, key=lambda t: _IMPORTS[t]["at"]))
        _IMPORTS[token] = {"at": now, "files": files}
    groups = sorted({_group_of(n) for n in files})
    return {"token": token, "version": manifest.get("version") or "", "exported_at": manifest.get("at"),
            "groups": groups, "files": [_summary(n, files[n]) for n in sorted(files)]}


def import_apply(token: str, groups) -> dict:
    with _LOCK:
        pending = _IMPORTS.pop(token, None)
    if not pending:
        raise ValueError("this import has expired; choose the file again")
    with _LOCK:
        safety = create("safety", note="before an import")
        restored = _put_back(pending["files"], groups)
    log.warning("config imported: %s", ", ".join(restored) or "nothing")
    return {"restored": restored, "safety": safety["id"] if safety else None, "restart_needed": True}
