"""Who runs backups for a repository: detection of other kopia engines on this computer.

KopiaUI starts one `kopia server start` per `*.config` in kopia's config folder, BlinkDisk one per
vault; a server schedules snapshots and runs maintenance. Two engines on one repository take every
snapshot twice, and kopia only serializes maintenance between engines that share one config file
(traps 57-58). KopiaLens therefore schedules a repository only when the user chose it and no other
live engine is found.

Certain signals decide (a running server on our config or on a config for the same storage). Hints
(snapshots or maintenance runs KopiaLens did not start) only warn: the kopia command line produces
them innocently.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import config

from .applog import get_logger

log = get_logger("coexist")

_SCAN_TTL = 10.0
_scan_lock = threading.Lock()
_scan_cache: tuple[float, tuple[list[dict], bool]] = (0.0, ([], False))
_OWNER_TTL = 1800.0
_owner_cache: dict[str, tuple[float, dict]] = {}

MOVED_SUFFIX = ".moved-to-kopialens"   # a KopiaUI config KopiaLens took over (engine.set_choice)

APP_LABELS = {"kopiaui": "KopiaUI", "blinkdisk": "BlinkDisk", "kopia": "A kopia server",
              "kopialens": "KopiaLens"}

# The storage fields that name one repository location, per kopia storage type.
_LOCATION_FIELDS = {
    "s3": ("endpoint", "bucket", "prefix"), "b2": ("bucket", "prefix"), "gcs": ("bucket", "prefix"),
    "azureBlob": ("storageAccount", "container", "prefix"), "sftp": ("host", "port", "path"),
    "webdav": ("url",), "rclone": ("remotePath",), "gdrive": ("folderID",),
}


def read_config(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - an unreadable config is a normal answer here
        return {}


def storage_key(cfg: dict) -> str | None:
    """One string per repository location, comparable across config files. Never carries a secret."""
    api = cfg.get("apiServer") or {}
    if api:
        return "server|" + str(api.get("url") or "").rstrip("/").lower()
    storage = cfg.get("storage") or {}
    stype = storage.get("type") or ""
    conf = storage.get("config") or {}
    if not stype:
        return None
    if stype == "filesystem":
        path = conf.get("path") or ""
        return "filesystem|" + os.path.normcase(os.path.abspath(path)) if path else None
    fields = _LOCATION_FIELDS.get(stype)
    if not fields:
        return None
    return stype + "|" + "|".join(str(conf.get(f) or "").strip().rstrip("/").lower() for f in fields)


def identity_of(cfg: dict) -> str:
    """``user@host`` of a connection config ("" when the file has none)."""
    user, host = cfg.get("username") or "", cfg.get("hostname") or ""
    return f"{user}@{host}" if user and host else ""


def config_path(repo: str) -> str:
    """The config file a repo uses (kopia's default connection when the record names none)."""
    from . import repos
    from .repoconnect import kopia_config_dir
    return repos.config_path_for(repo) or os.path.join(kopia_config_dir(), "repository.config")


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ---- running engines -------------------------------------------------------------------------

def _flag_value(cmd: list[str], flag: str) -> str | None:
    for i, arg in enumerate(cmd):
        if arg == flag and i + 1 < len(cmd):
            return cmd[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


def _classify(exe: str, parent: str, pid: int, ours: set[int]) -> str:
    """Which app started a server. By its parent process: KopiaUI's bundled kopia.exe is often the
    kopia on PATH, so a server started by hand from it is not KopiaUI's."""
    if pid in ours:
        return "kopialens"
    parent = parent.lower()
    if parent.startswith("kopiaui") or parent.startswith("kopia-ui"):
        return "kopiaui"
    if "blinkdisk" in parent or "blinkdisk" in exe.lower():
        return "blinkdisk"
    return "kopia"


def _scan() -> tuple[list[dict], bool]:
    """One pass over the process list: every `kopia server start` this user can see (pid, app,
    config file, read-only) and whether KopiaUI is open. Names only for every process (cheap); the
    command line and parent are read only for processes named kopia or BlinkDisk. psutil's parent
    lookup takes a process snapshot per call: asking it of every process took ~7 s here and stalled
    the app's requests."""
    servers: list[dict] = []
    try:
        import psutil
    except ImportError:
        return servers, False
    from . import engine
    from .repoconnect import kopia_config_dir
    ours = engine.server_pids()
    default_cfg = os.path.join(kopia_config_dir(), "repository.config")
    names: dict[int, str] = {}
    candidates: list[int] = []
    kopiaui_open = False
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        names[proc.info["pid"]] = name
        if name.startswith("kopiaui") or name.startswith("kopia-ui"):
            kopiaui_open = True
        elif "kopia" in name or "blinkdisk" in name:
            candidates.append(proc.info["pid"])
    for pid in candidates:
        try:
            proc = psutil.Process(pid)
            cmd = proc.cmdline()
            if "server" not in cmd or "start" not in cmd:
                continue
            exe = proc.exe() or (cmd[0] if cmd else "")
            parent = names.get(proc.ppid(), "")
            servers.append({"pid": pid, "exe": exe, "parent": parent,
                            "app": _classify(exe, parent, pid, ours),
                            "config": _flag_value(cmd, "--config-file") or default_cfg,
                            "readonly": "--readonly" in cmd})
        except Exception:  # noqa: BLE001 - access denied / process ended mid-scan
            continue
    return servers, kopiaui_open


def _scanned(force: bool = False) -> tuple[list[dict], bool]:
    global _scan_cache
    with _scan_lock:
        if force or time.time() - _scan_cache[0] >= _SCAN_TTL:
            servers, open_ = _scan()
            _scan_cache = (time.time(), (servers, open_))
        return _scan_cache[1]


def kopia_servers(force: bool = False) -> list[dict]:
    """Every running `kopia server start` this user can see. Cached briefly; a process of another
    user (or the host, from a container) is invisible."""
    return _scanned(force)[0]


def kopiaui_running() -> bool:
    return _scanned()[1]


def kopiaui_autostart() -> bool:
    """KopiaUI's "Launch At Startup" is on (best effort, per platform)."""
    try:
        if os.name == "nt":
            import winreg
            run = r"Software\Microsoft\Windows\CurrentVersion\Run"
            approved = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        return False
                    i += 1
                    if "kopiaui" not in str(value).lower():
                        continue
                    try:
                        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved) as ak:
                            data, _ = winreg.QueryValueEx(ak, name)
                            if isinstance(data, bytes) and data[:1] == b"\x03":
                                continue   # turned off in Task Manager's startup list
                    except OSError:
                        pass
                    return True
        # KopiaUI uses the auto-launch package with the name "Kopia": ~/Library/LaunchAgents/Kopia.plist
        # on macOS, ~/.config/autostart/Kopia.desktop on Linux (always ~/.config, not XDG_CONFIG_HOME).
        if sys.platform == "darwin":
            paths = [os.path.expanduser("~/Library/LaunchAgents/Kopia.plist")]
        else:
            dirs = {os.path.expanduser("~/.config"), os.environ.get("XDG_CONFIG_HOME") or ""}
            paths = [os.path.join(d, "autostart", n) for d in dirs if d
                     for n in ("Kopia.desktop", "KopiaUI.desktop")]
        for path in paths:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read().lower()
            if ("kopiaui" in text or "kopia-ui" in text) and "hidden=true" not in text:
                return True
        return False
    except Exception:  # noqa: BLE001 - an unreadable startup list just means "unknown"
        return False


def blinkdisk_vault_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "blinkdisk", "vaults")


