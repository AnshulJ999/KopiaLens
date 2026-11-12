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

from config import APP_COMMIT, APP_VERSION, DATA_DIR, REPOS

from .applog import INDEX_LOG_DIR, LOG_FILE

SECTIONS = ("problems", "app_log", "index_logs", "history", "settings", "kopia_status")
LOG_LINES = 1500
INDEX_LOG_LINES = 300
HISTORY_ENTRIES = 300
JOB_ENTRIES = 100
PROBLEM_GROUPS = 300
FAILED_RUNS = 3

_PROBLEM_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+(WARNING|ERROR|CRITICAL)\s+(\S+) \| (.*)$")
# Index worker logs carry only the time; the date comes from the run header above each run.
_RUN_HEADER = re.compile(r"^===== run started (\d{4}-\d{2}-\d{2}) ")
_INDEX_PROBLEM = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+(WARNING|ERROR|CRITICAL) (.*)$")

STATUS_TIMEOUT = 20   # seconds per repo: an offline remote repository must not hold the bundle

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
        ("ip", re.compile(r"(?<![/\w.])(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b")),
        # IPv6, with an optional prefix length: needs "::" or 6+ groups, so clock times never match.
        ("ip", re.compile(r"(?<![\w:.])(?!::1?(?![\w:]))(?:(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}::"
                          r"(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}|(?:[0-9A-Fa-f]{1,4}:){5,7}[0-9A-Fa-f]{1,4})"
                          r"(?:/\d{1,3})?(?![\w:])")),
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
    _TEXT_KEYS = {"query": "text", "filename": "name", "actor": "user",
                  "allowed_hosts": "host", "trusted_proxies": "ip", "access_addresses": "ip"}

    def walk(self, obj):
        """Anonymise every string (keys included) of parsed JSON data."""
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                kind = self._TEXT_KEYS.get(k)
                if kind and isinstance(v, str) and v and v != "admin":
                    out[k] = self.token(kind, v)
                elif kind and isinstance(v, list):
                    out[k] = [self.token(kind, x) if isinstance(x, str) and x else x for x in v]
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
    lines = [f"KopiaLens version: {APP_VERSION}" + (f" (commit {APP_COMMIT})" if APP_COMMIT else ""),
             f"Install type: {checkin.install_type()}",
             f"OS: {platform.platform()}", f"CPU: {platform.machine()} x{os.cpu_count()}",
             f"Python: {sys.version.split()[0]}"]
    from .host import _cgroup_memory_limit, ram_bytes
    if ram := ram_bytes():
        capped = ram == _cgroup_memory_limit()
        lines.append(f"Memory: {ram // (1024 ** 2)} MB" + (" (container limit)" if capped else ""))
    try:
        from ..index.store import resolve_limits
        from ..settings import load_settings
        limit, threads = resolve_limits()
        kl = load_settings().get("kopialens") or {}
        how = ("set in Settings" if kl.get("duckdb_memory")
               else "set by KOPIALENS_DUCKDB_MEMORY" if os.environ.get("KOPIALENS_DUCKDB_MEMORY")
               else "automatic")
        lines.append(f"Database memory limit: {limit} ({how}), {threads} threads "
                     f"({'set in Settings' if kl.get('duckdb_threads') else 'automatic'})")
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


def _index_sizes() -> dict:
    try:
        from .instrument import memory_snapshot
        return memory_snapshot().get("repos", {})
    except Exception:  # noqa: BLE001
        return {}


def _bz_drive_lines() -> list[str]:
    """Each Backblaze drive: its filelist's size, whether it is an aborted scan, whether the index holds it.
    A Mac drive is numbered, never named: its name is the user's own volume name."""
    try:
        from ..backblaze import parsers, paths, pq, queries
        if not paths.exists():
            return ["  Backblaze data folder not found"]
        parquet = queries._store() == "parquet"
        mac = paths.platform() == "mac"
        out = []
        for n, vol in enumerate(paths.volumes(), 1):
            try:
                size = f"{os.path.getsize(vol.filelist)} bytes"
            except OSError:
                size = "missing"
            state = "aborted scan" if parsers.is_stub_filelist(vol.filelist) else "ok"
            name = ("Startup disk" if vol.mount == paths.POSIX_SEP else f"External drive {n}") if mac else f"Drive {vol.key}"
            line = f"  {name}: filelist {size}, {state}"
            if parquet:
                line += ", in the index" if pq.has_drive(vol.key) else ", not in the index"
            out.append(line)
        return out
    except Exception as exc:  # noqa: BLE001 - the bundle never fails over one section
        return [f"  Backblaze drives could not be read: {exc}"]


