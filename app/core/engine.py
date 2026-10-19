"""KopiaLens's backup engine: one kopia server per repository KopiaLens runs backups for.

The server is kopia's own scheduler (scheduled snapshots, retry after a failure, the parallel limit,
maintenance as the owner, notifications), started the way KopiaUI starts it. Analysis and the
guarded deletes stay on the direct command line; after one of those writes the server is told to
refresh so it sees the change.

A server runs only when the user chose KopiaLens for the repository, backups are not paused, and
no other engine runs on it (core/coexist.py). If another engine appears, KopiaLens stops its own
server and raises an alert; it never fights. Finished tasks are copied into KopiaLens's own task
history (kopia keeps them in memory only) and kept for `task_history_days`.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections import deque

import httpx

import config
from config import DATA_DIR

from . import filecrypto
from .applog import LOG_DIR, get_logger
from .coexist import MOVED_SUFFIX

log = get_logger("engine")

STATE_FILE = os.path.join(DATA_DIR, "engine_state.json")
HISTORY_FILE = os.path.join(DATA_DIR, "engine_tasks.jsonl")
ENGINE_LOG_DIR = os.path.join(LOG_DIR, "engine")
RECONCILE_EVERY = 20.0
POLL_EVERY = 5.0
CONNECT_TIMEOUT = 900.0      # opening a very large repository can take minutes
STOP_GRACE = 15.0
LOG_ROTATE_BYTES = 5_000_000
OVERDUE_GRACE = 15 * 60
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_lock = threading.RLock()
_servers: dict[str, "_Server"] = {}
_fail: dict[str, dict] = {}          # repo -> {count, next_try, error, alerted}
_states: dict[str, str] = {}         # repo -> last reconciled state (for transition alerts)
_done_seq: dict[str, int] = {}       # repo -> finished backup/maintenance tasks (screens reload on change)
_alerts: deque = deque(maxlen=200)
_alert_seq = 0
_stop = threading.Event()
_threads: list[threading.Thread] = []
_hist_lock = threading.Lock()
_last_prune = 0.0
_hint_cache: dict[str, tuple[float, list[dict]]] = {}


class _Server:
    def __init__(self, repo: str, proc: subprocess.Popen, port: int, ui_pw: str, ctl_pw: str,
                 log_path: str) -> None:
        self.repo, self.proc, self.port = repo, proc, port
        self.ui = httpx.Client(base_url=f"http://127.0.0.1:{port}", auth=("kopia", ui_pw),
                               timeout=httpx.Timeout(30.0, connect=5.0))
        self.ctl = httpx.Client(base_url=f"http://127.0.0.1:{port}", auth=("server-control", ctl_pw),
                                timeout=httpx.Timeout(30.0, connect=5.0))
        self.started = time.time()
        self.tag = str(int(self.started))   # kopia numbers tasks from 1 on every start
        self.connected_at: float | None = None
        self.stopping = False
        self.sources: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.recorded: set[str] = set()
        self.log_seen: dict[str, list] = {}      # task id -> kopia's log window as last read
        self.log_lock = threading.Lock()
        self.log_path = log_path
        self.last_err = ""

    def close(self) -> None:
        for c in (self.ui, self.ctl):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


# ---- state file: pause -----------------------------------------------------------------------

def _read_state() -> dict:
    data = filecrypto.read_json(STATE_FILE, default={}) or {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict) -> None:
    filecrypto.write_json(STATE_FILE, data)


def _active(p: dict | None) -> dict:
    if not p or not p.get("on"):
        return {"on": False, "until": None}
    until = p.get("until")
    if until and until <= time.time():
        return {"on": False, "until": None}
    return {"on": True, "until": until}


def pause_state(repo: str | None = None) -> dict:
    """The pause that applies: all backups, or this repository's. ``until`` None = until resumed."""
    st = _read_state()
    whole = _active(st.get("pause"))
    if whole["on"] or repo is None:
        return {**whole, "scope": "all"}
    one = _active((st.get("repos") or {}).get(str(repo)))
    return {**one, "scope": "repo"}


def set_pause(repo: str | None, hours: float) -> dict:
    """Pause backups (all, or one repository). ``hours`` 0 = until resumed. Survives a restart."""
    until = time.time() + hours * 3600 if hours and hours > 0 else None
    st = _read_state()
    entry = {"on": True, "until": until, "since": time.time()}
    if repo is None:
        st["pause"] = entry
    else:
        st.setdefault("repos", {})[str(repo)] = entry
    _write_state(st)
    _audit("engine-pause", repo, {"hours": hours})
    wake()
    return pause_state(repo)


def resume(repo: str | None) -> dict:
    st = _read_state()
    if repo is None:
        st["pause"] = {"on": False}
    else:
        st.setdefault("repos", {})[str(repo)] = {"on": False}
    _write_state(st)
    _audit("engine-resume", repo, {})
    wake()
    return pause_state(repo)


def source_pauses(repo: str) -> dict[str, dict]:
    """Folders whose scheduled backups are paused: source -> {on, until, since}."""
    raw = ((_read_state().get("sources") or {}).get(str(repo)) or {})
    out = {}
    for src, p in raw.items():
        a = _active(p)
        if a["on"]:
            out[src] = {**a, "since": p.get("since")}
    return out