def _configs_in(folder: str) -> list[str]:
    if not folder or not os.path.isdir(folder):
        return []
    return [os.path.join(folder, n) for n in sorted(os.listdir(folder))
            if n.endswith(".config") and os.path.isfile(os.path.join(folder, n))]


def is_blinkdisk_vault(unique_id_hex: str | None) -> bool:
    """BlinkDisk vaults carry their vault id ("vlt_...") as the repository unique ID."""
    try:
        text = bytes.fromhex(unique_id_hex or "").decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return False
    return text.startswith("vlt_") and text.isprintable()


# ---- per-repository view ---------------------------------------------------------------------

def maintenance_info(repo: str, force: bool = False) -> dict:
    """`maintenance info --json`, cached. An old answer is served while a background thread asks
    kopia again, so a page load waits for kopia only the first time."""
    hit = _owner_cache.get(repo)
    if hit and not force:
        if time.time() - hit[0] >= _OWNER_TTL and repo not in _owner_refreshing:
            _owner_refreshing.add(repo)
            threading.Thread(target=_refresh_maintenance_info, args=(repo,), daemon=True).start()
        return hit[1]
    return _read_maintenance_info(repo)


_owner_refreshing: set[str] = set()


def _refresh_maintenance_info(repo: str) -> None:
    try:
        _read_maintenance_info(repo)
    finally:
        _owner_refreshing.discard(repo)


def _read_maintenance_info(repo: str) -> dict:
    from .kopia import kopia
    data: dict = {}
    try:
        res = kopia(repo, ["maintenance", "info", "--json"], timeout=120)
        if res.returncode == 0 and (res.stdout or "").strip():
            data = json.loads(res.stdout)
    except Exception as exc:  # noqa: BLE001 - an offline repo has no answer
        log.debug("maintenance info repo=%s failed: %s", repo, exc)
    _owner_cache[repo] = (time.time(), data)
    return data


