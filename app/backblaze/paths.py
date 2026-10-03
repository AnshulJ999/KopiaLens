"""Locate the Backblaze bzdata files and build the drive<->volume map dynamically.

Do NOT hardcode the GUID->letter map (bbtoolkit did, via GUID2LETTER) - it breaks if a
drive/GUID changes. We derive it from `bzvolumes.xml` (decode mountPointPathHex) and the
on-disk per-volume filelist filenames (`v{guid}_{letter}____filelist.dat`).
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

# Default install location. Overridable by callers (e.g. a settings value) via set_bzdata_root.
DEFAULT_BZDATA = r"C:\ProgramData\Backblaze\bzdata"
try:
    from config import DATA_DIR, DEMO_INSTANCE
except ImportError:   # standalone tooling outside the app
    DATA_DIR, DEMO_INSTANCE = "", False
# A demo data folder only ever reads its own generated bzdata, never this machine's.
DEMO_BZDATA = os.path.join(DATA_DIR, "bzdata") if DEMO_INSTANCE else None
_bzdata_root = DEMO_BZDATA or DEFAULT_BZDATA


def set_bzdata_root(path: str) -> None:
    global _bzdata_root
    if DEMO_BZDATA is None:
        _bzdata_root = path


def bzdata_root() -> str:
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


# ---- drive / volume map ----------------------------------------------------------------
_FILELIST_RE = re.compile(r"^(v[0-9a-f]+)_([a-z])_+filelist\.dat$", re.IGNORECASE)


@dataclass
class Volume:
    letter: str                 # 'C'..'I'
    guid: str                   # 'v0005...'
    mount: str                  # 'C:\\'
    filelist: str = ""          # absolute path to the per-volume filelist (.dat), or ""
    free: int = 0
    used: int = 0
    total: int = 0


def _decode_mountpoint_hex(h: str) -> str:
    """'433a5c' -> 'C:\\' (hex pairs -> ascii bytes)."""
    try:
        return bytes.fromhex(h).decode("ascii", "replace")
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


def volumes() -> list[Volume]:
    """All backed volumes, joined from the filelist filenames (authoritative for what's scanned)
    + bzvolumes.xml metadata. Sorted by drive letter."""
    vols: dict[str, Volume] = {}
    # 1) on-disk filelists -> letter + guid (the things we actually index)
    for fp in glob.glob(os.path.join(filelists_dir(), "v*_*____filelist.dat")):
        m = _FILELIST_RE.match(os.path.basename(fp))
        if not m:
            continue
        guid, letter = m.group(1), m.group(2).upper()
        vols[letter] = Volume(letter=letter, guid=guid, mount=f"{letter}:\\", filelist=fp)
    # 2) enrich with bzvolumes.xml (free/used/total + canonical mount)
    meta = _parse_bzvolumes()
    by_guid = {v["mount"][:1].upper(): v for g, v in meta.items() if v.get("mount")}
    for letter, v in vols.items():
        info = by_guid.get(letter)
        if info:
            v.free, v.used, v.total = info["free"], info["used"], info["total"]
            if info.get("mount"):
                v.mount = info["mount"]
    return [vols[k] for k in sorted(vols)]


def drive_letters() -> list[str]:
    return [v.letter for v in volumes()]