def set_source_pause(repo: str, source: str, hours: float) -> dict:
    """Pause one folder's scheduled backups. kopia forgets a pause when its server restarts, so the
    pause lives here and is re-applied on every poll. ``hours`` 0 = until resumed."""
    st = _read_state()
    until = time.time() + hours * 3600 if hours and hours > 0 else None
    st.setdefault("sources", {}).setdefault(str(repo), {})[source] = {"on": True, "until": until, "since": time.time()}
    _write_state(st)
    _audit("engine-pause-source", repo, {"source": source, "hours": hours})
    _apply_source_pauses(repo)
    return source_pauses(repo).get(source) or {"on": False, "until": None}


def resume_source(repo: str, source: str) -> dict:
    st = _read_state()
    (st.setdefault("sources", {}).setdefault(str(repo), {})).pop(source, None)
    _write_state(st)
    _audit("engine-resume-source", repo, {"source": source})
    _apply_source_pauses(repo)
    return {"on": False, "until": None}


def _apply_source_pauses(repo: str) -> None:
    """Make KopiaLens's server match the saved folder pauses."""
    srv = running(repo)
    if srv is None:
        return
    want_paused = source_pauses(repo)
    for s in srv.sources:
        src = s.get("source") or {}
        key = f"{src.get('userName')}@{src.get('host')}:{src.get('path')}"
        is_paused = (s.get("status") or "") == "PAUSED"
        if (key in want_paused) == is_paused:
            continue
        try:
            srv.ctl.post("/api/v1/control/" + ("pause-source" if key in want_paused else "resume-source"),
                         params=_source_params(key), json={})
        except Exception as exc:  # noqa: BLE001 - the next poll tries again
            log.debug("engine folder pause repo=%s %s: %s", repo, key, exc)


def _take_back_waits() -> bool:
    from ..settings import load_settings
    return (load_settings().get("kopialens") or {}).get("engine_takeback", "auto") == "wait"


# ---- alerts ----------------------------------------------------------------------------------

def alert(repo: str | None, level: str, title: str, body: str = "") -> None:
    """One alert for the tray toasts and the in-app list. level: info | warn | error."""
    global _alert_seq
    with _lock:
        _alert_seq += 1
        _alerts.append({"id": _alert_seq, "ts": time.time(), "repo": repo, "level": level,
                        "title": title, "body": body})
    (log.warning if level != "info" else log.info)("alert repo=%s %s: %s %s", repo, level, title, body)
    from . import notify
    notify.send_later(title, body, level)


def alerts(after: int = 0) -> list[dict]:
    with _lock:
        return [a for a in _alerts if a["id"] > after]


def _repo_label(repo: str) -> str:
    rec = config.repo_record(repo)
    return rec.get("short_name") or rec.get("name") or f"Repo {repo}"


def _audit(action: str, repo: str | None, summary: dict) -> None:
    try:
        from . import audit
        audit.audit("executed", action, repo=repo or "", summary=summary)
    except Exception:  # noqa: BLE001 - the audit line is best-effort
        pass


# ---- server lifecycle ------------------------------------------------------------------------

def server_pids() -> set[int]:
    with _lock:
        return {s.proc.pid for s in _servers.values() if s.proc.poll() is None}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _log_line(srv: _Server, text: str) -> None:
    try:
        if os.path.isfile(srv.log_path) and os.path.getsize(srv.log_path) > LOG_ROTATE_BYTES:
            os.replace(srv.log_path, srv.log_path + ".1")
        filecrypto.append_line(srv.log_path, time.strftime("%Y-%m-%d %H:%M:%S ") + text)
    except Exception:  # noqa: BLE001 - logging must never break the reader
        pass


def _reader(srv: _Server) -> None:
    """Drain the server's output: keep the log, turn kopia's notification lines into alerts."""
    try:
        for line in srv.proc.stderr:
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith("NOTIFICATION: "):
                try:
                    note = json.loads(line[len("NOTIFICATION: "):])
                    sev = int(note.get("severity") or 0)
                    alert(srv.repo, "error" if sev >= 20 else "warn" if sev >= 10 else "info",
                          str(note.get("subject") or "Kopia notification")[:200])
                except Exception:  # noqa: BLE001 - a malformed line is logged below
                    pass
            srv.last_err = line[-300:]
            _log_line(srv, line)
    except Exception:  # noqa: BLE001 - the pipe closed underneath us
        pass