def repo_view(repo: str, with_owner: bool = False) -> dict:
    """Detection facts for one repository: who runs its backups now, and whether KopiaLens may."""
    from .repoconnect import kopia_config_dir
    rec = config.repo_record(repo)
    path = config_path(repo)
    cfg = read_config(path)
    key = storage_key(cfg)
    view = {
        "repo": repo, "choice": "kopialens" if rec.get("engine") == "kopialens" else "external",
        "config": path, "identity": identity_of(cfg), "running": [], "kopiaui_config": "",
        "kopiaui_shares_file": False, "kopiaui_installed": False, "kopiaui_running": False,
        "kopiaui_autostart": False, "blinkdisk_vault": "", "blinkdisk": False,
        "engine": "none", "conflict": "", "owner": None, "owner_is_us": None,
    }
    if not key:
        return view
    for srv in kopia_servers():
        if srv["readonly"]:
            continue            # a read-only server never schedules or runs maintenance
        # A server keeps running on a config KopiaLens renamed until its app restarts.
        other = read_config(srv["config"]) or read_config(srv["config"] + MOVED_SUFFIX)
        if _same_file(srv["config"], path) or storage_key(other) == key:
            view["running"].append({"app": srv["app"], "label": APP_LABELS[srv["app"]],
                                    "pid": srv["pid"], "config": srv["config"],
                                    "same_file": _same_file(srv["config"], path)})
    from . import kopiabin
    # A KopiaUI AppImage has no install folder to find: seen running or starting at logon counts.
    view["kopiaui_installed"] = kopiabin.kopiaui_uses_kopia_folder() or (
        sys.platform.startswith("linux") and (kopiaui_running() or kopiaui_autostart()))
    for other in _configs_in(kopia_config_dir()):
        if storage_key(read_config(other)) == key:
            view["kopiaui_config"] = other
            view["kopiaui_shares_file"] = _same_file(other, path)
            break
    if view["kopiaui_installed"] and view["kopiaui_config"]:
        view["kopiaui_running"] = kopiaui_running()
        view["kopiaui_autostart"] = kopiaui_autostart()
    for vault in _configs_in(blinkdisk_vault_dir()):
        if storage_key(read_config(vault)) == key:
            view["blinkdisk_vault"] = vault
            break
    view["blinkdisk"] = bool(view["blinkdisk_vault"]) or is_blinkdisk_vault(rec.get("unique_id"))
    others = [r for r in view["running"] if r["app"] != "kopialens"]
    if others:
        view["engine"] = others[0]["app"]
        view["conflict"] = f"{others[0]['label']} is running backups for this repository."
    elif any(r["app"] == "kopialens" for r in view["running"]):
        view["engine"] = "kopialens"
    elif view["kopiaui_running"] and view["kopiaui_config"] and view["choice"] != "kopialens":
        view["engine"] = "kopiaui"     # KopiaUI open, its server not seen yet: it starts one per file
    elif view["blinkdisk"] and view["choice"] != "kopialens":
        view["engine"] = "blinkdisk"
    if with_owner:
        owner = maintenance_info(repo).get("owner")
        view["owner"] = owner
        view["owner_is_us"] = (owner == view["identity"]) if owner and view["identity"] else None
    return view


def identity_check(repo: str) -> dict | None:
    """This computer's name appears in more than one letter case (BlinkDisk writes
    ``Anshul@Anshul-Desktop``; kopia's default is lowercase). ``suggested`` is the spelling to keep:
    the maintenance owner's when it is one of them, else the one with the most snapshots (the
    current one on a tie), so it does not flip after Connect as. ``connect_needed``: this connection
    uses another spelling (a snapshot from here starts a separate history). ``merge_from``: another
    spelling that still holds snapshots. None when neither applies."""
    from .sources import sources_index
    cfg = read_config(config_path(repo))
    user, host = cfg.get("username") or "", cfg.get("hostname") or ""
    if not user or not host:
        return None
    try:
        index = sources_index(repo)
    except Exception:  # noqa: BLE001 - no listing, nothing to compare
        return None
    current = f"{user}@{host}"
    snaps: dict[str, int] = {}
    sources: dict[str, int] = {}
    for k, rows in index.items():
        userhost = k.split(":", 1)[0]
        if userhost.startswith("BLINKDISK-DRAFT-") or userhost.lower() != current.lower():
            continue
        snaps[userhost] = snaps.get(userhost, 0) + len(rows or [])
        sources[userhost] = sources.get(userhost, 0) + 1
    if not snaps or set(snaps) == {current}:
        return None
    owner = maintenance_info(repo).get("owner") or ""
    if owner.lower() == current.lower():
        target = owner
    else:
        target = max([current, *snaps], key=lambda s: (snaps.get(s, 0), s == current))
    others = sorted((s for s in snaps if s != target), key=lambda s: -snaps[s])
    merge_from = others[0] if others else None
    connect_needed = current != target
    if not connect_needed and not merge_from:
        return None
    vu, _, vh = target.partition("@")
    from . import kopiabin
    from .repoconnect import kopia_config_dir
    in_kopiaui_folder = os.path.normcase(os.path.dirname(os.path.abspath(config_path(repo)))) == \
        os.path.normcase(os.path.abspath(kopia_config_dir()))
    return {"current": current, "suggested": target, "sources": sources.get(target, 0),
            "connect_needed": connect_needed, "merge_from": merge_from, "split": merge_from is not None,
            "user": vu, "host": vh,
            "kopiaui_shares_file": in_kopiaui_folder and kopiabin.kopiaui_uses_kopia_folder()}
