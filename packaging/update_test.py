"""Tests a real in-app update: installs the old build, lets it find the new one on a small local update
server, installs it through the app and checks that the new version runs with its settings kept.

    python packaging\\update_test.py <old -updatetest setup.exe> <new release folder>
    python packaging\\update_test.py --here <old -updatetest setup.exe> <new release folder>

Default: everything runs inside Windows Sandbox and nothing on this PC is touched.
--here: updates the test bed's installed copy on this PC (port 7128, its own data folder) instead.
The script installs the old build over it, starts it and serves the update; a person then picks the
Beta channel, checks for updates, clicks Install and answers the Windows prompt, while the script
watches and checks the result. For when Windows Sandbox is broken (trap 76).

The old build is an update-test build pointed at the local server:
    release.py --update-endpoint=http://127.0.0.1:8765/v1/update/{{target}}-{{arch}}/{{current_version}}
The new one is a normal release build (its folder has latest.json and the setup.exe it names).
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_test  # noqa: E402

PORT = 8765
WORK = os.path.join(installer_test.BUILD_ROOT, "update-test")
SCRIPT = "update_test_sandbox.ps1"
SERVER = "update_test_server.ps1"

# The test bed's installed copy (devtools/testbed/README.md).
HERE_EXE = r"C:\Program Files\KopiaLens\KopiaLens.exe"
HERE_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "KopiaLens")
HERE_URL = "http://127.0.0.1:7128"


def _load(new_dir: str) -> tuple[dict, str]:
    with open(os.path.join(new_dir, "latest.json"), encoding="utf-8") as fh:
        latest = json.load(fh)
    win = latest["platforms"]["windows-x86_64"]
    new_setup = os.path.join(new_dir, win["url"].rsplit("/", 1)[1])
    if not os.path.isfile(new_setup):
        raise SystemExit(f"Missing installer: {new_setup}")
    # The update is downloaded from the local server; the signature covers the file, not the address.
    win["url"] = f"http://127.0.0.1:{PORT}/files/new-setup.exe"
    return latest, new_setup


def sandbox(old_setup: str, new_dir: str) -> int:
    latest, new_setup = _load(new_dir)
    tmp = tempfile.mkdtemp(prefix="kl-update-test-")
    served = os.path.join(tmp, "latest.json")
    with open(served, "w", encoding="utf-8") as fh:
        json.dump(latest, fh)
    files = {"old-setup.exe": old_setup, "new-setup.exe": new_setup, "latest.json": served,
             SERVER: os.path.join(installer_test.HERE, SERVER)}
    print(f"Update test: {os.path.basename(old_setup)} -> {latest['version']}")
    result = installer_test.run_sandbox("update test", files, SCRIPT, WORK)
    if isinstance(result, str):
        return 1
    print(f"Update test {'passed' if result.get('ok') else 'FAILED'}: "
          f"{sum(1 for c in result.get('checks') or [] if c.get('ok'))}/{len(result.get('checks') or [])} checks")
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------- --here

def _serve(latest: dict, new_setup: str, requests: list[str]) -> http.server.ThreadingHTTPServer:
    """The same answers as update_test_server.ps1: check-in, updater, and the new installer."""
    body = json.dumps(latest).encode()
    version = latest["version"]

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, data: bytes = b"", kind: str = "application/json") -> None:
            self.send_response(code)
            if data:
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _answer(self) -> None:
            requests.append(f"{time.strftime('%H:%M:%S')} {self.command} {self.path}")
            path = self.path.split("?", 1)[0]
            if path == "/v1/checkin":
                self._send(200, json.dumps({"latest": version, "url": None, "notice": None,
                                            "latest_by_major": {}}).encode())
            elif path.startswith("/v1/update/"):
                if urllib.request.unquote(path.rsplit("/", 1)[-1]) != version:
                    self._send(200, body)
                else:
                    self._send(204)
            elif path == "/files/new-setup.exe":
                with open(new_setup, "rb") as fh:
                    self._send(200, fh.read(), "application/octet-stream")
            else:
                self._send(404)

        do_GET = do_POST = _answer

        def log_message(self, *args) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _version() -> str | None:
    try:
        with urllib.request.urlopen(HERE_URL + "/health", timeout=3) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None


def _wait(cond, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(3)
    return False


def here(old_setup: str, new_dir: str) -> int:
    latest, new_setup = _load(new_dir)
    new = latest["version"]
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'} {name} {detail}".rstrip(), flush=True)
        return ok

    requests: list[str] = []
    srv = _serve(latest, new_setup, requests)
    print(f"Update test on this PC: {os.path.basename(old_setup)} -> {new}, installed copy at {HERE_URL}")
    try:
        if _version():
            print("  closing the installed copy")
            subprocess.run([HERE_EXE, "--quit"], env={**os.environ, "KOPIALENS_DATA_DIR": HERE_DATA}, timeout=120)
            _wait(lambda: not _version(), 60)
        print("  installing the old build (answer Yes if Windows asks)")
        rc = subprocess.run([old_setup, "/S"]).returncode
        if not check("old version installs", rc == 0, f"exit {rc}"):
            return 1
        env = {**os.environ, "KOPIALENS_CHECKIN_URL": f"http://127.0.0.1:{PORT}/v1/checkin"}
        subprocess.Popen([HERE_EXE], env=env, close_fds=True)
        if not check("old version starts", _wait(_version, 180), f"version {_version()}"):
            return 1
        old = _version()
        print("\n  Now, in the KopiaLens window that opened (sign in if asked):\n"
              "    1. Settings > Updates > Update channel: Beta\n"
              "    2. Help > Check for updates, then Install the update it offers\n"
              "    3. Answer Yes if Windows asks\n"
              "  Waiting up to 20 minutes for the new version...\n", flush=True)
        check("the new version runs after the update", _wait(lambda: _version() == new, 1200), f"{old} -> {_version()}")
        try:
            with open(os.path.join(HERE_DATA, "settings.json"), encoding="utf-8") as fh:
                channel = json.load(fh).get("kopialens", {}).get("update_channel")
        except (OSError, ValueError):
            channel = None
        check("the update channel setting is kept", channel == "beta", f"channel={channel}")
        asked = [r for r in requests if "/v1/update/" in r]
        check("the updater asked for the beta channel", any("channel=beta" in r for r in asked), " | ".join(asked[-3:]))
        check("the update was downloaded from the update service", any("/files/new-setup.exe" in r for r in requests))
    finally:
        srv.shutdown()
        os.makedirs(WORK, exist_ok=True)
        with open(os.path.join(WORK, "here-requests.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(requests) + "\n")
    ok = all(c[1] for c in checks)
    print(f"Update test {'passed' if ok else 'FAILED'}: {sum(1 for c in checks if c[1])}/{len(checks)} checks"
          f" (server requests in {os.path.join(WORK, 'here-requests.txt')})")
    return 0 if ok else 1


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--here"]
    if len(args) != 2:
        print(__doc__)
        return 2
    old_setup, new_dir = args
    if not os.path.isfile(old_setup):
        print(f"Missing installer: {old_setup}")
        return 2
    return here(old_setup, new_dir) if "--here" in sys.argv[1:] else sandbox(old_setup, new_dir)


if __name__ == "__main__":
    sys.exit(main())
