"""KopiaLens tray launcher."""

from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from PIL import Image, ImageDraw
import pystray

from config import HOST, PORT

HERE = Path(__file__).resolve().parent
URL = f"http://{HOST}:{PORT}"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW (Windows) - suppress the child console (no flicker)
proc: subprocess.Popen | None = None


def icon_image() -> Image.Image:
    image = Image.new("RGB", (64, 64), "#fafaf9")
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill="#0f766e")
    draw.ellipse((22, 22, 42, 42), fill="#fafaf9")
    draw.rectangle((42, 42, 58, 50), fill="#0f766e")
    return image


def start_server() -> None:
    global proc
    if proc and proc.poll() is None:
        return
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "main.py")],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def stop_server() -> None:
    global proc
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def open_ui(_icon=None, _item=None) -> None:
    start_server()
    webbrowser.open(URL)


def restart(_icon=None, _item=None) -> None:
    stop_server()
    start_server()
    time.sleep(0.5)
    webbrowser.open(URL)


def quit_app(icon: pystray.Icon, _item=None) -> None:
    stop_server()
    icon.stop()


def main() -> None:
    start_server()  # start the backend, but do NOT auto-open the browser (use the tray menu)
    menu = pystray.Menu(
        pystray.MenuItem("Open", open_ui, default=True),
        pystray.MenuItem("Restart", restart),
        pystray.MenuItem("Quit", quit_app),
    )
    pystray.Icon("KopiaLens", icon_image(), "KopiaLens", menu).run()


if __name__ == "__main__":
    main()