def _repo_lines() -> list[str]:
    from . import repos as repos_mod
    from .runhistory import read_tail
    sizes = _index_sizes()
    lines = []
    for rid, rec in list(REPOS.items()):
        kind = rec.get("kind", "kopia")
        if kind != "kopia":
            lines.append(f"Repository {rid}: {kind}")
            if kind == "backblaze":
                lines += _bz_drive_lines()
            continue
        info = repos_mod.storage_info(rid)
        line = f"Repository {rid}: kopia, storage {info.get('type') or 'unknown'}"
        if repos_mod.via_server(rid):
            line += ", through a kopia server"
        if rec.get("demo"):
            line += ", sample data"
        runs = read_tail(200, repos=[rid])
        if runs:
            r = runs[0]                                   # newest first
            line += (f", last index {r.get('ts', '?')} {'ok' if r.get('ok') else 'failed'}"
                     f" ({r.get('trigger', '?')}, {float(r.get('elapsed') or 0):.0f} s)")
        lines.append(line)
        cap = info.get("capacity") or {}
        if cap.get("capacity"):
            lines.append(f"  Storage: {(cap.get('available') or 0) // 1024 ** 3} GB free of {cap['capacity'] // 1024 ** 3} GB")
        s = sizes.get(rid) or {}
        if "files" in s:
            lines.append(f"  Index: {s.get('snapshots')} snapshots, {s.get('sources')} sources, {s['files']} file versions,"
                         f" {s['paths']} paths, {s.get('content_refs')} content refs")
        for r in [r for r in runs if not r.get("ok")][:FAILED_RUNS]:
            why = r.get("reason") or r.get("errors") or "?"
            lines.append(f"  Failed index {r.get('ts', '?')} ({r.get('trigger', '?')}): {str(why)[:300]}")
    return lines


def _changed_settings(anon=None) -> list[str]:
    from ..settings import DEFAULT_SETTINGS, load_settings
    current, defaults = load_settings(), DEFAULT_SETTINGS
    out = []

    def show(k, v):
        if _SECRET_KEYS.search(k):
            return "(set)"
        v = anon.walk({k: v})[k] if anon else v
        return v if isinstance(v, str) else json.dumps(v)
    for key in sorted(set(current) | set(defaults)):
        cur, dflt = current.get(key), defaults.get(key)
        if isinstance(cur, dict) and isinstance(dflt, dict):
            for sub in sorted(set(cur) | set(dflt)):
                if cur.get(sub) != dflt.get(sub):
                    out.append(f"  {key}.{sub}: {show(sub, cur.get(sub))}")
        elif cur != dflt:
            out.append(f"  {key}: {show(key, cur)}")
    return out


def _problems(days: int) -> str:
    """Warnings and errors since ``days`` ago, repeats grouped (digits ignored), newest first."""
    from . import filecrypto
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    groups: dict[tuple, dict] = {}

    def add(ts: str, level: str, logger: str, msg: str) -> None:
        if ts < cutoff:
            return
        key = (level, logger, re.sub(r"\d+", "#", msg)[:200])
        g = groups.setdefault(key, {"count": 0, "first": ts, "last": ts, "msg": msg})
        g["count"] += 1
        g["first"], g["last"] = min(g["first"], ts), max(g["last"], ts)
        if ts >= g["last"]:
            g["msg"] = msg

    for path in [LOG_FILE] + [f"{LOG_FILE}.{i}" for i in range(1, 20)]:
        if not os.path.exists(path):
            continue
        for line in filecrypto.read_lines(path):
            if m := _PROBLEM_LINE.match(line):
                add(*m.groups())
    names = sorted(n for n in os.listdir(INDEX_LOG_DIR) if n.endswith(".log")) if os.path.isdir(INDEX_LOG_DIR) else []
    for name in names:
        logger = "index " + name.removesuffix(".log").removeprefix("index_worker_")
        base = os.path.join(INDEX_LOG_DIR, name)
        for path in [f"{base}.{i}" for i in range(10, 0, -1)] + [base]:   # oldest first
            if not os.path.exists(path):
                continue
            day = None
            for line in filecrypto.read_lines(path):
                if h := _RUN_HEADER.match(line):
                    day = h.group(1)
                elif day and (m := _INDEX_PROBLEM.match(line)):
                    add(f"{day} {m.group(1)}", m.group(2), logger, m.group(3))
    head = f"Warnings and errors since {cutoff} ({days} day{'' if days == 1 else 's'})"
    if not groups:
        return head + "\nNone.\n"
    rows = sorted(groups.items(), key=lambda kv: kv[1]["last"], reverse=True)[:PROBLEM_GROUPS]
    body = [f"{g['count']}x {level} last {g['last']} first {g['first']} {logger} | {g['msg'][:500]}"
            for (level, logger, _k), g in rows]
    return "\n".join([head, f"{sum(g['count'] for g in groups.values())} lines, {len(groups)} kinds", ""] + body) + "\n"


