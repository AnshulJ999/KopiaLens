"""Locate the Backblaze bzdata files (this computer's install, or any configured folder) and
build the drive<->volume map dynamically.

Do NOT hardcode the GUID->letter map (bbtoolkit did, via GUID2LETTER) - it breaks if a
drive/GUID changes. We derive it from `bzvolumes.xml` (decode mountPointPathHex) and the
on-disk per-volume filelist filenames (`v{guid}_{letter}____filelist.dat` on Windows,
`v{guid}_root_filelist.dat` / `v{guid}_{name}_filelist.dat` on a Mac).

The data's platform (Windows or Mac) comes from the bzdata itself, never from the computer
KopiaLens runs on: a shared folder from a Mac can be read on Windows, Linux or in Docker.
"""

from __future__ import annotations

import glob
import os
import re
import socket
import sys
from dataclasses import dataclass

# Default install locations. Overridable by the `backblaze.bzdata_path` setting (apply_configured_root).
DEFAULT_BZDATA = r"C:\ProgramData\Backblaze\bzdata"
MAC_BZDATA = "/Library/Backblaze.bzpkg/bzdata"
WIN_SEP = "\\"
POSIX_SEP = "/"


def default_bzdata() -> str | None:
    """Where Backblaze keeps its data on this computer, or None where Backblaze doesn't run."""
    if os.name == "nt":
        return os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "Backblaze", "bzdata")
    if sys.platform == "darwin":
        return MAC_BZDATA
    return None


def looks_like_bzdata(path: str | None) -> bool:
    """A Backblaze data folder: it holds the per-drive file lists."""
    return bool(path) and os.path.isdir(os.path.join(path, "bzfilelists"))
try:
    from config import DATA_DIR, DEMO_INSTANCE
except ImportError:   # standalone tooling outside the app
    DATA_DIR, DEMO_INSTANCE = "", False
# A demo data folder only ever reads bzdata generated inside its own data folder (the Windows set,
# or a second folder such as bzdata-mac switched in Settings), never this machine's.
DEMO_BZDATA = os.path.join(DATA_DIR, "bzdata") if DEMO_INSTANCE else None
_DEMO_ROOT = os.path.dirname(DEMO_BZDATA) if DEMO_BZDATA else None
_bzdata_root = DEMO_BZDATA or DEFAULT_BZDATA


def _inside_demo(path: str | None) -> bool:
    if not _DEMO_ROOT or not path:
        return False
    root = os.path.normcase(os.path.abspath(_DEMO_ROOT)) + os.sep
    return os.path.normcase(os.path.abspath(path)).startswith(root)


def set_bzdata_root(path: str) -> None:
    global _bzdata_root
    if DEMO_BZDATA is None or _inside_demo(path):
        _bzdata_root = path
    # A demo folder outside its own data is ignored: a demo never reads this machine's Backblaze.


def bzdata_root() -> str:
    return _bzdata_root


def configured_root(settings: dict | None = None) -> str:
    """The folder the settings point at: `backblaze.bzdata_path`, else this computer's default."""
    if settings is None:
        try:
            from ..settings import load_settings
            settings = load_settings()
        except Exception:  # noqa: BLE001 - standalone tooling without the app's settings
            settings = {}
    path = str((settings.get("backblaze") or {}).get("bzdata_path") or "").strip()
    return path or default_bzdata() or _bzdata_root


def apply_configured_root(settings: dict | None = None) -> str:
    """Point the reader at the configured folder. Every process that reads bzdata calls this
    (the app at start and on a settings change, the refresh worker, bz_verify)."""
    set_bzdata_root(configured_root(settings))
    return _bzdata_root


def exists() -> bool:
    return os.path.isdir(_bzdata_root)


# ---- well-known sub-paths (all read-only) ----------------------------------------------
def _p(*parts: str) -> str:
    return os.path.join(_bzdata_root, *parts)


def bzinfo_path() -> str:
    return _p("bzinfo.xml")


def editable_rules_path() -> str:
    return _p("bzexcluderules_editable.xml")


def mandatory_rules_path() -> str:
    return _p("bzexcluderules_mandatory.xml")


def bzvolumes_path() -> str:
    return _p("bzvolumes.xml")


def fileids_path() -> str:
    return _p("bzbackup", "bzfileids.dat")


def fileids_total_hint_path() -> str:
    """Demo data only: the full file-id count, kept after `trim` shortens bzfileids.dat."""
    return _p("bzbackup", "bzfileids_total.txt")


def datacenter_dir() -> str:
    return _p("bzbackup", "bzdatacenter")


def filelists_dir() -> str:
    return _p("bzfilelists")


def reports_dir() -> str:
    return _p("bzreports")


def eventlog_dir() -> str:
    return _p("bzlogs", "bzreports_eventlog")


