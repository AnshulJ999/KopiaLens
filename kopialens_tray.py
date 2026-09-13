"""KopiaLens tray launcher.

Owns the server process lifecycle and exposes a small loopback control API (restart / quit /
status) so a script or an AI session can drive the app without clicking the tray icon. Restart and
quit live HERE rather than in the server because a process cannot kill itself and still answer the
request that asked it to.
"""

from __future__ import annotations

import ctypes
import datetime
import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image, ImageDraw
import pystray

from config import DATA_DIR, GUARD_HOST, PORT, TRAY_CONTROL_HOST, TRAY_CONTROL_PORT

HERE = Path(__file__).resolve().parent
# Browsable URL, so loopback - NOT HOST, which is the 0.0.0.0 bind address. "Open" was handing the
# browser http://0.0.0.0:7123 ever since the server was rebound for LAN access.
URL = f"http://{GUARD_HOST}:{PORT}"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW (Windows) - suppress the child console (no flicker)
proc: subprocess.Popen | None = None
_icon: pystray.Icon | None = None


def _is_first_instance() -> bool:
    """Single-instance guard for the TRAY (the port guard in main.py only covers the server, so
    multiple VBS launches would each spawn a tray icon). Uses a session-scoped named mutex: returns
    True if we are the first tray. The handle is intentionally left open (held for the process
    lifetime); Windows frees it automatically on exit, so there is no stale-lock problem."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW(None, False, "KopiaLensTray_SingleInstance")
        return kernel32.GetLastError() != 183  # 183 = ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 - if the guard itself errors, never block startup
        return True


def _log_guard(msg: str, level: str = "WARNING") -> None:
    """Append one line to the shared log file so a duplicate-launch is identifiable. Plain append
    (NOT a RotatingFileHandler) so it can't clash with the server process's log rotation."""
    try:
        from app.core.applog import LOG_FILE
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {level:<5} kopialens.tray | {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def icon_image() -> Image.Image:
    image = Image.new("RGB", (64, 64), "#fafaf9")
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill="#0f766e")
    draw.ellipse((22, 22, 42, 42), fill="#fafaf9")
    draw.rectangle((42, 42, 58, 50), fill="#0f766e")
    return image


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _workers_running() -> list[dict]:
    """Index workers that look alive, read straight from their own progress files.

    The tray stays a light process - it does NOT import the app package (DuckDB, pyarrow and the
    rest) just to answer a status call. A leftover file from a crashed worker can show up here as
    running; that errs toward "don't restart right now", and the app reaps genuine orphans at
    startup anyway. ``age`` is how many seconds since the worker last wrote (it writes every ~2s),
    so a large age means it is almost certainly dead.
    """
    now = time.time()
    out: list[dict] = []
    for path in glob.glob(os.path.join(DATA_DIR, "index_progress_repo*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception:  # noqa: BLE001 - a half-written or junk file tells us nothing
            continue
        out.append({
            "repo": info.get("repo"), "stage": info.get("stage"), "pid": info.get("pid"),
            "started": info.get("started"), "indexed": info.get("indexed"),
            "total": info.get("total"), "age": round(now - float(info.get("updated") or now), 1),
        })
    return out


def start_server() -> None:
    global proc
    if proc and proc.poll() is None:
        return
    if _port_open(GUARD_HOST, PORT):
        # Something is already serving. Spawning anyway would hit main.py's single-instance guard
        # and exit immediately, leaving `proc` pointing at a dead process - after which Restart and
        # Quit would silently do nothing to the server that is actually running.
        _log_guard(f"tray: KopiaLens already listening on {GUARD_HOST}:{PORT} - not starting another")
        return
    # The tray launch IS the desktop profile: the child resolves its bind/auth defaults
    # from KOPIALENS_PROFILE. Anything started without it (bare python, systemd, Docker) is a
    # server and binds all interfaces by default instead.
    child_env = dict(os.environ)
    child_env["KOPIALENS_PROFILE"] = "desktop"
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "main.py")],
        cwd=str(HERE),
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def _find_server_process():
    """The running KopiaLens server, even when this tray did not start it.

    Matched by command line against our own main.py path - never by port owner (enumerating socket
    owners needs elevation on Windows) and never by PID alone (PIDs get recycled). Without this,
    launching the tray on top of an already-running server left Restart and Quit controlling
    nothing at all."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return None
    target = str(HERE / "main.py").lower()
    me = os.getpid()
    for candidate in psutil.process_iter(["pid", "cmdline"]):
        if candidate.info.get("pid") == me:
            continue
        try:
            cmd = " ".join(candidate.info.get("cmdline") or []).lower()
        except Exception:  # noqa: BLE001 - a process we cannot inspect is not ours
            continue
        if target in cmd:
            return candidate
    return None


