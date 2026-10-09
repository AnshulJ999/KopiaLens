"""UI-suite harness: a small known kopia repository and a throwaway KopiaLens server against it.

Everything lives under one temp root. ``APPDATA``/``LOCALAPPDATA`` point inside it, so neither
kopia nor KopiaLens sees the developer's real kopia connections, caches or keyring entries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import random
import site
import socket
import ssl
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


# ---------------------------------------------------------------- child cleanup
# Every process the harness starts must die with the test run, including a hard exit (a crash or
# the watchdog) where fixture teardown never runs. Windows: a kill-on-close job object, which
# also takes the children's own children. Elsewhere: best effort at interpreter exit.

_JOB = None
_POSIX_CHILDREN: list = []


def _windows_job():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.Basic.LimitFlags = 0x2000                # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "could not create the cleanup job object")
    return k32, job


def contain(proc: subprocess.Popen) -> subprocess.Popen:
    """Tie ``proc`` (and anything it starts) to this test run's lifetime."""
    global _JOB
    if os.name == "nt":
        if _JOB is None:
            _JOB = _windows_job()           # never closed: the handle closes when this process ends
        k32, job = _JOB
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            print(f"warning: pid {proc.pid} is not tied to the test run", file=sys.stderr)
    else:
        if not _POSIX_CHILDREN:
            import atexit
            atexit.register(lambda: [p.kill() for p in _POSIX_CHILDREN if p.poll() is None])
        _POSIX_CHILDREN.append(proc)
    return proc


def isolated_env(root: Path, kopia: str) -> dict:
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("KOPIALENS_") or k.startswith("KOPIA_")]:
        del env[key]
    # Python's user site-packages live under APPDATA on Windows: keep them where they are.
    env.setdefault("PYTHONUSERBASE", site.getuserbase())
    env.update(APPDATA=str(root / "appdata"), LOCALAPPDATA=str(root / "localappdata"),
               KOPIALENS_KOPIA=kopia, KOPIA_CHECK_FOR_UPDATES="false",
               KOPIALENS_DEV_ADDONS="")   # empty beats a developer's .env.dev: no dev add-ons in tests
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


# ---------------------------------------------------------------- kopia repository server

SERVER_USER_PASSWORD = "server user pw"


def start_kopia_server(root: Path, kopia: str, env: dict, config: str) -> dict:
    """``kopia server start`` over the fixture repository, with one account for the snapshots'
    owner (a server account sees only its own snapshots). TLS is mandatory for its clients."""
    cfg = Path(config)
    env = {**env, "KOPIA_PASSWORD": REPO_PASSWORD}
    kopia_run(kopia, env, cfg, "server", "user", "add", f"{USER}@{HOST}",
              "--user-password", SERVER_USER_PASSWORD)
    port = _free_port()
    crt, key, log = root / "kopia-server.crt", root / "kopia-server.key", root / "kopia-server.log"
    out = open(log, "a", encoding="utf-8")
    proc = contain(subprocess.Popen([kopia, "--config-file", str(cfg), "server", "start",
                             f"--address=127.0.0.1:{port}", "--tls-generate-cert",
                             "--tls-cert-file", str(crt), "--tls-key-file", str(key),
                             "--server-control-username", "control",
                             "--server-control-password", "control pw", "--no-ui"],
                            env=env, stdout=out, stderr=subprocess.STDOUT))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("kopia server exited early:\n" + log.read_text(errors="replace")[-2000:])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
            if crt.exists() and crt.stat().st_size:
                break
        except OSError:
            pass
        time.sleep(0.3)
    else:
        proc.terminate()
        raise RuntimeError("kopia server did not start within 60s")
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(crt.read_text())).hexdigest()
    return {"proc": proc, "log": out, "url": f"https://127.0.0.1:{port}", "fingerprint": fingerprint,
            "username": USER, "hostname": HOST, "password": SERVER_USER_PASSWORD}


# ---------------------------------------------------------------- KopiaLens server

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


KNOWN_ANSWERS = APP_ROOT.parents[1] / "LicenseKit" / "test-vectors" / "licensekit-known-answers.json"


def known_answer_licence() -> dict | None:
    """LicenseKit's public test key and its lifetime licence, or None without the LicenseKit folder."""
    if not KNOWN_ANSWERS.is_file():
        return None
    data = json.loads(KNOWN_ANSWERS.read_text(encoding="utf-8"))
    token = next(c["token"] for c in data["cases"] if c["name"] == "lifetime")
    return {"kid": data["kid"], "public_key": data["public_key"], "token": token}


class Server:
    """One KopiaLens process on a free loopback port with its own data folder. ``pro`` starts it
    through ``_pro_main.py``, which trusts the test key so ``activate_test_licence`` makes it Pro."""

    def __init__(self, root: Path, env: dict, name: str = "app", pro: bool = False):
        self.pro = pro
        self.data = root / f"data-{name}"
        self.log = root / f"server-{name}.log"
        self.env = {**env, "KOPIALENS_DATA_DIR": str(self.data), "KOPIALENS_PROFILE": "server",
                    "KOPIALENS_BIND_HOST": "127.0.0.1", "KOPIALENS_PORT": str(_free_port()),
                    "KOPIALENS_HTTPS": "off"}   # a server install serves HTTPS by default; the flows test HTTP
        self.url = f"http://127.0.0.1:{self.env['KOPIALENS_PORT']}"
        self.proc: subprocess.Popen | None = None
        self._http = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def start(self, timeout: float = 60) -> "Server":
        self._out = open(self.log, "a", encoding="utf-8")
        entry = str(Path(__file__).with_name("_pro_main.py")) if self.pro else "main.py"
        self.proc = contain(subprocess.Popen([sys.executable, entry], cwd=APP_ROOT, env=self.env,
                                             stdout=self._out, stderr=subprocess.STDOUT))
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

    def activate_test_licence(self) -> dict:
        """Store LicenseKit's known-answer licence; only a ``pro`` server trusts its key."""
        return self.post("/api/licence", {"text": known_answer_licence()["token"]})

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