def _start(repo: str) -> _Server | None:
    from . import kopiabin
    from .kopia import _kopia_exe, _repo_args
    port = _free_port()
    ui_pw, ctl_pw = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    cmd = [_kopia_exe()] + _repo_args(repo) + [
        "server", "start", "--insecure", "--disable-csrf-token-checks", "--no-grpc",
        "--async-repo-connect", f"--address=127.0.0.1:{port}", "--shutdown-on-stdin",
        "--kopiaui-notifications", "--error-notifications=always",
    ]
    env = dict(os.environ)
    env.update({"KOPIA_SERVER_PASSWORD": ui_pw, "KOPIA_SERVER_CONTROL_PASSWORD": ctl_pw,
                "KOPIA_CHECK_FOR_UPDATES": "false"})
    os.makedirs(ENGINE_LOG_DIR, exist_ok=True)
    log_path = os.path.join(ENGINE_LOG_DIR, f"engine_repo{repo}.log")
    if not kopiabin.resolve():
        raise RuntimeError("kopia was not found")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            env=env, creationflags=_NO_WINDOW)
    srv = _Server(repo, proc, port, ui_pw, ctl_pw, log_path)
    _log_line(srv, f"=== KopiaLens started its kopia server (pid {proc.pid}, port {port}) ===")
    threading.Thread(target=_reader, args=(srv,), name=f"engine-out-{repo}", daemon=True).start()
    log.warning("ENGINE START repo=%s pid=%s port=%s", repo, proc.pid, port)
    _audit("engine-start", repo, {"pid": proc.pid})
    return srv


def _stop_server(srv: _Server, reason: str) -> None:
    srv.stopping = True
    try:
        if srv.proc.stdin:
            srv.proc.stdin.close()      # --shutdown-on-stdin: kopia cancels uploads and exits cleanly
    except Exception:  # noqa: BLE001
        pass
    try:
        srv.proc.wait(timeout=STOP_GRACE)
    except Exception:  # noqa: BLE001
        try:
            srv.proc.terminate()
            srv.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                srv.proc.kill()
            except Exception:  # noqa: BLE001
                pass
    srv.close()
    _log_line(srv, f"=== KopiaLens stopped its kopia server ({reason}) ===")
    log.warning("ENGINE STOP repo=%s reason=%s", srv.repo, reason)
    _audit("engine-stop", srv.repo, {"reason": reason})


def want(repo: str) -> tuple[bool, str]:
    """Whether KopiaLens's server should run for ``repo`` now, and the state name when not."""
    rec = config.repo_record(repo)
    if rec.get("engine") != "kopialens":
        return False, "off"
    if config.repo_kind(repo) != "kopia" or config.is_demo(repo):
        return False, "off"
    if pause_state(repo)["on"]:
        return False, "paused"
    from . import coexist, writemode
    if not writemode.backups_allowed(repo):
        return False, "write_mode"
    if coexist.repo_view(repo)["conflict"]:
        return False, "blocked"
    f = _fail.get(repo)
    if f and f.get("next_try", 0) > time.time():
        return False, "failed"
    return True, "running"


def reconcile() -> None:
    """Start, stop and restart servers so reality matches `want`. Raises alerts on transitions."""
    from config import REPOS
    for repo in list(REPOS):
        try:
            ok, state = want(repo)
        except Exception as exc:  # noqa: BLE001 - one repo's problem must not stop the others
            log.error("engine reconcile repo=%s: %s", repo, exc)
            continue
        with _lock:
            srv = _servers.get(repo)
        prev = _states.get(repo)
        if state == "blocked" and prev in ("running", "starting"):
            from . import coexist
            other = next((r["label"] for r in coexist.repo_view(repo)["running"] if r["app"] != "kopialens"),
                         "Another app")
            waits = _take_back_waits()
            if waits:
                st = _read_state()
                st.setdefault("repos", {})[str(repo)] = {"on": True, "until": None, "since": time.time(),
                                                         "reason": "other-app"}
                _write_state(st)
            alert(repo, "warn", f"{other} started running backups for {_repo_label(repo)}",
                  "KopiaLens stopped its own backup schedule there."
                  + (" Resume it when you're ready." if waits else ""))
        if ok and srv is None:
            try:
                srv = _start(repo)
                with _lock:
                    _servers[repo] = srv
            except Exception as exc:  # noqa: BLE001
                _note_failure(repo, str(exc))
                continue
        elif not ok and srv is not None:
            _stop_server(srv, state)
            with _lock:
                _servers.pop(repo, None)
        _states[repo] = state if not ok else ("running" if srv and srv.connected_at else "starting")
    with _lock:
        stray = [r for r in _servers if r not in REPOS]
    for repo in stray:                       # a repository removed from KopiaLens
        with _lock:
            srv = _servers.pop(repo, None)
        if srv:
            _stop_server(srv, "removed")


def _note_failure(repo: str, error: str) -> None:
    f = _fail.setdefault(repo, {"count": 0})
    f["count"] += 1
    f["error"] = error[-300:]
    f["next_try"] = time.time() + min(60 * 2 ** (f["count"] - 1), 1800)
    if f["count"] in (1, 3) or f["count"] % 10 == 0:
        alert(repo, "error", f"Backups for {_repo_label(repo)} stopped", error[-200:])
    log.error("engine repo=%s failed (%d): %s", repo, f["count"], error)


# ---- polling ---------------------------------------------------------------------------------

def _get(client: httpx.Client, path: str):
    r = client.get(path)
    r.raise_for_status()
    return r.json()