def transmit_log_dir() -> str:
    return _p("bzlogs", "bztransmit")


def lastfiles_dir() -> str:
    return _p("bzlogs", "bzreports_lastfilestransmitted")


def report(name: str) -> str:
    return _p("bzreports", name)


def client_version_path() -> str:
    return _p("bzupdates", "clientversion.xml")


def log_dir(name: str) -> str:
    return _p("bzlogs", name)


# ---- drive / volume map ----------------------------------------------------------------
# The token is the drive letter on Windows (padded with underscores) and "root" or the start of
# the drive's name on a Mac.
_FILELIST_RE = re.compile(r"^(v[0-9a-f]+)_(.+?)_+filelist\.dat$", re.IGNORECASE)
_PLATFORM: dict[str, str] = {}   # bzdata root -> "windows" | "mac" (only once the data says so)


@dataclass
class Volume:
    key: str                    # 'C' on Windows; the file list's own name on a Mac ('root', 'Seagate')
    guid: str                   # 'v0005...'
    mount: str                  # 'C:\\', '/', '/Volumes/Seagate HD'
    label: str = ""             # shown in the UI: 'C:' or the mount path
    filelist: str = ""          # absolute path to the per-volume filelist (.dat), or ""
    free: int = 0
    used: int = 0
    total: int = 0

    @property
    def root(self) -> str:
        """The mount with its trailing separator: every path on this volume starts with it."""
        sep = POSIX_SEP if self.mount.startswith(POSIX_SEP) else WIN_SEP
        return self.mount if self.mount.endswith(sep) else self.mount + sep


def _decode_mountpoint_hex(h: str) -> str:
    """'433a5c' -> 'C:\\' (hex pairs -> bytes; Mac drive names can be UTF-8)."""
    try:
        return bytes.fromhex(h).decode("utf-8", "replace")
    except Exception:
        return ""


def _parse_bzvolumes() -> dict[str, dict]:
    """guid -> {mount, free, used, total} from bzvolumes.xml (regex; tolerant of formatting)."""
    out: dict[str, dict] = {}
    path = bzvolumes_path()
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except Exception:
        return out
    for m in re.finditer(
        r'bzVolumeGuid="(v[0-9a-f]+)"\s+mountPointPathHex="([0-9a-fA-F]*)"[^>]*?'
        r'numBytesFreeOnVolume="(\d+)"\s+numBytesUsedOnVolume="(\d+)"\s+numBytesTotalOnVolume="(\d+)"',
        txt,
    ):
        guid, hexmp, free, used, total = m.groups()
        out[guid] = {
            "mount": _decode_mountpoint_hex(hexmp),
            "free": int(free), "used": int(used), "total": int(total),
        }
    return out


def _filelists() -> list[tuple[str, str, str]]:
    """[(path, guid, token)] for every committed per-volume file list (never the .future scans)."""
    out = []
    for fp in sorted(glob.glob(os.path.join(filelists_dir(), "v*_*filelist.dat"))):
        m = _FILELIST_RE.match(os.path.basename(fp))
        if m:
            out.append((fp, m.group(1), m.group(2)))
    return out


def forget_platform(path: str | None = None) -> None:
    """Drop the remembered platform for one root (a switched folder), or for every root."""
    if path is None:
        _PLATFORM.clear()
    else:
        _PLATFORM.pop(path, None)


def platform() -> str:
    """"mac" or "windows" for the data being read. A Mac's file lists are named "root" or by
    drive name and its mount points start with "/"; Windows' are single drive letters."""
    root = _bzdata_root
    hit = _PLATFORM.get(root)
    if hit:
        return hit
    tokens = [t for _fp, _g, t in _filelists()]
    mounts = [v.get("mount") or "" for v in _parse_bzvolumes().values()]
    if any(len(t) != 1 for t in tokens) or any(m.startswith(POSIX_SEP) for m in mounts):
        plat = "mac"
    elif tokens or mounts:
        plat = "windows"
    else:
        return "windows"   # nothing to go on yet: not remembered, the folder may fill in later
    _PLATFORM[root] = plat
    return plat


def sep() -> str:
    return POSIX_SEP if platform() == "mac" else WIN_SEP


