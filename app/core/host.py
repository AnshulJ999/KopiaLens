"""What the machine running KopiaLens can do for the person using it: open a folder on its screen,
show the Windows folder picker, mount snapshots, keep its data across restarts."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from config import DATA_DIR

from ..settings import load_settings
from .applog import get_logger

log = get_logger("host")

FOLDER_PICKERS = ("auto", "native", "browser")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _kl() -> dict:
    try:
        return load_settings().get("kopialens", {}) or {}
    except Exception:  # noqa: BLE001 - settings trouble falls back to the defaults
        return {}


def os_name() -> str:
    return "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")


def in_docker() -> bool:
    return os.environ.get("KOPIALENS_IN_DOCKER") == "1" or os.path.exists("/.dockerenv")


def has_desktop() -> bool:
    """A screen on this machine where a folder window can open."""
    if in_docker():
        return False
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def file_manager() -> str:
    return {"windows": "Explorer", "macos": "Finder"}.get(os_name(), "file manager")


def open_folder(target: str) -> bool:
    """Show ``target`` in this machine's file manager (a file is selected where the OS allows it).
    Best effort: False when there is no screen or the call fails."""
    if not has_desktop():
        return False
    is_dir = os.path.isdir(target)
    if os.name == "nt":
        args = ["explorer", target] if is_dir else ["explorer", "/select,", target]
    elif sys.platform == "darwin":
        args = ["open", target] if is_dir else ["open", "-R", target]
    else:
        args = ["xdg-open", target if is_dir else os.path.dirname(target)]
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("open_folder failed for %s: %s", target, exc)
        return False


def native_picker_possible() -> bool:
    return os.name == "nt" and has_desktop()


def folder_picker(client_host: str | None) -> str:
    """"native" (the Windows dialog on this PC's screen) or "browser" (KopiaLens's own folder
    list). Automatic picks the dialog only when the browser runs on this same PC."""
    choice = str(_kl().get("folder_picker") or "auto")
    if not native_picker_possible() or choice == "browser":
        return "browser"
    if choice == "native":
        return "native"
    return "native" if (client_host or "") in _LOOPBACK else "browser"


def folder_roots() -> list[dict]:
    """Where the in-app folder browser starts: drives on Windows, ``/`` plus mounted disks elsewhere."""
    from .disklookup import list_drives
    if os.name == "nt":
        import psutil
        roots = sorted({p.mountpoint for p in psutil.disk_partitions(all=False) if ":" in (p.mountpoint or "")})
        return [{"name": r.rstrip("\\"), "path": r} for r in roots]
    out = [{"name": "/", "path": "/"}]
    out += [{"name": d["path"], "path": d["path"]} for d in list_drives() if d["path"] != "/"]
    return out


def list_folders(path: str, limit: int = 5000) -> dict:
    """The folders directly inside ``path`` on this machine (names only, nothing is read inside)."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    folders = []
    hidden = 0x2 | 0x4   # FILE_ATTRIBUTE_HIDDEN | SYSTEM: what Explorer hides by default
    with os.scandir(path) as it:
        for entry in it:
            try:
                if not entry.is_dir():
                    continue
                if os.name == "nt" and getattr(entry.stat(), "st_file_attributes", 0) & hidden:
                    continue
                folders.append({"name": entry.name, "path": entry.path})
            except OSError:
                continue
    folders.sort(key=lambda f: f["name"].casefold())
    parent = os.path.dirname(path.rstrip("\\/")) if path.rstrip("\\/") else ""
    if parent and os.name == "nt" and len(parent) == 2:
        parent += "\\"
    return {"path": path, "parent": parent if parent and parent != path else "",
            "folders": folders[:limit], "truncated": len(folders) > limit}


# ---------------------------------------------------------------------------- snapshot mounts

def mount_dir() -> str | None:
    """The folder snapshot mounts go under (Linux/macOS). Windows mounts to drive letters: None."""
    if os.name == "nt":
        return None
    return (os.environ.get("KOPIALENS_MOUNT_DIR") or str(_kl().get("mount_dir") or "").strip()
            or os.path.join(DATA_DIR, "mounts"))


def mount_host_dir() -> str:
    """The same folder as the host machine sees it (Docker), shown in place of mount_dir."""
    return str(_kl().get("mount_host_path") or "").strip()


def shown_path(path: str) -> str:
    """``path`` as the user finds it: a mount inside Docker becomes its folder on the host."""
    root, host = mount_dir(), mount_host_dir()
    if not (root and host and path):
        return path
    root = root.rstrip("/")
    if path == root or path.startswith(root + "/"):
        return host.rstrip("/\\") + path[len(root):]
    return path


def fusermount() -> str | None:
    return shutil.which("fusermount3") or shutil.which("fusermount")


def mount_support() -> dict:
    """Whether kopia can mount a snapshot here, and if not, what is missing."""
    if os.name == "nt":
        return {"ok": True, "reason": ""}
    if sys.platform == "darwin":
        ok = os.path.isdir("/Library/Filesystems/macfuse.fs")
        return {"ok": ok, "reason": "" if ok else "Mounting needs macFUSE (osxfuse.github.io)."}
    if not os.path.exists("/dev/fuse"):
        reason = ("Mounting needs FUSE. Add the FUSE device to the container (see the Docker guide)."
                  if in_docker() else "Mounting needs FUSE (install the fuse3 package).")
        return {"ok": False, "reason": reason}
    if not fusermount():
        return {"ok": False, "reason": "Mounting needs fusermount3 (install the fuse3 package)."}
    return {"ok": True, "reason": ""}


def data_persistent() -> bool:
    """False when a container keeps the data folder in its own throwaway layer."""
    if not in_docker():
        return True
    path = os.path.realpath(DATA_DIR)
    while True:
        if os.path.ismount(path):
            return path != "/"
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def info(client_host: str | None) -> dict:
    return {
        "os": os_name(),
        "docker": in_docker(),
        "can_open": has_desktop(),
        "file_manager": file_manager(),
        "folder_picker": folder_picker(client_host),
        "native_picker_possible": native_picker_possible(),
        "mount": {**mount_support(), "dir": mount_dir(), "host_dir": mount_host_dir()},
        "data_persistent": data_persistent(),
    }