def _poll_one(srv: _Server) -> None:
    if srv.proc.poll() is not None:
        if not srv.stopping:
            with _lock:
                _servers.pop(srv.repo, None)
            srv.close()
            _note_failure(srv.repo, srv.last_err or f"kopia server exited ({srv.proc.returncode})")
        return
    try:
        st = _get(srv.ctl, "/api/v1/control/status")
    except Exception:  # noqa: BLE001 - still starting up
        if time.time() - srv.started > CONNECT_TIMEOUT:
            _stop_server(srv, "did not open the repository")
            with _lock:
                _servers.pop(srv.repo, None)
            _note_failure(srv.repo, "KopiaLens's kopia server didn't open the repository in time")
        return
    if not st.get("connected"):
        # kopia retries a failed open forever ("unable to open repository ... will keep trying"),
        # e.g. when the password isn't saved: stop and say why instead of "starting" for ever.
        retrying = "unable to open repository" in srv.last_err.lower()
        if retrying or time.time() - srv.started > CONNECT_TIMEOUT:
            reason = srv.last_err if retrying else "KopiaLens's kopia server didn't open the repository in time"
            _stop_server(srv, "could not open the repository")
            with _lock:
                _servers.pop(srv.repo, None)
            _note_failure(srv.repo, reason)
        return
    if srv.connected_at is None:
        srv.connected_at = time.time()
        _states[srv.repo] = "running"
    if time.time() - srv.connected_at > 600:
        _fail.pop(srv.repo, None)
    try:
        srv.sources = (_get(srv.ctl, "/api/v1/control/sources") or {}).get("sources") or []
        _apply_source_pauses(srv.repo)
    except Exception:  # noqa: BLE001
        pass
    try:
        tasks = (_get(srv.ui, "/api/v1/tasks") or {}).get("tasks") or []
    except Exception:  # noqa: BLE001
        return
    changed = False
    for t in tasks:
        srv.tasks[t["id"]] = t
        if t["id"] in srv.recorded:
            continue
        if not t.get("endTime"):
            collect_task_log(srv, t["id"])
        else:
            srv.recorded.add(t["id"])
            _record(srv, t)
            changed = changed or t.get("kind") != "Repository"
    if changed:
        # A backup or maintenance changed kopia's snapshot list: rebuild it here, off any page
        # load, before the screens are told to reload.
        from .sources import rebuild
        rebuild(srv.repo)
        _done_seq[srv.repo] = _done_seq.get(srv.repo, 0) + 1


def _poll_loop() -> None:
    while not _stop.wait(POLL_EVERY):
        with _lock:
            servers = list(_servers.values())
        for srv in servers:
            try:
                _poll_one(srv)
            except Exception as exc:  # noqa: BLE001
                log.debug("engine poll repo=%s: %s", srv.repo, exc)


_wake = threading.Event()


def wake() -> None:
    """Reconcile soon (after a choice, pause or resume) instead of on the next tick."""
    _wake.set()


def restart(repo: str, reason: str) -> bool:
    """Stop KopiaLens's server for ``repo`` so the next reconcile starts it fresh (it re-reads its
    connection file). False when none was running."""
    with _lock:
        srv = _servers.pop(repo, None)
    if srv is None:
        return False
    _stop_server(srv, reason)
    wake()
    return True


def _reconcile_loop() -> None:
    while not _stop.is_set():
        try:
            reconcile()
            _prune_history()
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            log.error("engine reconcile failed: %s", exc)
        _wake.wait(RECONCILE_EVERY)
        _wake.clear()


def start() -> None:
    """Start the engine threads (app startup)."""
    if config.DEMO_INSTANCE:
        return
    _stop.clear()
    for target, name in ((_reconcile_loop, "engine-reconcile"), (_poll_loop, "engine-poll")):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        _threads.append(t)


def shutdown() -> None:
    """App exit: stop every server KopiaLens started (KopiaLens off = no scheduled backups)."""
    _stop.set()
    _wake.set()
    with _lock:
        servers = list(_servers.values())
        _servers.clear()
    for srv in servers:
        _stop_server(srv, "KopiaLens is closing")


# ---- actions on a running server -------------------------------------------------------------

def running(repo: str) -> _Server | None:
    with _lock:
        srv = _servers.get(repo)
    return srv if srv and srv.connected_at and srv.proc.poll() is None else None


def refresh(repo: str) -> None:
    """After a KopiaLens write on the command line: make the server re-read sources and policies."""
    srv = running(repo)
    if srv is None:
        return
    try:
        srv.ctl.post("/api/v1/control/refresh", json={})
    except Exception as exc:  # noqa: BLE001 - the server refreshes on its own interval anyway
        log.debug("engine refresh repo=%s: %s", repo, exc)


def _source_params(source: str) -> dict:
    userhost, _, path = source.partition(":")
    user, _, host = userhost.partition("@")
    return {"userName": user, "host": host, "path": path}


def trigger_snapshot(repo: str, source: str) -> bool:
    srv = running(repo)
    if srv is None:
        return False
    r = srv.ctl.post("/api/v1/control/trigger-snapshot", params=_source_params(source), json={})
    r.raise_for_status()
    return True


