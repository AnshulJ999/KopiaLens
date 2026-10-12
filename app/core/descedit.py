"""Snapshot description edits on a repository whose backups another app runs.

kopia changes a description only through its server. Where KopiaLens runs no server for the
repository, a short-lived one is started on a COPY of the connection file under another hostname,
so it schedules nothing and is never the maintenance owner; it is stopped and the copy removed
right after the edit. The copy can't find kopia's saved password (it is keyed by the original
file's path), so the password kopia saved for the original file is read here and handed to the
temporary server through its environment. It is never stored, logged or returned.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

import httpx

from .applog import get_logger

log = get_logger("descedit")

EDIT_HOSTNAME = "kopialens-edit"
CONNECT_TIMEOUT = 300.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class PasswordNeeded(Exception):
    """kopia's saved password was not found, or did not open the repository."""


# ---- kopia's saved password (internal/passwordpersist) ---------------------------------------

def _item_id(config_file: str) -> str:
    """kopia's keyring item id: ``<file name>-<first 8 bytes of sha256(path) in hex>``."""
    digest = hashlib.sha256(config_file.encode("utf-8")).hexdigest()[:16]
    return f"{os.path.basename(config_file)}-{digest}"


def _spellings(config_file: str) -> list[str]:
    """Spellings of one path kopia may have hashed (the app that connected chose it)."""
    out = [config_file, os.path.normpath(config_file), os.path.abspath(config_file)]
    if len(config_file) > 1 and config_file[1] == ":":
        out += [config_file[0].upper() + config_file[1:], config_file[0].lower() + config_file[1:]]
    return list(dict.fromkeys(out))


def _from_file(config_file: str) -> str | None:
    path = config_file + ".kopia-password"
    try:
        with open(path, encoding="utf-8") as fh:
            return base64.b64decode(fh.read().strip()).decode("utf-8") or None
    except (OSError, ValueError):
        return None


def _windows_credentials(prefix: str) -> dict[str, str]:
    """Generic credentials whose target starts with ``prefix``: {target: secret}."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    count = wintypes.DWORD()
    creds = ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
    if not advapi.CredEnumerateW(prefix + "*", 0, ctypes.byref(count), ctypes.byref(creds)):
        return {}
    out = {}
    try:
        for i in range(count.value):
            c = creds[i].contents
            if c.Type != 1:  # CRED_TYPE_GENERIC
                continue
            blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
            try:
                out[c.TargetName] = blob.decode("utf-8")
            except UnicodeDecodeError:
                continue
    finally:
        advapi.CredFree(creds)
    return out


def _from_windows(config_file: str) -> str | None:
    """go-keyring stores ``<item id>:<user>``; the user part is the Windows user without domain."""
    base = os.path.basename(config_file)
    found = _windows_credentials(base + "-")
    if not found:
        return None
    ids = {_item_id(p) for p in _spellings(config_file)}
    for target, secret in found.items():
        if target.split(":", 1)[0] in ids:
            return secret
    if len(found) == 1:  # one entry for this file name: the path was spelled differently
        return next(iter(found.values()))
    return None


def _from_macos(config_file: str) -> str | None:
    import getpass
    for p in _spellings(config_file):
        try:
            res = subprocess.run(["security", "find-generic-password", "-s", _item_id(p), "-a", getpass.getuser(), "-w"],
                                 capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.rstrip("\n")
    return None


def saved_password(config_file: str) -> str | None:
    """The repository password kopia saved for this connection file, or None."""
    pw = _from_file(config_file)
    if pw:
        return pw
    try:
        if sys.platform == "win32":
            return _from_windows(config_file)
        if sys.platform == "darwin":
            return _from_macos(config_file)
    except Exception as exc:  # noqa: BLE001 - a missing keyring falls back to asking
        log.warning("reading kopia's saved password failed: %s", type(exc).__name__)
    return None


# ---- the temporary server ----------------------------------------------------------------------

def _copy_config(config_file: str, folder: str) -> str:
    with open(config_file, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "storage" not in data:
        raise RuntimeError("Couldn't read this repository's connection file.")
    data["hostname"] = EDIT_HOSTNAME
    data.setdefault("caching", {})["cacheDirectory"] = os.path.join(folder, "cache")
    path = os.path.join(folder, "repository.config")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def _stop(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin:
            proc.stdin.close()      # --shutdown-on-stdin
        proc.wait(timeout=20)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def edit(repo: str, snapshot_id: str, description: str, password: str | None = None) -> None:
    """Set one snapshot's description through a temporary kopia server. Raises PasswordNeeded
    when no password opens the repository (the caller asks the user once)."""
    from . import coexist, kopiabin
    from .engine import _free_port
    from .kopia import _kopia_exe
    if not kopiabin.resolve():
        raise RuntimeError("kopia was not found")
    original = coexist.config_path(repo)
    pw = password or saved_password(original)
    if not pw:
        raise PasswordNeeded()
    folder = tempfile.mkdtemp(prefix="kopialens-edit-")
    proc = None
    try:
        copy = _copy_config(original, folder)
        port = _free_port()
        ui_pw = secrets.token_urlsafe(24)
        env = dict(os.environ)
        env.update({"KOPIA_PASSWORD": pw, "KOPIA_SERVER_PASSWORD": ui_pw,
                    "KOPIA_SERVER_CONTROL_PASSWORD": secrets.token_urlsafe(24), "KOPIA_CHECK_FOR_UPDATES": "false"})
        cmd = [_kopia_exe(), "--config-file", copy, "server", "start", "--insecure", "--disable-csrf-token-checks",
               "--no-grpc", f"--address=127.0.0.1:{port}", "--shutdown-on-stdin"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, creationflags=_NO_WINDOW)
        del env, pw
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", auth=("kopia", ui_pw),
                          timeout=httpx.Timeout(30.0, connect=2.0)) as ui:
            deadline = time.time() + CONNECT_TIMEOUT
            while True:
                if proc.poll() is not None:
                    raise PasswordNeeded() if not password else RuntimeError(
                        "kopia couldn't open the repository with that password.")
                try:
                    st = ui.get("/api/v1/repo/status").json()
                    if st.get("connected"):
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                if time.time() > deadline:
                    raise RuntimeError("kopia took too long to open the repository.")
                time.sleep(0.3)
            r = ui.post("/api/v1/snapshots/edit", json={"snapshots": [snapshot_id], "description": description})
            if r.status_code >= 400:
                raise RuntimeError("kopia refused the description change.")
        log.info("repo=%s description edited through a temporary server", repo)
    finally:
        if proc is not None:
            _stop(proc)
        shutil.rmtree(folder, ignore_errors=True)
