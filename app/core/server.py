"""Long-lived kopia server processes (one per repo) + a pooled HTTP client.

Why: spawning a fresh ``kopia`` process per call pays a ~0.4s repo-open every time,
and listing sources/snapshots via ``snapshot list --json`` re-reads the entire repo
(13k+ snapshots) on every request. A persistent server keeps the repository open, so
source/snapshot listing is fast and fresh (this is what KopiaUI does).

Scope: the server is used for *listing* (sources, snapshots) and live object reads.
The cold object-tree WALK stays on batched subprocess ``content show`` + the sqlite
dir cache - the server's per-OID object endpoint has no batch form and would be slower
there.

Security: localhost-only, plain HTTP, CSRF disabled (safe: 127.0.0.1, single user,
app-internal calls - not a browser session). Started ``--readonly``: the server never
writes the repo and - crucially - does NOT run the repos' snapshot schedules in the
background (a non-readonly server auto-snapshots like KopiaUI, which duplicated backups
and held files open). Mount/Restore are unaffected - they run as separate ``kopia``
subprocesses (kopia.py), not through this server. Trade-off: kopia reports
nextSnapshotTime=nil in read-only mode, so that column is computed app-side instead.
"""

from __future__ import annotations

import atexit
import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass

import httpx

from config import REPOS, kopia_repos

from ..settings import load_settings, save_settings
from .applog import APP_LOG_DIR, get_logger
from .kopia import _kopia_exe, _repo_args

log = get_logger("server")

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW - no console flash
BASE_PORT = int(os.environ.get("KOPIALENS_KOPIA_BASE_PORT") or 51830)  # repo "1" -> 51831, repo "2" -> 51832
USERNAME = "kopialens"
READY_TIMEOUT = 40.0


def _password() -> str:
    settings = load_settings()
    srv = settings.get("server") or {}
    pw = srv.get("password")
    if not pw:
        pw = secrets.token_urlsafe(24)
        settings["server"] = {**srv, "password": pw}
        save_settings(settings)
    return pw


def parse_source(source: str) -> tuple[str, str, str]:
    """``user@host:path`` -> (user, host, path). The path keeps its own ``:`` (e.g. C:\\)."""
    userhost, _, path = source.partition(":")
    user, _, host = userhost.partition("@")
    return user, host, path


def adapt_snapshot(user: str, host: str, path: str, s: dict) -> dict:
    """Map a kopia-server snapshot object to the CLI ``snapshot list --json`` shape the
    rest of the engine already consumes (id/source/startTime/stats/rootEntry/pins)."""
    summ = s.get("summary", {}) or {}
    return {
        "id": s.get("id", ""),
        "source": {"userName": user, "host": host, "path": path},
        "startTime": s.get("startTime", ""),
        "endTime": s.get("endTime", ""),
        "stats": {"totalSize": summ.get("size", 0)},
        "rootEntry": {
            "obj": s.get("rootID", ""),
            "summ": {"files": summ.get("files", 0), "dirs": summ.get("dirs", 0)},
        },
        "pins": s.get("pins") or None,
        "retention": s.get("retention", []) or [],
    }


@dataclass
class _Server:
    repo: str
    port: int
    proc: subprocess.Popen
    client: httpx.Client


class KopiaServers:
    """Manages one kopia server per repo, spawned lazily and restarted if it dies."""

    def __init__(self) -> None:
        self._servers: dict[str, _Server] = {}
        self._lock = threading.Lock()
        self._password = _password()

    def port(self, repo: str) -> int:
        return BASE_PORT + int(repo)

    def _spawn(self, repo: str) -> _Server:
        if repo not in REPOS:
            raise ValueError(f"unknown repo: {repo}")
        port = self.port(repo)
        args = (
            [_kopia_exe()]
            + _repo_args(repo)   # never the raw args: a non-kopia repo would launch against the default
            + [
                "server", "start",
                "--insecure", "--disable-csrf-token-checks", "--no-grpc",
                "--readonly",  # pure read helper: never writes, never runs the snapshot schedule
                f"--address=127.0.0.1:{port}",
                f"--server-username={USERNAME}",
                f"--server-password={self._password}",
                "--shutdown-on-stdin",  # dies when we close its stdin / exit
            ]
        )
        log.info("starting kopia server repo=%s on 127.0.0.1:%d", repo, port)
        os.makedirs(APP_LOG_DIR, exist_ok=True)
        from . import filecrypto
        logf = filecrypto.open_text(os.path.join(APP_LOG_DIR, f"kopia-server-repo{repo}.log"))
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
        logf.close()  # child keeps its own handle; we don't need ours
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            auth=(USERNAME, self._password),
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
        srv = _Server(repo=repo, port=port, proc=proc, client=client)
        self._wait_ready(srv)
        return srv

    def _wait_ready(self, srv: _Server) -> None:
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if srv.proc.poll() is not None:
                raise RuntimeError(f"kopia server repo={srv.repo} exited rc={srv.proc.returncode}")
            try:
                if srv.client.get("/api/v1/repo/status").status_code == 200:
                    log.info("kopia server repo=%s ready", srv.repo)
                    return
            except Exception:
                pass
            time.sleep(0.4)
        raise RuntimeError(f"kopia server repo={srv.repo} not ready after {READY_TIMEOUT}s")

    def _get(self, repo: str) -> _Server:
        with self._lock:
            srv = self._servers.get(repo)
            if srv is not None and srv.proc.poll() is None:
                return srv
            if srv is not None:
                log.warning("kopia server repo=%s died (rc=%s); restarting", repo, srv.proc.returncode)
                try:
                    srv.client.close()
                except Exception:
                    pass
            srv = self._spawn(repo)
            self._servers[repo] = srv
            return srv

    def client(self, repo: str) -> httpx.Client:
        return self._get(repo).client

    # --- API helpers -----------------------------------------------------
    def list_sources(self, repo: str) -> list[dict]:
        r = self.client(repo).get("/api/v1/sources")
        r.raise_for_status()
        return r.json().get("sources", []) or []

    def list_snapshots(self, repo: str, source: str) -> list[dict]:
        user, host, path = parse_source(source)
        r = self.client(repo).get(
            "/api/v1/snapshots",
            params={"userName": user, "host": host, "path": path, "all": "1"},
        )
        r.raise_for_status()
        raw = r.json().get("snapshots", []) or []
        return [adapt_snapshot(user, host, path, s) for s in raw]

    def get_object(self, repo: str, oid: str) -> dict:
        """Return a directory object's parsed JSON ({stream, entries}). Used for live
        browse, NOT the batched cold walk."""
        r = self.client(repo).get(f"/api/v1/objects/{oid}")
        r.raise_for_status()
        return r.json()

    def warmup(self) -> None:
        """Pre-start servers for all configured repos (called at app startup)."""
        for repo in kopia_repos():  # the persistent-server backend is kopia-only
            try:
                self._get(repo)
            except Exception as exc:  # noqa: BLE001 - log and continue; lazy spawn retries later
                log.error("failed to start kopia server repo=%s: %s", repo, exc)

    def shutdown(self) -> None:
        with self._lock:
            for srv in self._servers.values():
                try:
                    srv.client.close()
                except Exception:
                    pass
                try:
                    if srv.proc.stdin:
                        srv.proc.stdin.close()  # triggers --shutdown-on-stdin
                except Exception:
                    pass
                try:
                    srv.proc.terminate()
                    srv.proc.wait(timeout=5)
                except Exception:
                    try:
                        srv.proc.kill()
                    except Exception:
                        pass
            self._servers.clear()


SERVERS = KopiaServers()
atexit.register(SERVERS.shutdown)