def cancel_snapshot(repo: str, source: str) -> bool:
    srv = running(repo)
    if srv is None:
        return False
    r = srv.ctl.post("/api/v1/control/cancel-snapshot", params=_source_params(source), json={})
    r.raise_for_status()
    return True


def cancel_task(repo: str, task_id: str) -> bool:
    srv = running(repo)
    if srv is None:
        return False
    r = srv.ui.post(f"/api/v1/tasks/{task_id}/cancel", json={})
    r.raise_for_status()
    return True


def edit_description(repo: str, snapshot_id: str, description: str, password: str | None = None) -> bool:
    """Set one snapshot's description. Only kopia's server can do this; the snapshot gets a new id.
    Without KopiaLens's own server, a temporary one does it (core/descedit; may raise PasswordNeeded)."""
    srv = running(repo)
    if srv is None:
        from . import descedit
        descedit.edit(repo, snapshot_id, description, password)
        return True
    r = srv.ui.post("/api/v1/snapshots/edit", json={"snapshots": [snapshot_id], "description": description})
    r.raise_for_status()
    return True


def task_log_path(repo: str, tag: str, task_id: str) -> str:
    return os.path.join(ENGINE_LOG_DIR, f"task_repo{repo}_{tag}_{task_id}.log")


def _new_log_entries(seen: list, now: list) -> tuple[list, bool]:
    """The entries of kopia's log window ``now`` that weren't in ``seen`` (the window read last
    time). kopia keeps only a task's newest 1000 entries, so the window slides; ``gap`` is True
    when nothing of ``seen`` is left in it (entries were dropped between two reads)."""
    if not seen:
        return now, False
    last = seen[-1]
    for j in range(len(now) - 1, -1, -1):
        if now[j] == last and now[: j + 1] == seen[len(seen) - j - 1:]:
            return now[j + 1:], False
    return now, True


def collect_task_log(srv: _Server, task_id: str) -> str:
    """Append what kopia logged for a task since the last read to its log file and return the
    file's path. Reading while the task runs keeps every line: kopia itself keeps only the newest
    1000 per task."""
    path = task_log_path(srv.repo, srv.tag, task_id)
    with srv.log_lock:
        try:
            raw = (_get(srv.ui, f"/api/v1/tasks/{task_id}/logs") or {}).get("logs") or []
        except Exception:  # noqa: BLE001 - the next poll reads again
            return path
        new, gap = _new_log_entries(srv.log_seen.get(task_id) or [], raw)
        srv.log_seen[task_id] = raw
        if gap:
            filecrypto.append_line(path, "… kopia dropped some log lines before KopiaLens could read them")
        for e in new:
            filecrypto.append_line(path, _log_text(e))
    return path


def task_logs(repo: str, task_id: str) -> list[str]:
    """A live task's log so far (every line read since KopiaLens's server started)."""
    srv = running(repo)
    if srv is None:
        return []
    return filecrypto.read_lines(collect_task_log(srv, task_id))


_LOG_LEVELS = {0: "", 1: "", 2: "warning", 3: "error"}   # kopia's task log levels (KopiaUI's loglevel-N); only problems are named


def _log_text(e) -> str:
    """One kopia log entry as text: time, level, message, then a tab and the entry's details
    (key=value), which the task panel shows muted like KopiaUI."""
    if not isinstance(e, dict):
        return str(e).replace("\n", " ")
    ts = e.get("ts")
    when = time.strftime("%H:%M:%S", time.localtime(float(ts))) if isinstance(ts, (int, float)) else "--:--:--"
    extra = " ".join(f"{k}={json.dumps(v) if isinstance(v, (dict, list)) else v}"
                     for k, v in e.items() if k not in ("ts", "level", "msg", "mod"))
    level = _LOG_LEVELS.get(e.get("level"), str(e.get("level") or ""))
    text = f"{when} {level + ' ' if level and level != 'info' else ''}{e.get('msg', '')}{chr(9) + extra if extra else ''}"
    return text.replace("\n", " ")


def source_status(repo: str) -> list[dict]:
    """The server's live view of each source (status, next snapshot, upload progress)."""
    srv = running(repo)
    if srv is None:
        return []
    paused = source_pauses(repo)
    out = []
    for s in srv.sources:
        src = s.get("source") or {}
        up = s.get("upload") or {}
        out.append({
            "source": f"{src.get('userName')}@{src.get('host')}:{src.get('path')}",
            "status": s.get("status") or "", "next": s.get("nextSnapshotTime"),
            "task": s.get("currentTask") or "",
            "paused": paused.get(f"{src.get('userName')}@{src.get('host')}:{src.get('path')}"),
            "progress": {k: up.get(k) for k in ("hashedBytes", "cachedBytes", "estimatedBytes",
                                                "hashedFiles", "cachedFiles", "directory")} if up else None,
        })
    return out


# ---- task history ----------------------------------------------------------------------------