def stop_server() -> None:
    global proc
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = None
        return
    proc = None
    external = _find_server_process()   # a server someone else started (bare `python main.py`)
    if external is None:
        return
    _log_guard(f"tray: stopping a KopiaLens server this tray did not start (pid {external.pid})")
    try:
        external.terminate()
        try:
            external.wait(timeout=10)
        except Exception:  # noqa: BLE001
            external.kill()
    except Exception as exc:  # noqa: BLE001 - never let cleanup take the tray down
        _log_guard(f"tray: could not stop server pid {external.pid}: {exc}")


def _wait_port_free(timeout: float = 15.0) -> bool:
    """The old server holds the port for a moment after terminate; starting into that loses the
    race to main.py's own single-instance guard and the restart silently does nothing."""
    end = time.time() + timeout
    while time.time() < end:
        if not _port_open(GUARD_HOST, PORT):
            return True
        time.sleep(0.3)
    return False


def _wait_port_up(timeout: float = 60.0) -> bool:
    """uvicorn takes a second or two to bind after the process starts. Checking the port
    immediately made /restart answer `server_up: false` for a restart that was working perfectly,
    and an automated caller reading that as failure would restart again, and again."""
    end = time.time() + timeout
    while time.time() < end:
        if _port_open(GUARD_HOST, PORT):
            return True
        time.sleep(0.3)
    return False


def _do_restart() -> bool:
    """True once the NEW server is accepting connections.

    The `_wait_port_free` result is load-bearing, not decoration. If the old server refuses to die,
    start_server() declines to spawn (the port is taken) and _wait_port_up() then answers True -
    because the OLD process is still listening. That reports a successful restart for one that
    never happened, which is the same false-success this API was fixed to stop reporting."""
    stop_server()
    if not _wait_port_free():
        _log_guard("tray: the old server never released the port - restart aborted")
        return False
    start_server()
    return _wait_port_up()


def open_ui(_icon=None, _item=None) -> None:
    start_server()
    webbrowser.open(URL)


def restart(_icon=None, _item=None) -> None:
    _do_restart()
    # Do NOT auto-open the browser on restart (user opens via the "Open" menu).


def quit_app(icon: pystray.Icon, _item=None) -> None:
    stop_server()
    icon.stop()


class _ControlHandler(BaseHTTPRequestHandler):
    """POST /restart · POST /quit · GET /status, loopback only.

    /restart does not refuse while an index worker is running - it reports the worker in /status and
    leaves the decision to the caller. A restart mid-pass costs that pass, not the data: the worker
    is reaped at the next startup and the index self-heals on the following run."""

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path == "/restart":
            up = _do_restart()   # waits for the new server to bind before answering
            self._json(200, {"ok": up, "server_up": up})
        elif self.path == "/quit":
            self._json(200, {"ok": True})
            # Answer first, then die - the same reason restart lives in the tray at all.
            threading.Thread(target=self._quit_after_response, daemon=True).start()
        else:
            self._json(404, {"ok": False, "detail": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status":
            self._json(200, {
                "server_up": _port_open(GUARD_HOST, PORT),
                # The process THIS tray spawned; null when the server was started some other way.
                # "server_up" is the answer to "is it running" - this is only "did we start it".
                "pid": proc.pid if proc and proc.poll() is None else None,
                "url": URL,
                "indexing": _workers_running(),
            })
        else:
            self._json(404, {"ok": False, "detail": "not found"})

    @staticmethod
    def _quit_after_response() -> None:
        time.sleep(0.2)
        stop_server()
        if _icon:
            _icon.stop()

    def log_message(self, fmt, *args) -> None:  # keep the control API out of stderr
        line = fmt % args
        if '"GET /status' in line:
            return  # polled constantly - not worth a log line
        _log_guard("tray control: " + line, level="INFO")


def _start_control_server() -> None:
    try:
        srv = HTTPServer((TRAY_CONTROL_HOST, TRAY_CONTROL_PORT), _ControlHandler)
    except OSError as exc:
        _log_guard(f"tray control API failed to bind {TRAY_CONTROL_HOST}:{TRAY_CONTROL_PORT}: {exc}")
        return  # the tray icon still works; only the scripted path is unavailable
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def main() -> None:
    global _icon
    if not _is_first_instance():  # another tray is already running -> exit quietly (logged for diagnosis)
        _log_guard("tray single-instance guard: a KopiaLens tray is already running - this launch is exiting")
        return
    start_server()  # start the backend, but do NOT auto-open the browser (use the tray menu)
    _start_control_server()
    menu = pystray.Menu(
        pystray.MenuItem("Open", open_ui, default=True),
        pystray.MenuItem("Restart", restart),
        pystray.MenuItem("Quit", quit_app),
    )
    _icon = pystray.Icon("KopiaLens", icon_image(), "KopiaLens", menu)
    _icon.run()


if __name__ == "__main__":
    main()