def _jobs() -> list[dict]:
    from .jobs import read_job_history
    keep = ("kind", "repo", "state", "dry", "rc", "elapsed", "total", "done", "errors", "error")
    out = []
    for j in read_job_history(JOB_ENTRIES):
        row = {k: j.get(k) for k in keep}
        row["finished"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(j["finished"])) if j.get("finished") else None
        if isinstance(row.get("error"), str):
            row["error"] = row["error"][-300:]
        out.append(row)
    return out


def _kopia_status() -> str:
    from . import repos as repos_mod
    lines = []
    for rid, rec in list(REPOS.items()):
        if rec.get("kind", "kopia") != "kopia" or rec.get("demo") or repos_mod.via_server(rid):
            continue
        st = repos_mod.read_status(rid, timeout=STATUS_TIMEOUT)
        if not st:
            lines.append(f"Repository {rid}: kopia could not read its status")
            continue
        cf, of = st.get("contentFormat") or {}, st.get("objectFormat") or {}
        ret = st.get("blobRetention") or {}
        lines.append(f"Repository {rid}: format {cf.get('version')}, index {cf.get('indexVersion')}, hash {cf.get('hash')},"
                     f" encryption {cf.get('encryption')}, ecc {cf.get('ecc') or 'none'}, splitter {of.get('splitter')},"
                     f" max pack {(cf.get('maxPackSize') or 0) // 1024 ** 2} MB,"
                     f" epochs {'on' if (cf.get('epochParameters') or {}).get('Enabled') else 'off'},"
                     f" retention {ret.get('mode') or 'none'}")
    return "\n".join(lines) + "\n"


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


def build(sections, anonymise: bool = True, bundle_id: str | None = None, days: int = 7,
          browser: str = "") -> dict:
    """{"id", "files": {name: text}} for the chosen sections; the summary is always first."""
    from . import checkin
    bundle_id = bundle_id if bundle_id and re.fullmatch(r"[0-9a-f]{8}", bundle_id) else secrets.token_hex(4)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
    head = [f"KopiaLens diagnostics bundle {bundle_id}, created {stamp}",
            f"Anonymised: {'yes' if anonymise else 'no'}", ""]
    system = _system_lines() + ([f"Browser: {browser[:300]}"] if browser else [])
    anon = _Anon(checkin.install_id()) if anonymise else None
    changed = _changed_settings(anon)
    summary = "\n".join(head + _licence_lines() + [""] + system + [""] + _repo_lines() + [""]
                        + [f"Settings changed from defaults: {len(changed) or 'none'}"] + changed) + "\n"
    files = {"summary.txt": summary}
    chosen = [s for s in SECTIONS if s in set(sections or ())]
    if "problems" in chosen:
        files["problems.txt"] = _problems(days)
    if "app_log" in chosen:
        files["app-log.txt"] = _tail(LOG_FILE, LOG_LINES)
    if "index_logs" in chosen and os.path.isdir(INDEX_LOG_DIR):
        for name in sorted(os.listdir(INDEX_LOG_DIR)):
            if name.endswith(".log"):
                files[f"index-{name.removesuffix('.log').removeprefix('index_worker_')}.txt"] = _tail(os.path.join(INDEX_LOG_DIR, name), INDEX_LOG_LINES)
    if "kopia_status" in chosen:
        files["kopia-status.txt"] = _kopia_status()
    if anon:
        files = {name: anon(text) for name, text in files.items()}
    # JSON is anonymised before it is written: as text its backslashes are escaped, which splits
    # Windows paths for the text rules.
    keep = anon.walk if anon else (lambda o: o)
    if "history" in chosen:
        from .audit import read_tail as audit_tail
        files["history.jsonl"] = "\n".join(json.dumps(keep(e)) for e in audit_tail(HISTORY_ENTRIES))
        files["jobs.jsonl"] = "\n".join(json.dumps(keep(j)) for j in _jobs())
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