def _ts(text) -> float | None:
    if not text:
        return None
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def task_record(repo: str, t: dict, tag: str) -> dict:
    """One kopia server task in the Tasks tab's shape. ``tag`` names the server run, because kopia
    restarts its task numbers with every start."""
    desc = t.get("description") or ""
    source = desc.rsplit(" at ", 1)[0] if t.get("kind") == "Snapshot" else ""
    return {
        "id": f"engine:{repo}:{tag}.{t.get('id')}", "repo": repo, "origin": "engine",
        "kind": t.get("kind") or "", "description": desc, "source": source,
        "status": (t.get("status") or "").lower(), "start": _ts(t.get("startTime")),
        "end": _ts(t.get("endTime")), "error": t.get("errorMessage") or "",
        "progress": t.get("progressInfo") or "", "counters": t.get("counters") or {},
    }


def _record(srv: _Server, t: dict) -> None:
    if t.get("kind") == "Repository" and (t.get("status") or "").upper() == "SUCCESS":
        return                      # the server opening the repository: noise on every start
    rec = task_record(srv.repo, t, srv.tag)
    if rec["id"] in _HIDDEN:
        return
    log_path = collect_task_log(srv, t["id"])     # the last lines; earlier ones were appended as it ran
    srv.log_seen.pop(t["id"], None)
    rec["log_path"] = log_path if os.path.isfile(log_path) else ""
    with _hist_lock:
        filecrypto.append_line(HISTORY_FILE, json.dumps(rec, ensure_ascii=False))
    if rec["kind"] == "Snapshot" and rec["status"] == "failed":
        alert(srv.repo, "error", f"Backup failed: {rec['source'].split(':', 1)[-1]}", rec["error"][:200])
    elif rec["kind"] == "Maintenance" and rec["status"] == "failed":
        alert(srv.repo, "error", f"Maintenance failed on {_repo_label(srv.repo)}", rec["error"][:200])


def history(limit: int = 2000) -> list[dict]:
    with _hist_lock:
        rows = filecrypto.read_jsonl(HISTORY_FILE, limit=limit) or []
    return rows


def history_days() -> int:
    from ..settings import load_settings
    try:
        return int((load_settings().get("kopialens") or {}).get("task_history_days", 90))
    except (TypeError, ValueError):
        return 90