def _safe_key(token: str) -> str:
    """A Mac drive's key: its file list token, safe for file names (per-drive index files)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", token).strip("_") or "volume"


def volumes() -> list[Volume]:
    """All backed volumes, joined from the filelist filenames (authoritative for what's scanned)
    + bzvolumes.xml metadata (matched by guid). Sorted by drive letter / mount."""
    mac = platform() == "mac"
    meta = _parse_bzvolumes()
    by_letter = {v["mount"][:1].upper(): v for v in meta.values() if v.get("mount")}
    vols: dict[str, Volume] = {}
    for fp, guid, token in _filelists():
        info = meta.get(guid)
        if mac:
            key = base = _safe_key(token)
            n = 2
            while key in vols:
                key = f"{base}-{n}"
                n += 1
            mount = (info or {}).get("mount") or ("/" if token.lower() == "root" else "/Volumes/" + token)
            mount = mount.rstrip(POSIX_SEP) or POSIX_SEP
            label = mount
        else:
            key = token.upper()
            info = info or by_letter.get(key)
            mount = (info or {}).get("mount") or f"{key}:\\"
            label = f"{key}:"
        v = Volume(key=key, guid=guid, mount=mount, label=label, filelist=fp)
        if info:
            v.free, v.used, v.total = info["free"], info["used"], info["total"]
        vols[key] = v
    return sorted(vols.values(), key=(lambda v: v.mount.lower()) if mac else (lambda v: v.key))


def drive_letters() -> list[str]:
    return [v.key for v in volumes()]


def volume_info() -> list[dict]:
    """What the UI needs per volume: its key (the `drive` value in every row), label and root."""
    return [{"drive": v.key, "label": v.label, "root": v.root} for v in volumes()]


def volume_key_for(path: str, vols: list[Volume] | None = None) -> str:
    """The volume a Backblaze path is on: its drive letter on Windows, the longest matching
    mount on a Mac ("" when none matches)."""
    if platform() != "mac":
        return path[0].upper() if len(path) > 1 and path[1] == ":" else ""
    pl = path.lower()
    best, blen = "", -1
    for v in (vols if vols is not None else volumes()):
        r = v.root.lower()
        if (pl.startswith(r) or pl == r.rstrip(POSIX_SEP)) and len(r) > blen:
            best, blen = v.key, len(r)
    return best


# ---- which computer the data belongs to -------------------------------------------------
_NAME_CACHE: dict[str, tuple] = {}


def computer_name() -> str:
    """The Backblaze computer's name from bzinfo.xml (`online_hostname`, the name Backblaze shows
    online, e.g. "my-pc_2025_09_19"); "" when the file or the field is missing."""
    path = bzinfo_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = _NAME_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    name = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            m = re.search(r'\bonline_hostname="([^"]*)"', fh.read())
        name = (m.group(1) if m else "").strip()
    except OSError:
        pass
    _NAME_CACHE[path] = (mtime, name)
    return name


def computer_display(name: str | None = None) -> str:
    """The computer's name without the date Backblaze adds when it is set up again
    ("my-pc_2025_09_19" -> "my-pc")."""
    name = computer_name() if name is None else name
    return re.sub(r"_\d{4}_\d{2}_\d{2}$", "", name.strip())


def computer_key(name: str | None = None) -> str:
    """The computer's identity: `computer_display` lowercased (the same computer keeps one key)."""
    return computer_display(name).lower()


_SCAN_CACHE: dict[str, tuple] = {}


def newest_scan_ts() -> float | None:
    """Backblaze's own newest scan start (epoch), from the committed file lists' header line.
    None when no list carries one. Backblaze rewrites this header on every scan pass."""
    best = None
    for fp, _guid, _token in _filelists():
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            continue
        hit = _SCAN_CACHE.get(fp)
        if hit and hit[0] == mtime:
            ts = hit[1]
        else:
            ts = None
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    m = re.search(r"#\s*GmtMillisThisListWasStarted\s+(\d+)", fh.readline())
                if m:
                    ts = int(m.group(1)) / 1000.0
            except OSError:
                pass
            _SCAN_CACHE[fp] = (mtime, ts)
        if ts and (best is None or ts > best):
            best = ts
    return best


def _short(name: str) -> str:
    return name.split(".")[0].strip().lower()


def _same_root(a: str, b: str | None) -> bool:
    return bool(b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def is_this_computer(settings: dict | None = None) -> bool:
    """Whether the data is this computer's own Backblaze. Setting `backblaze.computer`: "this",
    "other", or "auto": this computer's own install folder always is; elsewhere (a shared folder)
    Backblaze's name for the computer must be this computer's name, or start with it plus "_"
    (Backblaze adds a date when a computer is set up again). No name to compare counts as this
    computer, as before."""
    if settings is None:
        try:
            from ..settings import load_settings
            settings = load_settings()
        except Exception:  # noqa: BLE001
            settings = {}
    mode = str((settings.get("backblaze") or {}).get("computer") or "auto")
    if mode == "this":
        return True
    if mode == "other":
        return False
    if DEMO_BZDATA is not None:
        return True   # a demo's own bzdata stands for this computer unless "Another computer" is picked
    if _same_root(_bzdata_root, default_bzdata()):
        return True
    name = computer_name().strip().lower()
    if not name:
        return True
    here = {_short(socket.gethostname()), _short(os.environ.get("COMPUTERNAME") or "")} - {""}
    return any(name == h or name.startswith(h + "_") for h in here)
