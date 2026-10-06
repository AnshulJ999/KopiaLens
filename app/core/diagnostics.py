"""Diagnostics bundle: what support needs to answer "what went wrong", anonymised by default.

The summary (app, system, licence tier, repositories) is always included; logs, the action
history and settings are the user's choice. Anonymising replaces paths, user and host names,
emails, addresses and repository ids with short tokens derived from this install's id, so the
same folder reads the same in two bundles from one user. Never included: passwords, keys,
licence tokens, the encryption key.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sys
import time
import zipfile

from config import APP_VERSION, DATA_DIR, REPOS

from .applog import INDEX_LOG_DIR, LOG_FILE

SECTIONS = ("app_log", "index_logs", "history", "settings")
LOG_LINES = 1500
INDEX_LOG_LINES = 300
HISTORY_ENTRIES = 300

# Setting keys whose values never leave the computer.
_SECRET_KEYS = re.compile(r"(password|hash|secret|token|key|salt|cert)", re.I)


class _Anon:
    """Deterministic replacement of private strings, keyed to this install."""

    _PATTERNS = (
        ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
        ("url", re.compile(r"https?://[^\s\"'<>]+")),
        ("user", re.compile(r"[\w.-]+(?=@[\w.-]+:)")),                   # user@host:path sources
        ("host", re.compile(r"(?<=@)[\w.-]+(?=:)")),
        # Windows, spaces included; also doubled separators (paths inside repr'd or escaped text).
        ("path", re.compile(r"\b[A-Za-z]:\\{1,2}(?:[^\\\r\n\"'<>|:*?]+\\{0,2})*")),
        ("path", re.compile(r"\\\\[^\s\"'<>|]+")),                        # UNC
        ("path", re.compile(r"(?<![\w:/.])/(?!api/|static/|v1/|health\b)(?:[^/\s\"'<>|]+/)+[^\s\"'<>|]*")),
        ("ip", re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b")),
    )

    def __init__(self, salt: str):
        self.salt = salt.encode("utf-8")
        self.words: list[tuple[str, str]] = []
        for kind, word in (("user", getpass.getuser()), ("host", socket.gethostname())):
            if word and len(word) >= 3:
                self.words.append((kind, word))

    def token(self, kind: str, value: str) -> str:
        digest = hmac.new(self.salt, value.lower().encode("utf-8"), hashlib.sha256).hexdigest()[:6]
        return f"<{kind}-{digest}>"

    # Free text the user typed or named: replaced whole, whatever it looks like.
    _TEXT_KEYS = {"query": "text", "filename": "name", "actor": "user"}

    def walk(self, obj):
        """Anonymise every string (keys included) of parsed JSON data."""
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                kind = self._TEXT_KEYS.get(k)
                if kind and isinstance(v, str) and v and v != "admin":
                    out[k] = self.token(kind, v)
                else:
                    out[self(k) if isinstance(k, str) else k] = self.walk(v)
            return out
        if isinstance(obj, list):
            return [self.walk(v) for v in obj]
        return self(obj) if isinstance(obj, str) else obj

    def __call__(self, text: str) -> str:
        for kind, rx in self._PATTERNS:
            text = rx.sub(lambda m, k=kind: self.token(k, m.group(0)), text)
        for kind, word in self.words:
            text = re.sub(re.escape(word), lambda m, k=kind: self.token(k, m.group(0)), text, flags=re.I)
        return text


def _licence_lines() -> list[str]:
    from ..licensing import licence
    st = licence.status()
    state = st.get("state", "none")
    tier = "Free" if state != "active" else f"Pro ({st.get('plan') or 'pro'})"
    lines = [f"Licence: {tier}", f"Licence state: {state}"]
    if state != "none":
        lines += [f"Licence id: {st.get('id')}", f"Licence source: {st.get('source')}",
                  f"Licence expires: {st.get('expires') or 'never'}",
                  f"Licence covers updates through: {getattr(licence.current(), 'updates_through', None) or 'all versions'}"]
    return lines


def _system_lines() -> list[str]:
    from . import checkin, kopiabin
    lines = [f"KopiaLens version: {APP_VERSION}", f"Install type: {checkin.install_type()}",
             f"OS: {platform.platform()}", f"CPU: {platform.machine()} x{os.cpu_count()}",
             f"Python: {sys.version.split()[0]}"]
    try:
        import psutil
        lines.append(f"Memory: {psutil.virtual_memory().total // (1024 ** 2)} MB")
    except Exception:  # noqa: BLE001
        pass
    try:
        lines.append(f"Free disk (data folder): {shutil.disk_usage(DATA_DIR).free // (1024 ** 3)} GB")
    except OSError:
        pass
    try:
        import duckdb
        lines.append(f"DuckDB: {duckdb.__version__}")
    except Exception:  # noqa: BLE001
        pass
    lines.append(f"Kopia: {kopiabin.version() or 'not found'} ({kopiabin.source() or 'unknown'})")
    try:
        from ..index import crypto
        lines.append(f"Encryption: {crypto.level()}")
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import netaccess
        lines.append(f"Access: {netaccess.mode()}")
    except Exception:  # noqa: BLE001
        pass
    return lines


def _repo_lines() -> list[str]:
    from . import repos as repos_mod
    from .runhistory import read_tail
    lines = []
    for rid, rec in REPOS.items():
        kind = rec.get("kind", "kopia")
        if kind != "kopia":
            lines.append(f"Repository {rid}: {kind}")
            continue
        info = repos_mod.storage_info(rid)
        line = f"Repository {rid}: kopia, storage {info.get('type') or 'unknown'}"
        if repos_mod.via_server(rid):
            line += ", through a kopia server"
        if rec.get("demo"):
            line += ", sample data"
        runs = read_tail(1, repos=[rid])
        if runs:
            r = runs[0]
            line += (f", last index {r.get('ts', '?')} {'ok' if r.get('ok') else 'failed'}"
                     f" ({r.get('trigger', '?')}, {float(r.get('elapsed') or 0):.0f} s)")
        lines.append(line)
    return lines


_LINE_START = re.compile(r"^\d{2,4}[-:]\d{2}")


def _tail(path: str, n: int) -> str:
    """The last ``n`` lines, starting at a whole log entry rather than mid-traceback."""
    from . import filecrypto
    if not os.path.exists(path):
        return ""
    lines = filecrypto.read_log_tail(path, n)
    first = next((i for i, line in enumerate(lines) if _LINE_START.match(line)), 0)
    return "\n".join(lines[first:])


def _settings_data() -> dict:
    from ..settings import load_settings

    def scrub(obj):
        if isinstance(obj, dict):
            return {k: ("(removed)" if _SECRET_KEYS.search(k) and v not in ("", None, False, [], {}) else scrub(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj
    return scrub(load_settings())


def build(sections, anonymise: bool = True, bundle_id: str | None = None) -> dict:
    """{"id", "files": {name: text}} for the chosen sections; the summary is always first."""
    from . import checkin
    bundle_id = bundle_id if bundle_id and re.fullmatch(r"[0-9a-f]{8}", bundle_id) else secrets.token_hex(4)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
    head = [f"KopiaLens diagnostics bundle {bundle_id}, created {stamp}",
            f"Anonymised: {'yes' if anonymise else 'no'}", ""]
    summary = "\n".join(head + _licence_lines() + [""] + _system_lines() + [""] + _repo_lines()) + "\n"
    files = {"summary.txt": summary}
    chosen = [s for s in SECTIONS if s in set(sections or ())]
    if "app_log" in chosen:
        files["app-log.txt"] = _tail(LOG_FILE, LOG_LINES)
    if "index_logs" in chosen and os.path.isdir(INDEX_LOG_DIR):
        for name in sorted(os.listdir(INDEX_LOG_DIR)):
            if name.endswith(".log"):
                files[f"index-{name.removesuffix('.log').removeprefix('index_worker_')}.txt"] = _tail(os.path.join(INDEX_LOG_DIR, name), INDEX_LOG_LINES)
    anon = _Anon(checkin.install_id()) if anonymise else None
    if anon:
        files = {name: anon(text) for name, text in files.items()}
    # JSON is anonymised before it is written: as text its backslashes are escaped, which splits
    # Windows paths for the text rules.
    keep = anon.walk if anon else (lambda o: o)
    if "history" in chosen:
        from .audit import read_tail as audit_tail
        files["history.jsonl"] = "\n".join(json.dumps(keep(e)) for e in audit_tail(HISTORY_ENTRIES))
    if "settings" in chosen:
        files["settings.json"] = json.dumps(keep(_settings_data()), indent=2)
    if anon:
        files["summary.txt"] = re.sub(r"(?m)^(Licence id: )(.+)$", lambda m: m.group(1) + anon.token("licence", m.group(2)),
                                      files["summary.txt"])
    return {"id": bundle_id, "files": files}


def zip_bytes(bundle: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in bundle["files"].items():
            zf.writestr(name, text)
    return buf.getvalue()
