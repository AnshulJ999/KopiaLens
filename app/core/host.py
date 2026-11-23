"""What the machine running KopiaLens can do for the person using it: open a folder on its screen,
show the Windows folder picker, mount snapshots, keep its data across restarts."""

from __future__ import annotations

import os
import re
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


MOUNTINFO = "/proc/self/mountinfo"
_SYSTEM_MOUNTS = ("/proc", "/sys", "/dev", "/etc", "/run")


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/\\")
    return path == root or path.startswith(root + "/") or path.startswith(root + "\\")


def attached_folders() -> list[str]:
    """In Docker: the folders attached to the container (volumes), apart from KopiaLens's own."""
    if not in_docker():
        return []
    try:
        with open(MOUNTINFO, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    own = [os.path.abspath(DATA_DIR)] + [os.path.abspath(d) for d in (mount_dir(),) if d]
    out: list[str] = []
    for line in lines:
        parts = line.split(" ")
        if len(parts) < 5:
            continue
        # The mount point is the fifth field; the kernel writes spaces and the like as octal escapes.
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[4])
        if point in ("/", "") or any(_under(point, r) for r in _SYSTEM_MOUNTS + tuple(own)):
            continue
        if point not in out and os.path.isdir(point):
            out.append(point)
    return sorted(out)


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
    """What Browse… opens in a browser: "native" (the Windows dialog on this PC's screen) only
    when chosen and the browser runs on this PC, else "browser" (KopiaLens's own folder list).
    The desktop app opens its own dialog and never asks."""
    choice = str(_kl().get("folder_picker") or "auto")
    if choice == "native" and native_picker_possible() and (client_host or "") in _LOOPBACK:
        return "native"
    return "browser"


def folder_roots() -> list[dict]:
    """Where the in-app folder browser starts: drives on Windows, ``/`` plus mounted disks elsewhere."""
    from .disklookup import list_drives
    if os.name == "nt":
        import psutil
        roots = sorted({p.mountpoint for p in psutil.disk_partitions(all=False) if ":" in (p.mountpoint or "")})
        return [{"name": r.rstrip("\\"), "path": r} for r in roots]
    out = [{"name": "/", "path": "/"}]
    out += [{"name": d["path"], "path": d["path"]} for d in list_drives(local=True) if d["path"] != "/"]
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


def mac_webdav_mount() -> bool:
    """macOS without macFUSE: kopia mounts through its built-in WebDAV server (`mount -t webdav`)."""
    return sys.platform == "darwin" and not os.path.isdir("/Library/Filesystems/macfuse.fs")


def mount_support() -> dict:
    """Whether kopia can mount a snapshot here, and if not, what is missing."""
    if os.name == "nt":
        return {"ok": True, "reason": ""}
    if os.environ.get("KOPIALENS_HA_MOUNTING") == "off":    # the Home Assistant add-on's option
        return {"ok": False, "reason": "Snapshot mounting is off. Turn it on in the add-on's Configuration tab."}
    if sys.platform == "darwin":
        return {"ok": True, "reason": ""}   # macFUSE when installed, else macOS's own WebDAV mount
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


def ram_bytes() -> int:
    """Memory KopiaLens may use: physical RAM, or the container's memory limit when that is lower
    (a 2 GB container on a 16 GB machine must size itself for 2 GB). 0 when unknown."""
    total = 0
    try:
        import psutil
        total = int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001 - unknown RAM: callers use their cautious default
        pass
    limit = _cgroup_memory_limit()
    return limit if limit and (not total or limit < total) else total


def _cgroup_memory_limit() -> int:
    """The tightest cgroup memory limit on this process (v2 along its own cgroup path, else v1)."""
    if not sys.platform.startswith("linux"):
        return 0
    root = "/sys/fs/cgroup"
    paths: list[str] = []
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            rel = next((ln.split("::", 1)[1].strip() for ln in fh if ln.startswith("0::")), None)
    except OSError:
        rel = None
    if rel is not None:
        d = os.path.normpath(os.path.join(root, rel.lstrip("/")))
        while d.startswith(root):
            paths.append(os.path.join(d, "memory.max"))
            if d == root:
                break
            d = os.path.dirname(d)
    paths.append(os.path.join(root, "memory", "memory.limit_in_bytes"))
    best = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw.isdigit() and 0 < int(raw) < 1 << 60:   # "max" / v1's huge number = no limit
            best = min(best, int(raw)) if best else int(raw)
    return best


_FORMATS: dict | None = None


def _windows_formats() -> dict:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as key:
        def val(name: str) -> str:
            try:
                return str(winreg.QueryValueEx(key, name)[0])
            except OSError:
                return ""
        out: dict = {"locale": val("LocaleName") or None}
        short_time, short_date = val("sShortTime"), val("sShortDate")
        if short_time:
            out["clock"] = "24" if "H" in short_time else "12"
        pos = {c: short_date.find(c) for c in "dMy" if c in short_date}
        if len(pos) == 3:
            out["date"] = "iso" if pos["y"] < pos["M"] else ("dmy" if pos["d"] < pos["M"] else "mdy")
        dec, thou, grouping = val("sDecimal"), val("sThousand"), val("sGrouping")
        if dec == "." and thou == ",":
            out["number"] = "en-IN" if grouping.startswith("3;2") else "en-US"
        elif dec == "," and thou == ".":
            out["number"] = "de-DE"
        elif dec == "," and thou in (" ", " ", " "):
            out["number"] = "fr-FR"
        return out


def _posix_formats() -> dict:
    """The locale from the environment (Linux, Docker) or macOS's own setting."""
    tag = ""
    if sys.platform == "darwin":
        try:
            tag = subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True,
                                 timeout=5, stdin=subprocess.DEVNULL).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            tag = ""
    tag = tag or os.environ.get("LC_ALL") or os.environ.get("LC_TIME") or os.environ.get("LANG") or ""
    tag = tag.split(".")[0].split("@")[0].replace("_", "-")
    return {"locale": tag if tag and tag not in ("C", "POSIX") else None}


def os_formats() -> dict:
    """This computer's own clock, date order and number style ("follow the computer"). Read once;
    a field is absent when it can't be told."""
    global _FORMATS
    if _FORMATS is None:
        try:
            _FORMATS = _windows_formats() if os.name == "nt" else _posix_formats()
        except Exception as exc:  # noqa: BLE001 - unreadable: the browser's own style is used
            log.warning("could not read this computer's formats: %s", exc)
            _FORMATS = {}
    return dict(_FORMATS)


def restart_kind() -> str:
    """How the app's Restart works here: "desktop" (the desktop app restarts its server), "addon"
    (Home Assistant's watchdog), "container" (Docker's restart rule), or "" (a service or a source
    run, where whatever started KopiaLens restarts it)."""
    if os.environ.get("KOPIALENS_SHELL_TOKEN"):
        return "desktop"
    if not in_docker():
        return ""
    from .basepath import ingress_enabled
    return "addon" if ingress_enabled() else "container"


def info(client_host: str | None) -> dict:
    return {
        # Applies only when the browser runs on this computer; elsewhere its own language decides.
        "formats": {**os_formats(), "local": (client_host or "") in _LOOPBACK},
        "os": os_name(),
        "docker": in_docker(),
        "can_open": has_desktop(),
        "file_manager": file_manager(),
        "folder_picker": folder_picker(client_host),
        "native_picker_possible": native_picker_possible(),
        "mount": {**mount_support(), "dir": mount_dir(), "host_dir": mount_host_dir()},
        "data_persistent": data_persistent(),
        "restart": restart_kind(),
    }