def _kl_number(key: str, default: float) -> float:
    from ..settings import load_settings
    try:
        return float((load_settings().get("kopialens") or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def routine_seconds() -> float:
    return _kl_number("task_routine_seconds", 2)


def is_routine(rec: dict) -> bool:
    """A task that finished without errors in under ``task_routine_seconds``: a scheduled backup
    that found nothing to do. The Tasks tab hides these by default and keeps them a short time."""
    if rec.get("status") != "success" or rec.get("error"):
        return False
    if any((c or {}).get("value") for name, c in (rec.get("counters") or {}).items() if "error" in name.lower()):
        return False
    start, end = rec.get("start"), rec.get("end")
    return bool(start and end) and end - start < routine_seconds()


def _prune_history() -> None:
    """Drop task records (and their log files) older than `task_history_days`, and routine ones
    older than `task_routine_hours`, once a day."""
    global _last_prune
    if time.time() - _last_prune < 86400:
        return
    _last_prune = time.time()
    days = history_days()
    routine_cut = time.time() - _kl_number("task_routine_hours", 48) * 3600
    cutoff = time.time() - days * 86400 if days > 0 else 0
    with _hist_lock:
        lines = filecrypto.read_lines(HISTORY_FILE)
        keep, gone = [], []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            at = rec.get("end") or rec.get("start") or 0
            old = at < cutoff or (at < routine_cut and is_routine(rec))
            (gone if old else keep).append((line, rec))
        if not gone:
            return
        filecrypto.write_lines(HISTORY_FILE, [ln for ln, _ in keep])
    for _, rec in gone:
        path = rec.get("log_path") or ""
        if path.startswith(ENGINE_LOG_DIR) and os.path.isfile(path):
            try:
                os.remove(path)        # KopiaLens's own copy of a kopia task log
            except OSError:
                pass


_HIDDEN: set[str] = set()


def hide_task(repo: str, tag: str, task_id: str) -> None:
    """A server task KopiaLens started for one of its own jobs: the job is what Tasks lists."""
    _HIDDEN.add(f"engine:{repo}:{tag}.{task_id}")


def live_tasks(repo: str | None = None) -> list[dict]:
    with _lock:
        servers = [s for r, s in _servers.items() if repo in (None, r)]
    out = []
    for srv in servers:
        for t in list(srv.tasks.values()):
            if t.get("kind") == "Repository" and (t.get("status") or "").upper() == "SUCCESS":
                continue
            rec = task_record(srv.repo, t, srv.tag)
            if rec["id"] not in _HIDDEN:
                out.append(rec)
    return out


# ---- who runs backups ------------------------------------------------------------------------



def set_choice(repo: str, choice: str, how: str = "share", password: str = "") -> dict:
    """KopiaLens runs this repository's backups ("kopialens"), or another app does ("external").

    ``how="move"``: KopiaLens makes its own connection from KopiaUI's config file (the password
    once: kopia files a saved password under the config's path) and renames KopiaUI's file so
    KopiaUI stops loading it at its next start. Handing back renames it back. ``how="share"``
    keeps the one file; then KopiaUI must not run, which detection enforces."""
    from . import coexist, kopiabin, repoconnect, repos
    if choice not in ("kopialens", "external"):
        raise ValueError("unknown choice")
    if config.repo_kind(repo) != "kopia" or config.is_demo(repo):
        raise ValueError("KopiaLens can't run backups for this repository.")
    rec = config.repo_record(repo)
    if choice == "external":
        fields = {"engine": "", "engine_since": 0}
        moved = rec.get("engine_moved_from") or ""
        if moved and os.path.isfile(moved + MOVED_SUFFIX) and not os.path.exists(moved):
            try:
                os.replace(moved + MOVED_SUFFIX, moved)
            except OSError as exc:
                raise ValueError(f"Couldn't put KopiaUI's connection file back: {exc}") from exc
            fields.update(config_file=moved, engine_moved_from="")
        repos.set_engine_fields(repo, fields)
        _fail.pop(repo, None)
        _audit("engine-choice", repo, {"choice": "external", "restored": bool(moved)})
        wake()
        return repo_status(repo)
    view = coexist.repo_view(repo)
    if how == "move":
        src = view["config"]
        if not view["kopiaui_config"] or not view["kopiaui_shares_file"]:
            raise ValueError("This repository doesn't use KopiaUI's connection file.")
        if not (password or "").strip():
            raise ValueError("Enter the repository password.")
        dst = os.path.join(config.DATA_DIR, "configs", f"repo{repo}.config")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        res = repoconnect._run([kopiabin.exe(), "--config-file", dst, "repository", "connect",
                                "from-config", "--file", src, "--no-check-for-updates"],
                               password, timeout=300.0)
        if res["returncode"] != 0:
            raise ValueError(repoconnect._last_meaningful_line(res.get("stderr") or "")
                             or "kopia couldn't open the repository with that password.")
        try:
            os.replace(src, src + MOVED_SUFFIX)
        except OSError as exc:
            raise ValueError(f"KopiaLens connected, but couldn't rename KopiaUI's file: {exc}") from exc
        repos.set_engine_fields(repo, {"config_file": dst, "engine_moved_from": src})
    elif view["conflict"]:
        raise ValueError(view["conflict"] + " Quit it first, then try again.")
    repos.set_engine_fields(repo, {"engine": "kopialens", "engine_since": time.time()})
    _fail.pop(repo, None)
    _hint_cache.pop(repo, None)
    _audit("engine-choice", repo, {"choice": "kopialens", "how": how})
    wake()
    return repo_status(repo)


# ---- status, health, hints -------------------------------------------------------------------

def repo_status(repo: str) -> dict:
    """Everything the screens need about one repository's backups."""
    from . import coexist
    ok, state = want(repo)
    srv = running(repo)
    with _lock:
        pending = _servers.get(repo)
    if ok:
        state = "running" if srv else "starting"
    view = coexist.repo_view(repo)
    nxt = min((s["next"] for s in source_status(repo) if s.get("next")), default=None)
    f = _fail.get(repo) or {}
    # KopiaUI keeps running on the file KopiaLens moved until it restarts; then KopiaLens takes over.
    moved = config.repo_record(repo).get("engine_moved_from") or ""
    others = [r for r in view["running"] if r["app"] != "kopialens"]
    norm = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731
    until_restart = state == "blocked" and bool(moved) and bool(others) and all(
        norm(r["config"]) == norm(moved) for r in others)
    return {
        "repo": repo, "choice": view["choice"], "state": state, "engine": view["engine"],
        "conflict": view["conflict"], "until_restart": until_restart, "pause": pause_state(repo), "next": nxt,
        "paused_sources": len(source_pauses(repo)),
        "done_seq": _done_seq.get(repo, 0),
        "evidence": snapshot_evidence(repo) if view["choice"] != "kopialens" else {},
        "error": f.get("error") if state == "failed" else "",
        "retry_at": f.get("next_try") if state == "failed" else None,
        "connecting": bool(pending and not pending.connected_at),
    }


def snapshot_evidence(repo: str) -> dict:
    """What the snapshot list says about backups run elsewhere: the newest snapshot, which computer
    made it, and whether a source is past its scheduled time. From memory only (no kopia)."""
    from . import schedule
    from .sources import cached_index
    index = cached_index(repo)
    if index is None:
        return {}
    newest, by = "", ""
    for key, snaps in index.items():
        if snaps:
            at = snaps[-1].get("endTime") or snaps[-1].get("startTime") or ""
            if at > newest:
                newest, by = at, key.split(":", 1)[0]
    late = time.time() - 15 * 60
    overdue = False
    for when in (schedule.load_next_snapshots(repo) or {}).values():
        try:
            overdue = overdue or datetime.datetime.fromisoformat(when).timestamp() < late
        except (TypeError, ValueError):
            continue
    return {"known": True, "last_snapshot": newest or None, "by": by, "overdue": overdue}


def health(repo: str) -> list[dict]:
    """Backup and maintenance warnings for a repository KopiaLens runs backups for."""
    from . import coexist
    out: list[dict] = []
    if config.repo_record(repo).get("engine") != "kopialens":
        return out
    now = time.time()
    hist = [h for h in history() if h.get("repo") == repo and h.get("kind") == "Snapshot"]
    last_by_source: dict[str, dict] = {}
    for h in hist:
        if h.get("source"):
            last_by_source[h["source"]] = h
    for src, h in sorted(last_by_source.items()):
        if h.get("status") == "failed":
            out.append({"level": "error", "title": f"Last backup failed: {src.split(':', 1)[-1]}",
                        "detail": h.get("error") or ""})
    for src, p in sorted(source_pauses(repo).items()):
        if p.get("since") and now - p["since"] > 7 * 86400:
            out.append({"level": "warn", "title": f"Backups paused for {int((now - p['since']) // 86400)} days: "
                        f"{src.split(':', 1)[-1]}", "detail": ""})
    for s in source_status(repo):
        nt = _ts(s.get("next"))
        if nt and now - nt > OVERDUE_GRACE and s.get("status") == "IDLE":
            out.append({"level": "warn", "title": f"Backup overdue: {s['source'].split(':', 1)[-1]}",
                        "detail": ""})
    info = coexist.maintenance_info(repo)
    if not info:
        return out
    ident = coexist.identity_of(coexist.read_config(coexist.config_path(repo)))
    owner = info.get("owner") or ""
    runs = (info.get("schedule") or {}).get("runs") or {}
    last_any = max((_ts(r.get("start")) or 0 for rs in runs.values() for r in rs), default=0)
    last_full = max((_ts(r.get("start")) or 0 for r in runs.get("snapshot-gc") or []), default=0)
    failed = [r for rs in runs.values() for r in rs[:1] if r.get("success") is False]
    quick_iv = ((info.get("quick") or {}).get("interval") or 0) / 1e9
    full_iv = ((info.get("full") or {}).get("interval") or 0) / 1e9
    if owner and owner != ident:
        if last_any and now - last_any > 7 * 86400:
            out.append({"level": "warn", "title": "Maintenance hasn't run for a week",
                        "detail": f"It runs on {owner}, not on this computer."})
        return out
    if quick_iv and last_any and now - last_any > 2 * quick_iv:
        out.append({"level": "warn", "title": "Quick maintenance is overdue", "detail": ""})
    if (info.get("full") or {}).get("enabled", True) and full_iv and last_full and \
            now - last_full > max(2 * 86400, 2 * full_iv):
        out.append({"level": "warn", "title": "Full maintenance is overdue", "detail": ""})
    if failed:
        out.append({"level": "error", "title": "The last maintenance run failed",
                    "detail": str(failed[0].get("error") or "")[:200]})
    return out


def hints(repo: str) -> list[dict]:
    """Warn-only signs of another engine: snapshots and maintenance runs KopiaLens did not start
    since it took over. Cached; they also happen when someone runs the kopia command line."""
    hit = _hint_cache.get(repo)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    from . import coexist
    from .jobs import read_job_history
    from .sources import sources_index
    out: list[dict] = []
    rec = config.repo_record(repo)
    since = float(rec.get("engine_since") or 0)
    if rec.get("engine") != "kopialens" or not since:
        return out
    week = max(since, time.time() - 7 * 86400)
    ours = [(h.get("start") or 0, h.get("end") or h.get("start") or 0) for h in history()
            if h.get("repo") == repo]
    ours += [(j.get("started") or j.get("created") or 0, j.get("finished") or 0)
             for j in read_job_history(limit=500) if j.get("repo") == repo]
    with _lock:
        srv = _servers.get(repo)
    if srv:
        ours += [(_ts(t.get("startTime")) or 0, _ts(t.get("endTime")) or time.time())
                 for t in list(srv.tasks.values())]

    def by_us(ts: float) -> bool:
        return any(a - 120 <= ts <= (b or a) + 120 for a, b in ours)

    ident = coexist.identity_of(coexist.read_config(coexist.config_path(repo)))
    try:
        idx = sources_index(repo)
    except Exception:  # noqa: BLE001
        idx = {}
    foreign = []
    for key, snaps in idx.items():
        if not key.startswith(ident + ":"):
            continue
        for s in snaps:
            t = _ts(s.get("startTime"))
            if t and t >= week and not by_us(t):
                foreign.append(t)
    if foreign:
        out.append({"level": "info", "title": f"{len(foreign)} snapshot(s) here weren't taken by KopiaLens",
                    "detail": "Latest " + time.strftime("%Y-%m-%d %H:%M", time.localtime(max(foreign)))})
    runs = (coexist.maintenance_info(repo).get("schedule") or {}).get("runs") or {}
    other_maint = [t for rs in runs.values() for r in rs
                   for t in [_ts(r.get("start"))] if t and t >= week and not by_us(t)]
    if other_maint:
        out.append({"level": "info", "title": "Maintenance ran here without KopiaLens",
                    "detail": "Latest " + time.strftime("%Y-%m-%d %H:%M", time.localtime(max(other_maint)))})
    _hint_cache[repo] = (time.time(), out)
    return out
