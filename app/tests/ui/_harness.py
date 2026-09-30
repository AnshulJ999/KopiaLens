"""UI-suite harness: a small known kopia repository and a throwaway KopiaLens server against it.

Everything lives under one temp root. ``APPDATA``/``LOCALAPPDATA`` point inside it, so neither
kopia nor KopiaLens sees the developer's real kopia connections, caches or keyring entries.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import os
import random
import site
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
REPO_PASSWORD = "fixture repo pw"
HOST, USER = "kltest", "tester"
USERNAME, PASSWORD = "admin", "battery staple horse"   # the KopiaLens account
SNAPSHOT_DAYS_AGO = (6, 4, 2)           # one snapshot per generation, oldest first


def isolated_env(root: Path, kopia: str) -> dict:
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("KOPIALENS_") or k.startswith("KOPIA_")]:
        del env[key]
    # Python's user site-packages live under APPDATA on Windows: keep them where they are.
    env.setdefault("PYTHONUSERBASE", site.getuserbase())
    env.update(APPDATA=str(root / "appdata"), LOCALAPPDATA=str(root / "localappdata"),
               KOPIALENS_KOPIA=kopia, KOPIA_CHECK_FOR_UPDATES="false")
    (root / "appdata").mkdir(parents=True, exist_ok=True)
    (root / "localappdata").mkdir(parents=True, exist_ok=True)
    return env


# ---------------------------------------------------------------- fixture repository

def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _generation_1(src: Path, rnd: random.Random) -> None:
    _write(src / "Documents" / "needle-report.txt", b"quarterly needle report\n" * 40)
    for i in range(10):
        _write(src / "Documents" / f"notes-{i}.txt", rnd.randbytes(2_000 + i * 100))
    photo0 = rnd.randbytes(200_000)
    _write(src / "Photos" / "img_0.jpg", photo0)
    _write(src / "Photos" / "copy-of-img_0.jpg", photo0)     # same bytes: stored once
    for i in range(1, 8):
        _write(src / "Photos" / f"img_{i}.jpg", rnd.randbytes(200_000))
    for i in range(10):
        _write(src / "Projects" / "app" / "src" / f"module_{i}.py", rnd.randbytes(3_000))
    _write(src / "Projects" / "app" / "build" / "output.bin", rnd.randbytes(3_000_000))
    _write(src / "Archive" / "big-video.mp4", rnd.randbytes(5_000_000))


def _generation_2(src: Path, rnd: random.Random) -> None:
    for i in range(3):
        _write(src / "Documents" / f"notes-{i}.txt", rnd.randbytes(2_500))
    for i in (8, 9):
        _write(src / "Photos" / f"img_{i}.jpg", rnd.randbytes(200_000))
    (src / "Projects" / "app" / "src" / "module_9.py").unlink()


def _generation_3(src: Path, rnd: random.Random) -> None:
    (src / "Archive" / "big-video.mp4").unlink()             # now held only by snapshots 1 and 2
    _write(src / "Documents" / "added-in-third.txt", rnd.randbytes(1_000))


def kopia_run(kopia: str, env: dict, config: Path, *args: str) -> str:
    cmd = [kopia, *args, "--config-file", str(config)]
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    if done.returncode:
        raise RuntimeError(f"kopia {' '.join(args)} failed: {done.stderr.strip()[-800:]}")
    return done.stdout


def build_fixture_repo(root: Path, kopia: str) -> dict:
    """A filesystem repository with one source and three snapshots of a seeded tree.

    The connection is written to kopia's default config file inside the isolated APPDATA, so
    onboarding lists it under "Found on this computer"."""
    env = isolated_env(root, kopia)
    src, repo = root / "source", root / "repo"
    config = root / "appdata" / "kopia" / "repository.config"
    config.parent.mkdir(parents=True, exist_ok=True)
    kopia_run(kopia, env, config, "repository", "create", "filesystem", "--path", str(repo),
              "--password", REPO_PASSWORD, "--no-use-credential-manager",
              "--override-hostname", HOST, "--override-username", USER)
    rnd = random.Random(20260930)
    today = dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    for step, days in zip((_generation_1, _generation_2, _generation_3), SNAPSHOT_DAYS_AGO):
        step(src, rnd)
        when = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S UTC")
        kopia_run(kopia, env, config, "snapshot", "create", str(src),
                  "--start-time", when, "--end-time", when)
    return {"source": str(src), "repo": str(repo), "config": str(config), "env": env}


# ---------------------------------------------------------------- KopiaLens server

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    """One KopiaLens process on a free loopback port with its own data folder."""

    def __init__(self, root: Path, env: dict, name: str = "app"):
        self.data = root / f"data-{name}"
        self.log = root / f"server-{name}.log"
        self.env = {**env, "KOPIALENS_DATA_DIR": str(self.data), "KOPIALENS_PROFILE": "server",
                    "KOPIALENS_BIND_HOST": "127.0.0.1", "KOPIALENS_PORT": str(_free_port())}
        self.url = f"http://127.0.0.1:{self.env['KOPIALENS_PORT']}"
        self.proc: subprocess.Popen | None = None
        self._http = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def start(self, timeout: float = 60) -> "Server":
        self._out = open(self.log, "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "main.py"], cwd=APP_ROOT, env=self.env,
                                     stdout=self._out, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"KopiaLens exited early:\n{self.tail()}")
            try:
                self.get("/api/onboarding/state")
                return self
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.3)
        raise RuntimeError(f"KopiaLens did not start within {timeout}s:\n{self.tail()}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if getattr(self, "_out", None):
            self._out.close()

    def restart(self) -> "Server":
        self.stop()
        return self.start()

    def tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no log)"

    # Small JSON client (keeps its own session cookie) for arranging state; a UI test asserts
    # through the browser, not here.
    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with self._http.open(req, timeout=60) as resp:
            return json.loads(resp.read() or b"{}")

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self.request("POST", path, body or {})

    def sign_in(self, username: str, password: str) -> None:
        form = urllib.parse.urlencode({"username": username, "password": password}).encode()
        self._http.open(urllib.request.Request(self.url + "/api/auth/login", data=form),
                        timeout=60).read()

    def wait_indexed(self, repo: str, timeout: float = 300) -> dict:
        deadline = time.monotonic() + timeout
        status: dict = {}
        while time.monotonic() < deadline:
            status = self.get(f"/api/index/status?repo={repo}")
            if not status.get("building") and not status.get("never_indexed"):
                return status
            time.sleep(1)
        raise RuntimeError(f"repo {repo} not indexed after {timeout}s: {status}\n{self.tail()}")


# ---------------------------------------------------------------- page helpers

def sign_in(page, server: Server, password: str = PASSWORD) -> None:
    page.goto(server.url + "/login")
    page.fill("input[name=username]", USERNAME)
    page.fill("input[name=password]", password)
    page.locator("button[type=submit]").click()
    page.locator("button.tab").first.wait_for(timeout=30_000)


def go_tab(page, label: str) -> None:
    page.locator("button.tab", has_text=label).first.click()
