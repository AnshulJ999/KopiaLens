"""Fake Backblaze data for a demo folder, read by the real Backblaze indexer.

``write`` lays out one bzdata folder (Windows, or a Mac's when ``platform="mac"``) shaped like
a real Backblaze setup: per-drive counts and sizes, where the excluded files sit, the file-type
mix and the upload rhythm, each number moved by about 10-20%. Numbers come from the real data;
names never do. Everyday files are the demo's own already-faked Kopia files plus word-list files;
excluded areas have made-up folder names.

Every displayed number comes from these files: the report XMLs are counted from the same
classification the indexer uses, so per-drive Diff is +0 except for new files still in the upload
queue, the type categories add up to the backed total, and the file-id history is several times
the live file count the way Backblaze's own is.

``index`` runs the real indexer. ``trim`` drops what only indexing reads.
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import random
import re

from . import fakenames
from .applog import get_logger

log = get_logger("demobz")

BZ_RECORD = {"id": "bz", "name": "Backblaze", "short_name": "Backblaze", "kind": "backblaze"}
_DAY = 86400.0
_BS = chr(92)
# Backblaze's default excluded file types.
_EXCLUDED_TYPES = "wab~,vmc,vhd,vhdx,vdi,vo1,vo2,vsv,vud,sparseimage,qtch,ithmb,vmdk,vmem,vmsd,vmsn,vmss,vmx,vmxf"
_CATEGORY = {  # filestats.xml buckets
    "docs": {"doc", "docx", "pdf", "txt", "md", "odt", "rtf", "epub"},
    "spreadsheets": {"xls", "xlsx", "csv", "ods"},
    "presentations": {"ppt", "pptx", "odp"},
    "pict": {"jpg", "jpeg", "png", "gif", "heic", "dng", "svg", "webp", "bmp", "tif"},
    "music": {"mp3", "wav", "flac", "ogg", "m4a", "aac", "mid"},
    "video": {"mp4", "mkv", "mov", "avi", "webm"},
    "zips": {"zip", "7z", "rar", "gz", "tar", "msi", "exe"},
    "email": {"eml", "msg"},
    "browserfavorites": {"url"},
}

# A real setup's shape (queries.overview on the live bzdata): per drive, backed files,
# excluded files, backed bytes, excluded bytes. The generator moves each of these by +-15%.
_WINDOWS_REAL = {
    "C": {"backed": 62402, "excluded": 1820687, "backed_bytes": 112_717_561_051,
          "excluded_bytes": 1_132_500_000_000},
    "D": {"backed": 590, "excluded": 23090, "backed_bytes": 32_700_000_000,
          "excluded_bytes": 458_500_000_000},
    "E": {"backed": 257504, "excluded": 348316, "backed_bytes": 2_764_000_000_000,
          "excluded_bytes": 686_300_000_000},
    "F": {"backed": 482588, "excluded": 216010, "backed_bytes": 2_960_000_000_000,
          "excluded_bytes": 681_300_000_000},
    "G": {"backed": 123595, "excluded": 920945, "backed_bytes": 596_000_000_000,
          "excluded_bytes": 1_074_900_000_000},
    "H": {"backed": 26013, "excluded": 438131, "backed_bytes": 96_000_000_000,
          "excluded_bytes": 603_300_000_000},
    "I": {"backed": 177539, "excluded": 169848, "backed_bytes": 4_264_000_000_000,
          "excluded_bytes": 474_000_000_000},
}

# A Mac with two external drives: believable on its own, and unlike the Windows shape on purpose
# (it stands for another computer, and the folder switch shows that feature).
_MAC_REAL = {
    "root": {"backed": 420_000, "excluded": 1_180_000, "backed_bytes": 2_200_000_000_000,
             "excluded_bytes": 1_100_000_000_000},
    "Archive": {"backed": 380_000, "excluded": 20_000, "backed_bytes": 5_000_000_000_000,
                "excluded_bytes": 200_000_000_000},
    "Photos": {"backed": 218_000, "excluded": 2_000, "backed_bytes": 1_800_000_000_000,
               "excluded_bytes": 10_000_000_000},
}

_SAMPLE = {
    "C": {"backed": 22_000, "excluded": 18_000, "backed_bytes": 40_000_000_000,
          "excluded_bytes": 30_000_000_000},
    "D": {"backed": 2_000, "excluded": 1_000, "backed_bytes": 20_000_000_000,
          "excluded_bytes": 4_000_000_000},
    "E": {"backed": 8_000, "excluded": 2_000, "backed_bytes": 60_000_000_000,
          "excluded_bytes": 6_000_000_000},
}

_TEST_WINDOWS = {
    "C": {"backed": 90, "excluded": 55, "backed_bytes": 12_000_000, "excluded_bytes": 6_000_000},
    "E": {"backed": 40, "excluded": 12, "backed_bytes": 80_000_000, "excluded_bytes": 4_000_000},
}
_TEST_MAC = {
    "root": {"backed": 70, "excluded": 35, "backed_bytes": 9_000_000, "excluded_bytes": 5_000_000},
    "Archive": {"backed": 25, "excluded": 4, "backed_bytes": 60_000_000, "excluded_bytes": 1_000_000},
}

# The real file-type mix (filestats.xml, 2026-11-10): bucket -> (files, bytes).
_REAL_TYPES = {
    "video": (60_714, 1_820_000_000_000), "music": (97_187, 515_500_000_000),
    "pict": (243_611, 218_000_000_000), "docs": (36_008, 38_500_000_000),
    "spreadsheets": (1_074, 740_000_000), "presentations": (979, 4_360_000_000),
    "email": (21, 350_000), "zips": (4_236, 393_000_000_000), "browserfavorites": (602, 110_000),
    "other": (685_867, 7_840_000_000_000),
}
_BUCKET_FILES = {   # bucket -> (fakenames template group, extensions)
    "video": ("Videos", ["mp4", "mp4", "mkv", "mov"]),
    "music": ("Music", ["mp3", "mp3", "flac", "m4a", "wav"]),
    "pict": ("Photos", ["jpg", "jpg", "jpg", "png", "heic", "jpeg"]),
    "docs": ("Documents", ["pdf", "pdf", "docx", "txt", "epub"]),
    "spreadsheets": ("Documents", ["xlsx", "csv"]),
    "presentations": ("Documents", ["pptx"]),
    "zips": ("Archives", ["zip", "zip", "7z", "rar"]),
    "email": ("Documents", ["eml"]),
    "browserfavorites": ("Mixed", ["url"]),
    "other": ("Code", ["cr2", "nef", "arw", "psd", "json", "py", "db", "dat", "xml", "blend", "iso", "bak"]),
}
_BUCKET_FOLDERS = {"video": "Videos", "music": "Music", "pict": "Photos", "docs": "Documents",
                   "spreadsheets": "Documents", "presentations": "Documents", "zips": "Archives",
                   "email": "Documents", "browserfavorites": "Mixed", "other": "Code"}

# The real upload rhythm (bz_done, the 45 days before 2026-11-10): (days before, files, bytes).
_REAL_UPLOADS = [(1, 2607, 9_901_688_793), (4, 1891, 7_962_714_999), (5, 3044, 12_696_319_699),
                 (6, 5174, 9_120_857_907), (11, 2828, 11_934_155_782), (14, 6889, 23_905_170_984),
                 (20, 3601, 15_167_507_526), (22, 959, 4_026_132_853), (24, 2205, 8_564_480_209),
                 (25, 937, 3_770_118_807), (27, 923, 3_552_960_130), (28, 2994, 11_225_458_721),
                 (30, 1530, 5_920_064_316), (31, 1525, 6_317_632_501), (32, 8550, 28_184_994_951),
                 (37, 3032, 12_002_713_105), (40, 6203, 22_064_916_864)]

# Where a drive's excluded files sit: (area, share of the drive's excluded files, share of its
# excluded bytes). Shaped from the real drive tops; every name is made up.
_WIN_AREAS = {
    "C": [("appdata", 0.915, 0.36), ("games", 0.046, 0.46), ("programdata", 0.032, 0.06),
          ("vendor", 0.007, 0.01)],
    "D": [("games", 0.68, 0.70), ("repacks", 0.16, 0.06), ("parked", 0.16, 0.24)],
    "E": [("phone", 0.50, 0.32), ("export", 0.24, 0.16), ("old_media", 0.13, 0.33),
          ("tablet", 0.10, 0.17), ("parked", 0.03, 0.02)],
    "F": [("laptop", 0.90, 0.09), ("repacks", 0.065, 0.06), ("movies", 0.02, 0.70), ("tv", 0.015, 0.15)],
    "G": [("code", 0.58, 0.10), ("emulator", 0.20, 0.04), ("ai_models", 0.10, 0.03),
          ("games", 0.07, 0.80), ("docker", 0.05, 0.03)],
    "H": [("media_server", 0.66, 0.21), ("photo_server", 0.14, 0.02), ("experiments", 0.09, 0.08),
          ("emulator", 0.07, 0.01), ("games", 0.04, 0.68)],
    "I": [("system_image", 0.87, 0.17), ("vms", 0.003, 0.55), ("parked", 0.06, 0.16),
          ("games", 0.054, 0.11), ("temp", 0.013, 0.01)],
}
_MAC_AREAS = {
    "root": [("mac_library", 0.62, 0.30), ("mac_apps", 0.18, 0.40), ("mac_private", 0.12, 0.05),
             ("mac_syslib", 0.06, 0.10), ("mac_system", 0.02, 0.05)],
    "Archive": [("mac_exports", 0.45, 0.80), ("mac_spotlight", 0.30, 0.10), ("mac_events", 0.25, 0.10)],
    "Photos": [("mac_derivatives", 1.0, 1.0)],
}
_GAMES = ["Starfall Drift", "Iron Harbor", "Neon Tides", "Echo Ridge", "Skyward Forge", "Crimson Outpost",
          "Hollow Peaks", "Atlas Frontier", "Silent Meridian", "Frostline Rally", "Ember Keep",
          "Tidal Run", "Quantum Siege", "Velvet Circuit", "Last Lantern", "Copper Canyon", "Wild Orbit"]
_VENDORS = ["NVIDIA Corporation", "Package Cache", "Razer", "Adobe", "Intel", "Logitech", "Oracle",
            "Epic", "Microsoft" + _BS + "Windows" + _BS + "WER", "Microsoft" + _BS + "Search" + _BS + "Data",
            "Docker", "Corsair"]
_APPS = [("Local", "Google" + _BS + "Chrome" + _BS + "User Data" + _BS + "Default",
          ["Cache" + _BS + "Cache_Data", "Code Cache" + _BS + "js", "Service Worker" + _BS + "CacheStorage", "GPUCache", "IndexedDB"]),
         ("Local", "Microsoft" + _BS + "Edge" + _BS + "User Data" + _BS + "Default",
          ["Cache" + _BS + "Cache_Data", "Code Cache" + _BS + "js", "Service Worker" + _BS + "CacheStorage"]),
         ("Roaming", "Mozilla" + _BS + "Firefox" + _BS + "Profiles" + _BS + "k3x9.default-release",
          ["storage" + _BS + "default", "cache2" + _BS + "entries"]),
         ("Roaming", "discord", ["Cache" + _BS + "Cache_Data", "Code Cache" + _BS + "js"]),
         ("Local", "Spotify", ["Data", "Storage"]), ("Local", "NVIDIA", ["DXCache", "GLCache"]),
         ("Local", "JetBrains" + _BS + "PyCharm2024.2", ["caches", "index"]),
         ("Local", "npm-cache", ["_cacache" + _BS + "content-v2" + _BS + "sha512"]),
         ("Local", "pip", ["cache" + _BS + "http-v2"]), ("Local", "Temp", ["{h8}"]),
         ("Local", "Packages", ["Microsoft.Windows.Photos_8wekyb3d8bbwe" + _BS + "LocalState"]),
         ("Roaming", "Code", ["CachedData", "User" + _BS + "workspaceStorage"]),
         ("LocalLow", "Unity", ["cache"]), ("Local", "Steam", ["htmlcache" + _BS + "Cache"])]

# Folder rules that match nothing today: a real exclusion list carries plenty of these.
_WIN_SPARE_RULES = [
    "downloads" + _BS + "incomplete", "steamlibrary", "obs recordings" + _BS + "raw", "$windows.~bt",
    "$windows.~ws", "windows.old", "msocache", "intel", "perflogs", "recovery", "swsetup", "drivers",
    "esd", "onedrivetemp", "xboxgames", "riot games", "battle.net", "ea games", "gog games",
    "ubisoft" + _BS + "cache", "origin games", "emulators" + _BS + "shaders", "emulators" + _BS + "cache",
    "vm images", "virtual machines", "hyper-v", "wsl", "android" + _BS + "sdk", "android" + _BS + "avd",
    "gradle" + _BS + "caches", "unity" + _BS + "cache", "unreal projects" + _BS + "intermediate",
    "render cache", "proxy media", "premiere cache", "davinci cache", "lightroom previews",
    "capture one cache", "photo previews", "thumbnails", "torrents" + _BS + "incomplete", "scratch",
    "tmp", "temp downloads", "old installers", "iso images", "disk images", "backups" + _BS + "old",
    "phone backup" + _BS + "whatsapp" + _BS + "databases", "phone backup" + _BS + "thumbnails",
    "media server" + _BS + "transcodes", "media server" + _BS + "metadata", "photo server" + _BS + "thumbs",
    "code" + _BS + "node_modules", "code" + _BS + "venv", "code" + _BS + "build", "ai models" + _BS + "cache",
    "docker" + _BS + "volumes", "minecraft servers", "game saves" + _BS + "cloud", "screen recordings" + _BS + "raw",
    "camera imports" + _BS + "duplicates"]
_MAC_SPARE_RULES = ["Downloads/Incomplete/", "Movies/Final Cut Backups/", "Music/Logic/Bounces Cache/",
                    "Library/Developer/", "Library/Mobile Documents/", "Parallels/", "VirtualBox VMs/",
                    "Pictures/Lightroom Previews/", "Projects/node_modules/", "Projects/venv/",
                    "Movies/Proxy Media/", "Desktop/Scratch/", "Documents/Temp/"]

# Dashboard "Recently added": the folders a viewer sees first, with their recent file-id counts
# (each moved a few percent). Windows: the list the demo showed before 2026-11-11; Mac: hand-picked,
# about 40% on the external drives.
_WIN_RECENT = [("G:" + _BS + "Downloads" + _BS + "Trip 2019", 84485),
               ("G:" + _BS + "GitHub" + _BS + "photo-sorter", 19118), ("C:" + _BS + "Users" + _BS + "{user}", 17390),
               ("G:" + _BS + "GitHub" + _BS + "home-lab", 13475), ("G:" + _BS + "Project Restore" + _BS + "Scripts", 13076),
               ("G:" + _BS + "GitHub" + _BS + "task-runner", 11766), ("G:" + _BS + "GitHub" + _BS + "awesome-scripts", 6219),
               ("G:" + _BS + "GitHub" + _BS + "home-config", 5557),
               ("G:" + _BS + "GitHub" + _BS + "photo-sorter Combined", 5372),
               ("G:" + _BS + "GitHub" + _BS + "Solace-Synth", 5050)]
_MAC_RECENT = [("/Users/{user}", 61200), ("/Volumes/Photos/Iceland 2024/Raw", 24800),
               ("/Volumes/Archive/Projects/photo-sorter", 16300), ("/Volumes/Photos/Camera Roll/2025", 13900),
               ("/Volumes/Archive/Client Work/2025", 11200), ("/Volumes/Archive/Music Production/Stems", 9400),
               ("/Volumes/Photos/Lisbon 2023/Edited", 7300), ("/Volumes/Archive/Exports/Video 2024", 6100),
               ("/Volumes/Archive/Scans/Family Albums", 5400), ("/Volumes/Photos/Drone/2025", 4900)]


def _stamp(t: float) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%Y%m%d%H%M%S")


def _gmt(t: float) -> str:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y%m%d%H%M%S")


def _gmt_day(t: float) -> str:
    return _gmt(t)[:8]


def _hex(s: str) -> str:
    return s.encode("utf-8").hex()


def _moved(value: int, r: random.Random) -> int:
    return max(1, int(value * r.uniform(0.85, 1.15)))


def _targets(profile, platform: str) -> dict:
    if isinstance(profile, dict):
        return profile
    if profile == "test":
        return dict(_TEST_MAC if platform == "mac" else _TEST_WINDOWS)
    if profile == "sample":
        return dict(_SAMPLE)
    return dict(_MAC_REAL if platform == "mac" else _WINDOWS_REAL)


def _lognormal_sizes(count: int, total: int, sigma: float, r: random.Random) -> list[int]:
    """`count` varied sizes adding up to about `total` (heavy-tailed, the way real files are)."""
    if count <= 0:
        return []
    mean = max(1.0, total / count)
    mu = math.log(mean) - sigma * sigma / 2
    out = [r.lognormvariate(mu, sigma) for _ in range(count)]
    scale = total / max(1.0, sum(out))
    return [max(1, int(x * scale)) for x in out]


def _bucket_of(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for cat, exts in _CATEGORY.items():
        if ext in exts:
            return cat
    return "other"


def _leaf(path: str) -> str:
    return path.rsplit(_BS, 1)[-1] if _BS in path else path.rsplit("/", 1)[-1]


# ---- excluded areas ------------------------------------------------------------------------

def _area_spec(kind: str, user: str, sep: str) -> dict:
    """An excluded area: its top folder parts, sub-folder shapes, file names and size spread."""
    games = {"top": ["Games"],
             "branches": [("{game}" + sep + "Content" + sep + "Paks", ["pakchunk{n}-Windows.pak", "pakchunk{n}optional-Windows.pak"], True),
                          ("{game}" + sep + "Data" + sep + "StreamingAssets", ["sharedassets{n}.assets", "level{n}.assets", "audio_{n}.bank"], True),
                          ("{game}" + sep + "Movies", ["intro_{n}.bk2", "cutscene_{n}.bk2"], True),
                          ("{game}" + sep + "Binaries" + sep + "Win64", ["{word}{n}.dll", "{word}{n}.exe"], False),
                          ("{game}" + sep + "Shaders", ["shader_{h6}.cache"], False)],
             "cap": (8, 220), "sigma": 2.2}
    specs = {
        "appdata": {"top": ["Users", user, "AppData"], "app": True,
                    "names": ["f_{h6}", "data_{n}", "{h16}_0", "{h16}", "{h8}.tmp", "{h8}.ldb"],
                    "cap": (200, 2400), "sigma": 1.6},
        "games": games,
        "programdata": {"top": ["ProgramData"], "branch": ["{vendor}" + sep + "{h4}", "{vendor}" + sep + "Logs",
                                                           "{vendor}" + sep + "Cache" + sep + "{h2}"],
                        "names": ["{h8}.dat", "log_{n}.txt", "{h8}.bin", "setup_{n}.msi", "{h8}.db"],
                        "cap": (60, 900), "sigma": 2.0},
        "vendor": {"top": ["RGB Sync"], "branch": ["Profiles", "Firmware", "Logs"],
                   "names": ["profile_{n}.json", "fw_{h6}.bin", "log_{n}.txt"], "cap": (40, 400), "sigma": 1.4},
        "repacks": {"top": ["Repacks"], "branch": ["{game}", "{game}" + sep + "Setup"],
                    "names": ["setup-{n}.bin", "data-{n}.bin", "fg-{n}.bin", "setup-{n}.exe"], "cap": (6, 60), "sigma": 1.2},
        "parked": {"top": ["Excluded from Backblaze"], "branch": ["{word}", "{word}" + sep + "{word2}"],
                   "names": ["{word}-{n}.bin", "{h8}.dat", "export-{n}.zip", "capture-{n}.mkv"], "cap": (20, 600), "sigma": 2.4},
        "phone": {"top": ["Phone Backup", "Internal", "Android", "media"],
                  "branch": ["com.chat.app" + sep + "Media" + sep + "Images", "com.chat.app" + sep + "Media" + sep + "Video",
                             "com.chat.app" + sep + "Media" + sep + "Voice Notes" + sep + "{n}", "com.social.app" + sep + "cache"],
                  "names": ["IMG-{date}-WA{n4}.jpg", "VID-{date}-WA{n4}.mp4", "PTT-{date}-WA{n4}.opus", "{h16}.0"],
                  "cap": (300, 2500), "sigma": 1.5},
        "export": {"top": ["Cloud Export"], "branch": ["Takeout-{n}" + sep + "Photos from {year}", "Takeout-{n}" + sep + "Drive",
                                                      "Takeout-{n}" + sep + "Mail"],
                   "names": ["IMG_{n4}.jpg", "IMG_{n4}.jpg.json", "doc-{n}.pdf", "mbox-{n}.mbox"], "cap": (200, 2400), "sigma": 1.6},
        "old_media": {"top": ["Personal", "Phone Media Old"], "branch": ["{year}", "{year}" + sep + "{month}"],
                      "names": ["IMG_{date}_{n4}.jpg", "VID_{date}_{n4}.mp4"], "cap": (100, 1200), "sigma": 1.9},
        "tablet": {"top": ["Tablet Mirror", "$sbv$"], "branch": ["{h8}", "{h8}" + sep + "{h4}"],
                   "names": ["{h16}", "{h8}.blob"], "cap": (100, 1500), "sigma": 1.7},
        "laptop": {"top": ["Old Laptop Backup"], "branch": ["Windows" + sep + "System32" + sep + "{word}",
                                                             "Program Files" + sep + "{vendor}", "Users" + sep + "Laptop" + sep + "AppData" + sep + "{h4}"],
                   "names": ["{h8}.dll", "{word}{n}.exe", "{h8}.mui", "{h8}.dat"], "cap": (300, 2400), "sigma": 1.3},
        "movies": {"top": ["Movies"], "branch": ["{title} ({year})"],
                   "names": ["{title} ({year}).mkv", "{title} ({year}).srt"], "cap": (1, 3), "sigma": 0.9},
        "tv": {"top": ["TV Shows"], "branch": ["{title}" + sep + "Season {n2}"],
               "names": ["{title} - S{n2}E{n2}.mkv"], "cap": (6, 14), "sigma": 0.7},
        "code": {"top": ["Code"], "branch": ["{project}" + sep + "node_modules" + sep + "{pkg}", "{project}" + sep + ".git" + sep + "objects" + sep + "{h2}",
                                             "{project}" + sep + ".venv" + sep + "Lib" + sep + "site-packages" + sep + "{pkg}"],
                 "names": ["{h8}.js", "{pkg}-{h4}.js", "{h16}", "{h8}.py", "{h4}.d.ts", "{h8}.pyc"],
                 "cap": (40, 900), "sigma": 1.5},
        "emulator": {"top": ["Emulators", "{console}"], "branch": ["shaders" + sep + "{h8}", "textures" + sep + "{h8}", "cache"],
                     "names": ["{h16}.png", "{h8}.bin", "shader_{n}.cache"], "cap": (200, 2000), "sigma": 1.4},
        "ai_models": {"top": ["AI Models"], "branch": ["checkpoints", "loras", "cache" + sep + "{h4}", "outputs" + sep + "temp"],
                      "names": ["model-{n}.safetensors", "lora-{word}-{n}.safetensors", "{h16}.bin", "tmp_{n}.png"],
                      "cap": (20, 1500), "sigma": 2.6},
        "docker": {"top": ["Docker"], "branch": ["volumes" + sep + "{h8}" + sep + "_data", "wsl" + sep + "data"],
                   "names": ["{h8}.log", "{h16}", "data-{n}.db"], "cap": (100, 1500), "sigma": 2.2},
        "media_server": {"top": ["Media Server"], "branch": ["metadata" + sep + "{h2}" + sep + "{h8}", "cache" + sep + "transcodes",
                                                             "generated" + sep + "{h2}"],
                         "names": ["poster-{h4}.jpg", "fanart-{h4}.jpg", "{h16}.webp", "{h8}.ts", "{h16}.jpg"],
                         "cap": (200, 2400), "sigma": 1.7},
        "photo_server": {"top": ["Photo Server"], "branch": ["thumbs" + sep + "{h8}" + sep + "{h2}", "encoded-video" + sep + "{h8}"],
                         "names": ["{h16}-thumbnail.webp", "{h16}-preview.jpeg", "{h16}.mp4"], "cap": (200, 2400), "sigma": 1.5},
        "experiments": {"top": ["Experiments"], "branch": ["{word}-{n}", "{word}-{n}" + sep + "data"],
                        "names": ["run-{n}.log", "{h8}.parquet", "scratch-{n}.db", "{h8}.tmp"], "cap": (100, 1800), "sigma": 2.2},
        "system_image": {"top": ["System Image"], "branch": ["Windows" + sep + "WinSxS" + sep + "{h8}", "Program Files" + sep + "{vendor}",
                                                             "Users" + sep + "Old" + sep + "AppData" + sep + "{h4}"],
                         "names": ["{h8}.dll", "{h8}.cat", "{word}{n}.exe", "{h8}.mum"], "cap": (300, 2400), "sigma": 1.4},
        "vms": {"top": ["VM Images"], "branch": ["{word} VM"],
                "names": ["{word}-disk{n}.vhdx", "{word}.vmdk"], "cap": (1, 3), "sigma": 0.5},
        "temp": {"top": ["Temp"], "branch": ["{h8}"], "names": ["{h8}.tmp", "tmp{n}.dat"], "cap": (20, 300), "sigma": 1.0},
        # Mac
        "mac_library": {"top": ["Users", user, "Library"], "branch": ["Caches" + sep + "com.{brand}.{word}", "Caches" + sep + "com.{brand}.{word}" + sep + "{h8}",
                                                                       "Application Support" + sep + "{brand}" + sep + "Cache",
                                                                       "Containers" + sep + "com.{brand}.{word}" + sep + "Data" + sep + "Library" + sep + "Caches"],
                        "names": ["Cache-{n}.db", "{h16}", "data_{n}", "{h8}.cache", "fsCachedData_{h8}"],
                        "cap": (200, 2400), "sigma": 1.6},
        "mac_apps": {"top": ["Applications"], "branch": ["{app}.app" + sep + "Contents" + sep + "Frameworks",
                                                         "{app}.app" + sep + "Contents" + sep + "Resources" + sep + "{h4}",
                                                         "{app}.app" + sep + "Contents" + sep + "MacOS"],
                     "names": ["{word}{n}.dylib", "{word}{n}.nib", "{h8}.png", "{word}{n}.car", "{h8}"], "cap": (40, 900), "sigma": 1.9},
        "mac_private": {"top": ["private", "var", "folders"], "branch": ["{h2}" + sep + "{h16}" + sep + "C" + sep + "com.{brand}.{word}",
                                                                          "{h2}" + sep + "{h16}" + sep + "T"],
                        "names": ["{h8}.tmp", "data_{n}", "{h16}"], "cap": (100, 1500), "sigma": 1.5},
        "mac_syslib": {"top": ["Library"], "branch": ["Application Support" + sep + "{brand}", "Caches" + sep + "com.{brand}.{word}"],
                       "names": ["{h8}.db", "{word}{n}.plist", "{h16}"], "cap": (60, 900), "sigma": 1.8},
        "mac_system": {"top": ["System", "Library"], "branch": ["Frameworks" + sep + "{app}.framework", "PrivateFrameworks" + sep + "{app}.framework"],
                       "names": ["{word}{n}", "{word}{n}.plist", "{h8}.dylib"], "cap": (40, 600), "sigma": 1.6},
        "mac_exports": {"top": ["Old Exports"], "branch": ["{year}" + sep + "{word}", "Renders" + sep + "{word}"],
                        "names": ["render-{n}.mov", "export-{n}.mp4", "{word}-{n}.prores.mov"], "cap": (10, 300), "sigma": 1.6},
        "mac_spotlight": {"top": [".Spotlight-V100", "Store-V2", "{uuid}"], "branch": ["{h8}"],
                          "names": ["store-{n}.db", "{h8}.indexHead", "live.{n}.indexPostings"], "cap": (100, 1500), "sigma": 1.4},
        "mac_events": {"top": [".fseventsd"], "branch": ["{h2}"], "names": ["{h16}"], "cap": (200, 2000), "sigma": 0.8},
        "mac_derivatives": {"top": ["Photos Library.photoslibrary", "resources", "derivatives"], "branch": ["{h1}", "masters" + sep + "{h1}"],
                            "names": ["{uuid}_1_105_c.jpeg", "{uuid}_4_5005_c.jpeg"], "cap": (200, 1200), "sigma": 0.8},
    }
    spec = dict(specs[kind])
    spec["kind"] = kind
    spec["sep"] = sep
    return spec


_BRANDS = ["apple", "google", "microsoft", "adobe", "spotify", "slack", "figma", "zoom", "docker", "jetbrains"]
_MAC_APPS = ["Photon", "Mixboard", "Notecraft", "Lumen", "Taskline", "Framewise", "Soundloop", "Ledger", "Pixelate"]
_CONSOLES = ["Console A", "Console B", "Handheld"]
_TITLES = ["The Quiet Coast", "Northern Lights", "Paper Moons", "Long Road Home", "The Glass House",
           "Silver Lining", "Harbor Nights", "City of Echoes", "Distant Shores", "Morning Static",
           "The Last Orchard", "Winter Station", "Open Water", "Blue Hour", "The Night Shift"]
_PROJECTS = [w + s for w in fakenames.PROJECT_WORDS for s in fakenames.PROJECT_SUFFIXES]
_PKGS = ["lodash", "react", "chalk", "commander", "express", "requests", "numpy", "pillow", "yaml", "ms",
         "debug", "semver", "tslib", "urllib3", "certifi", "idna", "zod", "axios"]


def _fill(template: str, ctx: dict, r: random.Random) -> str:
    def rep(m):
        key = m.group(1)
        if key in ctx:
            return str(ctx[key])
        if key == "h1":
            return f"{r.getrandbits(4):x}"
        if key.startswith("h") and key[1:].isdigit():
            n = int(key[1:])
            return f"{r.getrandbits(4 * n):0{n}x}"
        if key == "n":
            return str(r.randint(1, 400))
        if key == "n2":
            return f"{r.randint(1, 12):02d}"
        if key == "n4":
            return f"{r.randint(1, 9999):04d}"
        if key == "year":
            return str(r.randint(2016, 2025))
        if key == "month":
            return f"{r.randint(1, 12):02d}"
        if key == "date":
            return f"{r.randint(2018, 2025)}{r.randint(1, 12):02d}{r.randint(1, 28):02d}"
        if key == "word":
            return r.choice(fakenames.FOLDER_WORDS["Mixed"]).replace(" ", "")
        if key == "word2":
            return r.choice(fakenames.FOLDER_WORDS["Documents"])
        if key == "uuid":
            return f"{r.getrandbits(32):08X}-{r.getrandbits(16):04X}-{r.getrandbits(16):04X}-{r.getrandbits(16):04X}-{r.getrandbits(48):012X}"
        return m.group(0)
    return re.sub(r"\{(\w+)\}", rep, template)


def _rule_top(spec: dict) -> str:
    """The area's folder as a rule: its fixed parts, up to the first made-up one."""
    parts = []
    for p in spec["top"]:
        if "{" in p:
            break
        parts.append(p)
    return spec["sep"].join(parts)


def _title(r: random.Random) -> str:
    return ("The " if r.random() < 0.4 else "") + r.choice(fakenames.CODE_ADJ).title() + " " + \
        r.choice(fakenames.CODE_NOUN).title()


_FOLDER_MAX = 2500   # files in one folder at most


def _area_files(spec: dict, root: str, count: int, total_bytes: int, r: random.Random):
    """Yield (path, size) for one area, nested leaf folders of at most a few thousand files."""
    if count <= 0:
        return
    sep = spec["sep"]
    # Biggest sizes first: folders that hold big files (game paks, movies) take from the front,
    # the rest from the back.
    sizes = sorted(_lognormal_sizes(count, total_bytes, spec["sigma"], r), reverse=True)
    front, back = 0, count - 1
    ctx_top ={"console": r.choice(_CONSOLES), "uuid": _fill("{uuid}", {}, r)}
    top = sep.join(_fill(p, ctx_top, r) for p in spec["top"])
    base = root + top if root.endswith(sep) else root + sep + top
    i = 0
    leaves: dict[str, list] = {}   # folder -> [files, names used]
    while i < count:
        ctx = {"game": r.choice(_GAMES), "vendor": r.choice(_VENDORS), "title": _title(r),
               "project": r.choice(_PROJECTS), "pkg": r.choice(_PKGS), "brand": r.choice(_BRANDS),
               "app": r.choice(_MAC_APPS)}
        names_of, big = spec.get("names"), None
        if spec.get("app"):
            where, app, subs = r.choice(_APPS)
            sub = _fill(r.choice(subs), ctx, r)
            if sep != _BS:
                app, sub = app.replace(_BS, sep), sub.replace(_BS, sep)
            leaf = sep.join([base, where, app, sub])
            if r.random() < 0.5:
                leaf += sep + f"{r.getrandbits(8):02x}"
        elif spec.get("branches"):
            branch, names_of, big = r.choice(spec["branches"])
            leaf = base + sep + _fill(branch, ctx, r)
        else:
            leaf = base + sep + _fill(r.choice(spec["branch"]), ctx, r)
        st = leaves.get(leaf.lower())
        if st and st[0] >= _FOLDER_MAX:   # a full folder spills into a numbered sub-folder
            leaf += sep + f"{r.randint(1, 99):02d}"
            st = leaves.get(leaf.lower())
            if st and st[0] >= _FOLDER_MAX:
                continue
        if not st:
            st = leaves[leaf.lower()] = [0, set()]
        lo, hi = spec["cap"]
        n = min(count - i, r.randint(lo, hi), _FOLDER_MAX - st[0])
        for _k in range(n):
            name = _fill(r.choice(names_of), ctx, r).replace(sep, "_")
            if name.lower() in st[1]:
                stem, dot, ext = name.rpartition(".")
                c = 2
                while True:
                    cand = f"{stem} ({c}).{ext}" if dot else f"{name} ({c})"
                    if cand.lower() not in st[1]:
                        name = cand
                        break
                    c += 1
            st[1].add(name.lower())
            st[0] += 1
            if big is None:
                size = sizes[r.randint(front, back)] if back > front else sizes[front]
            elif big:
                size = sizes[front]
                front += 1
            else:
                size = sizes[back]
                back -= 1
            yield leaf + sep + name, size
            i += 1


# ---- everyday (backed) files ---------------------------------------------------------------

def _folder_pool(platform: str, drive: str, user: str, n: int, r: random.Random) -> dict[str, list[str]]:
    """{fakenames category: [folders]} for the word-list files on a drive; no repeated words."""
    sep = _BS if platform == "windows" else "/"
    pool: dict[str, list[str]] = {}
    seen = set()
    cats = ["Photos", "Videos", "Music", "Books", "Code", "Archives", "Documents", "Mixed"]
    if drive == "Photos":   # a drive named for photos holds photos and videos
        cats = ["Photos", "Photos", "Videos"]
    tries = 0
    while sum(len(v) for v in pool.values()) < n and tries < n * 20:
        tries += 1
        cat = r.choice(cats)
        words = fakenames.FOLDER_WORDS[cat]
        first = r.choice(words) + r.choice(fakenames.QUALIFIERS)
        if platform == "windows":
            if drive == "C":
                top = sep.join(["C:", "Users", user, r.choice(["Documents", "Downloads", "Pictures", "Videos",
                                                               "Music", "Desktop", "Projects"])])
                folder = top + sep + first
            else:
                folder = f"{drive}:" + sep + first
        else:
            folder = ("/Users/" + user + "/" + r.choice(["Documents", "Pictures", "Movies", "Music", "Desktop",
                                                          "Projects"]) + "/" + first if drive == "root"
                      else f"/Volumes/{drive}/{first}")
        if r.random() < 0.85:
            second = r.choice([w for w in words if w not in first] or words)
            year = str(r.randint(2016, 2025))
            folder += sep + (second if r.random() < 0.6 or year in first else year)
        if r.random() < 0.25:
            folder += sep + r.choice(fakenames.FOLDER_WORDS["Mixed"])
        key = folder.lower()
        if key in seen:
            continue
        seen.add(key)
        pool.setdefault(cat, []).append(folder)
    return pool


_OTHER_TEMPLATES = ["IMG_{n4}.cr2", "IMG_{n4}.CR3", "DSC_{n4}.nef", "_DSC{n4}.arw", "DSCF{n4}.raf", "P{n4}.rw2",
                    "{topic} {yr}.psd", "{topic} poster v{n1}.psd", "{topic} mockup.ai", "{topic} edit.prproj",
                    "{topic} {yr}.lrcat", "{topic} timeline.drp", "{topic} scene.blend", "{topic} {n2}.blend",
                    "{topic} brochure.indd", "{topic} map.dwg", "{topic} {yr}.sketch", "{topic} final.aep",
                    "{topic} session.als", "{topic} mix.logicx", "{topic} {yr}.cpr", "{topic}.flp",
                    "settings-{yr}.json", "{topic} notes.json", "library.db", "{topic}.sqlite", "contacts {yr}.vcf",
                    "{topic} {yr}.iso", "Windows {yr} install.iso", "{topic} backup {yr}.bak",
                    "{topic} save {n2}.sav", "slot{n1}.sav", "{topic} {yr}.kdbx", "{topic}.gpx", "{topic} route.kml",
                    "{topic} model.stl", "{topic} part {n2}.step", "{topic} {yr}.obj", "{topic} dataset.parquet"]
# Stems that read like files someone named, not like a generator's counters.
_TOPICS = ["Iceland", "Lisbon", "Wedding", "Birthday", "Garden", "Kitchen", "Studio", "Holiday", "Portfolio",
           "Client", "Album", "Thesis", "Budget", "Recipe", "Renovation", "Road Trip", "Concert", "Podcast",
           "Product", "Family", "Hiking", "Beach", "Cabin", "Workshop", "Launch", "Course", "Garage", "Festival"]
_MIXED_STEMS = ["{topic} {yr}", "{topic} notes", "{topic} ({n2})", "{topic} final", "{topic} copy", "Scan {date}",
                "{topic} draft {n1}", "New {topic}", "{topic} export", "{topic} v{n1}"]
_TEMPLATE_NAME = re.compile(r"^(?:data|file|doc|blob|item|mbox)-\d+\.([A-Za-z0-9]+)$")


def _name_fmt(num: int, r: random.Random) -> dict:
    year = r.randint(2016, 2025)
    return {"n4": f"{num % 10000:04d}", "n3": f"{num % 1000:03d}", "n2": f"{num % 100:02d}",
            "n1": str(num % 9 + 1), "date": f"{year}-{r.randint(1, 12):02d}", "yr": year,
            "word": r.choice(fakenames.FOLDER_WORDS["Mixed"]), "topic": r.choice(_TOPICS)}


def _filler_name(bucket: str, seq: int, start: int, r: random.Random) -> str:
    group, exts = _BUCKET_FILES[bucket]
    fmt = _name_fmt(start + seq, r)
    if bucket == "other":
        return r.choice(_OTHER_TEMPLATES).format(**fmt)
    if bucket == "browserfavorites":
        return r.choice(fakenames.COMMON_SITES) + ".url"
    tpls = fakenames.FILE_TEMPLATES.get(group) if group != "Mixed" else None
    tpl = r.choice(tpls or _MIXED_STEMS)
    return tpl.format(**fmt) + "." + r.choice(exts)


def _believable(path: str, sep: str, used: set, r: random.Random) -> str:
    """A counter-style name from the Kopia demo's hidden areas (``data-956.mbox``) renamed to one a
    person would give that kind of file; the folder and extension stay."""
    folder, _, leaf = path.rpartition(sep)
    m = _TEMPLATE_NAME.match(leaf)
    if not m:
        return path
    ext = m.group(1)
    fmt = _name_fmt(r.randint(1, 9999), r)
    if _bucket_of(leaf) in ("other", "email"):
        stem = r.choice(_MIXED_STEMS).format(**fmt)
    else:
        group = _BUCKET_FILES[_bucket_of(leaf)][0]
        stem = r.choice(fakenames.FILE_TEMPLATES.get(group) if group != "Mixed" else _MIXED_STEMS).format(**fmt)
    out, c = f"{folder}{sep}{stem}.{ext}", 2
    while out.lower() in used:
        out, c = f"{folder}{sep}{stem} ({c}).{ext}", c + 1
    used.add(out.lower())
    return out


def _pick_kopia(by_drive: dict, drive: str, target: int, engine, mount: str):
    """A representative 70% slice of the demo's own (already faked) files on this drive."""
    cands = sorted(by_drive.get(drive, {}).values(), key=lambda f: f[0].lower())
    cands = [c for c in cands if not engine.classify(c[0], mount)[0]]
    want = min(len(cands), int(target * 0.7))
    if want <= 0:
        return []
    if want >= len(cands):
        return cands
    step = len(cands) / want
    return [cands[int(i * step)] for i in range(want)]


def _make_filler(platform: str, drive: str, user: str, count: int, total: int, weights: dict,
                 engine, mount: str, now: float, r: random.Random,
                 capped: bool = True) -> list[tuple[str, int, float]]:
    """Word-list files for a drive: types from `weights`, sized by type, added 2-4 years ago.
    Uncapped (the full demo), the sizes stretch to reach the drive's real total."""
    if count <= 0:
        return []
    sep = _BS if platform == "windows" else "/"
    pool = _folder_pool(platform, drive, user, max(8, count // 450), r)
    pool = {c: [f for f in fs if not engine.classify(f + sep + "x.jpg", mount)[0]] for c, fs in pool.items()}
    buckets = list(weights)
    wts = [weights[b] for b in buckets]
    seqs: dict[str, list[int]] = {}
    out = []
    raw_sizes = []
    for _ in range(count):
        b = r.choices(buckets, wts)[0]
        folders = pool.get(_BUCKET_FOLDERS[b]) or [f for fs in pool.values() for f in fs]
        folder = r.choice(folders)
        st = seqs.setdefault(folder, [0, r.randint(10, 7000)])
        st[0] += 1
        name = _filler_name(b, st[0], st[1], r)
        mean = _REAL_TYPES[b][1] / max(1, _REAL_TYPES[b][0])
        raw_sizes.append(r.lognormvariate(math.log(max(1.0, mean)) - 0.72, 1.2))
        out.append((folder + sep + name, now - r.uniform(60, 1500) * _DAY))
    scale = total / max(1.0, sum(raw_sizes))
    scale = max(0.3, min(4.0, scale) if capped else scale)
    files, names = [], set()
    for (path, t), s in zip(out, raw_sizes):
        if path.lower() in names:
            stem, dot, ext = path.rpartition(".")
            c = 2
            while True:
                cand = f"{stem} ({c}).{ext}" if dot else f"{path} ({c})"
                if cand.lower() not in names:
                    path = cand
                    break
                c += 1
        names.add(path.lower())
        files.append((path, max(1, int(s * scale)), t))
    return files


# ---- rules ---------------------------------------------------------------------------------

def _rule(attrs: str = "", *, plat: str = "win", optional: bool = False) -> str:
    base = {"plat": plat, "osVers": "*", "ruleIsOptional": "t" if optional else "f",
            "skipFirstCharThenStartsWith": "*", "contains_1": "*", "contains_2": "*",
            "doesNotContain": "*", "endsWith": "*", "hasFileExtension": "*"}
    if attrs:
        for kv in attrs.split("|"):
            k, _, v = kv.partition("=")
            base[k] = v
    return "<excludefname_rule " + " ".join(f'{k}="{v}"' for k, v in base.items()) + " />"


def _builtin_rules(platform: str) -> tuple[list[str], list[str]]:
    """(mandatory, editable) rule lines when Backblaze's own rule files are not on this PC."""
    if platform == "windows":
        mand = [_rule("skipFirstCharThenStartsWith=:" + _BS + "windows" + _BS),
                _rule("skipFirstCharThenStartsWith=:" + _BS + "program files" + _BS),
                _rule("skipFirstCharThenStartsWith=:" + _BS + "$recycle.bin" + _BS),
                _rule("skipFirstCharThenStartsWith=:" + _BS + "system volume information" + _BS),
                _rule("endsWith=" + _BS + "pagefile.sys|hasFileExtension=sys"),
                _rule("endsWith=" + _BS + "hiberfil.sys|hasFileExtension=sys"),
                _rule("endsWith=" + _BS + "swapfile.sys|hasFileExtension=sys")]
        edit = [_rule("skipFirstCharThenStartsWith=:" + _BS + "users" + _BS + "|contains_1=" + _BS + "appdata" + _BS + "local" + _BS + "temp" + _BS),
                _rule("contains_1=" + _BS + ".gradle" + _BS, optional=True)]
        return mand, edit
    mand = [_rule(f"skipFirstCharThenStartsWith={p}", plat="mac") for p in
            ("private/var/", ".spotlight-v100/", ".fseventsd/", "system/", "library/", "applications/",
             "usr/", "bin/", "sbin/", "cores/", "private/")]
    mand.append(_rule("skipFirstCharThenStartsWith=users/|contains_1=/library/caches/", plat="mac"))
    return mand, [_rule("contains_1=/node_modules/.cache/", plat="mac", optional=True)]


def _installed_rules(platform: str) -> tuple[list[str], list[str]] | None:
    """Backblaze's own default rule files from this PC (generic rules, no user data); rules naming
    this PC's user are dropped. None when Backblaze is not installed here."""
    root = os.path.join(os.environ.get("ProgramData", "C:" + _BS + "ProgramData"), "Backblaze", "bzdata")
    me = (os.environ.get("USERNAME") or "").lower()
    out = []
    for name in ("bzexcluderules_mandatory.xml", "bzexcluderules_editable.xml"):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
        rules = [m.group(0) for m in re.finditer(r"<excludefname_rule\b[^>]*/?>", text)]
        out.append([x for x in rules if not (me and me in x.lower())])
    return out[0], out[1]


def _write_rules(root: str, platform: str, user: str, host: str, folders: list[str], full: bool) -> None:
    rules = (_installed_rules(platform) if full else None) or _builtin_rules(platform)
    _xml(root, "bzexcluderules_mandatory.xml", "bzexclusions", ["<!-- Mandatory Exclusions -->"] + rules[0])
    _xml(root, "bzexcluderules_editable.xml", "bzexclusions", ["<!-- Editable Exclusions -->"] + rules[1])
    dirs = "".join(f'<bzdirfilter dir="{d}" whichfiles="none" />' for d in folders)
    _xml(root, "bzinfo.xml", "bzinfo",
         [f'<myidentity online_hostname="{host}" bzlogin="{user}@example.com" />'
          f'<globalexcludes excludefiletypes="{_EXCLUDED_TYPES}" />'
          f"<do_backup>{dirs}</do_backup>"])


def _xml(root: str, rel: str, top: str, lines: list[str]) -> None:
    fp = os.path.join(root, rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'<?xml version="1.0" encoding="utf-8" ?>\n<{top}>\n')
        fh.write("\n".join(lines))
        fh.write(f"\n</{top}>\n")


# ---- reports -------------------------------------------------------------------------------

def _filestats(root: str, files: list, host: str, now: float, platform: str) -> None:
    acc = {c: [0, 0] for c in (*_CATEGORY, "financial", "other", "everything")}
    for path, size, _m in files:
        for c in (_bucket_of(_leaf(path)), "everything"):
            acc[c][0] += 1
            acc[c][1] += size
    attrs = " ".join(f'{c}num="{n}" {c}totbytes="{b}"' for c, (n, b) in acc.items())
    _xml(root, os.path.join("bzfilelists", "filestats.xml"), "filestats",
         ['<progress good_for_installer="true" totally_final="true" />',
          f'<info datetime="{_stamp(now)}" aonlinename="{host}" plat="{"mac" if platform == "mac" else "win64"}" '
          f'osdetail="{"macOS" if platform == "mac" else "Windows11"}" {attrs} />'])


def _history(root: str, backed: list, queued: list, now: float, scale: bool, r: random.Random,
             tail: list[str] | None = None) -> None:
    """bzfileids (cumulative, ~3.7x the live files, oldest first, then ``tail``: the newest ids,
    which Recently added reads), the upload queue, the per-day upload records in the real rhythm,
    the event log and the upload-success history."""
    dc = os.path.join(root, "bzbackup", "bzdatacenter")
    os.makedirs(dc, exist_ok=True)
    current = sorted(backed, key=lambda f: f[2])
    hist = max(0, int(len(current) * 2.7))
    with open(os.path.join(root, "bzbackup", "bzfileids.dat"), "w", encoding="utf-8", newline="\n") as fh:
        seq = 0x1000000000000000
        if current:
            for i in range(hist):   # replaced and deleted files, all older than the recent window
                p = current[r.randrange(len(current))][0]
                if r.random() < 0.3:
                    stem, dot, ext = p.rpartition(".")
                    p = f"{stem} (old).{ext}" if dot and ext.isalnum() else p + " (old)"
                fh.write(f"{seq:016x}\t{p}\n")
                seq += 1
        for p, _s, _t in current:
            fh.write(f"{seq:016x}\t{p}\n")
            seq += 1
        for p in tail or []:
            fh.write(f"{seq:016x}\t{p}\n")
            seq += 1
    todo =os.path.join(dc, f"bz_todo_{_stamp(now)[:8]}_0.dat")
    with open(todo, "w", encoding="utf-8", newline="\n") as fh:
        for p, s, t in queued:
            fh.write(f"1\t+\t{r.getrandbits(64):016x}\t{int(t * 1000):016x}\t{s}\t{p}\n")
    os.utime(todo, (now - 1320, now - 1320))

    pattern = _REAL_UPLOADS if scale else [(d, max(1, len(backed) // 200), 0) for d in (1, 3, 6, 9, 14)]
    by_time = sorted(backed, key=lambda f: f[2])
    small = [f for f in backed if f[1] < 20_000_000] or backed
    events, upload_days, prev = [], [], now - 45 * _DAY
    records: dict[str, list[str]] = {}
    for days, files_n, bytes_n in sorted(pattern, key=lambda x: -x[0]):
        day0 = _dt.datetime.fromtimestamp(now - days * _DAY).replace(hour=0, minute=0, second=0, microsecond=0)
        start = day0.timestamp() + r.uniform(9, 19) * 3600   # one upload day stays inside its own date
        want = max(1, int(files_n * r.uniform(0.85, 1.15)))
        want_bytes = int(bytes_n * r.uniform(0.85, 1.15)) if bytes_n else 0
        new = [f for f in by_time if prev < f[2] <= start]
        r.shuffle(new)
        todays, total = [], 0
        for f in new:   # files added since the last upload day, within the day's real size
            if len(todays) >= want:
                break
            if not want_bytes or total + f[1] <= want_bytes or f[1] < 5_000_000:
                todays.append(f)
                total += f[1]
        tries = 0
        while len(todays) < want and tries < want * 6:
            tries += 1
            f = r.choice(small)
            if not want_bytes or total + f[1] <= want_bytes * 1.05 or f[1] < 2_000_000:
                todays.append(f)
                total += f[1]
        prev = start
        name = f"bz_done_{_stamp(start)[:8]}_0.dat"
        lines = records.setdefault(name, [])
        for path, size, _t in todays:
            t = start + r.uniform(0, 5400)
            lines.append("\t".join(["5", "-", "---", _stamp(t), "f_" + f"{r.getrandbits(80):020x}", "n--",
                                    f"{size:016x}", "n-_a-----", "-" * 20, "-" * 16, "-" * 16, "-", "0", path]))
        events.append(f"{_dt.datetime.fromtimestamp(start):%Y-%m-%d %H:%M:%S} - STARTBACKUP")
        if r.random() < 0.3:
            end = start + r.uniform(900, 7200)
            events.append(f"{_dt.datetime.fromtimestamp(end):%Y-%m-%d %H:%M:%S} - END_BACKUP: "
                          f"Backed up {len(todays)} FILES / {total // 1_000_000} MB")
        upload_days.append(f'<one_upload_success_stat date_gmt="{_gmt_day(start)}" '
                           f'num_upload_success="{len(todays)}" num_upload_fail_CvtTooBusy="{r.choice([0, 0, 0, 1])}" '
                           f'num_upload_fail_CvtNoRoom="0" num_upload_fail_UnknownReason="0" />')
    for name, lines in records.items():
        fp = os.path.join(dc, name)
        with open(fp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        t = _dt.datetime.strptime(name[8:16], "%Y%m%d").timestamp() + 86000
        os.utime(fp, (min(t, now), min(t, now)))
    for d in range(45, 0, -1):   # Backblaze starts a pass most days, even with nothing to send
        if r.random() < 0.3:
            t = now - d * _DAY + r.uniform(1, 23) * 3600
            events.append(f"{_dt.datetime.fromtimestamp(t):%Y-%m-%d %H:%M:%S} - STARTBACKUP")
    events.sort()
    os.makedirs(os.path.join(root, "bzlogs", "bzreports_eventlog"), exist_ok=True)
    with open(os.path.join(root, "bzlogs", "bzreports_eventlog", "01.log"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("\n".join(events) + "\n")
    _xml(root, os.path.join("bzreports", "bzstat_upload_success.xml"), "upload_success_history", upload_days)
    _xml(root, os.path.join("bzreports", "zbytes_history.xml"), "zbytes_history",
         [f'<one_zbyte_stat date_time_written_gmt="{_stamp(now - d * _DAY)}" '
          f'last_rollback_instr="none{_stamp(now - d * _DAY)}_ok000a" num_z_lines="0" tot_bytes_on_z_lines="0" />'
          for d in range(10, 0, -1)])


def _fixed_excluded(platform: str, drive: str, r: random.Random) -> list[tuple[str, int]]:
    """Single big system files a real scan lists (Backblaze excludes them)."""
    if platform == "windows" and drive == "C":
        return [("C:" + _BS + "pagefile.sys", int(64 * 1024**3 * r.uniform(0.85, 1.15))),
                ("C:" + _BS + "hiberfil.sys", int(25 * 1024**3 * r.uniform(0.85, 1.15))),
                ("C:" + _BS + "swapfile.sys", 16 * 1024**2)]
    if platform == "mac" and drive == "root":
        return [("/private/var/vm/swapfile0", 2 * 1024**3), ("/private/var/vm/swapfile1", 2 * 1024**3),
                ("/private/var/vm/sleepimage", int(16 * 1024**3 * r.uniform(0.85, 1.15)))]
    return []


def _top2(path: str, sep: str) -> str:
    """The folder Recently added rolls a path up to (two levels below its drive)."""
    parts = path.split(sep)
    if sep == "/" and path.startswith("/Volumes/"):
        return "/".join(parts[:5])
    return sep.join(parts[:3])


def _add_folder(v: dict, folder: str, count: int, engine, now: float, r: random.Random) -> list[str]:
    """Recent files for a hand-picked folder the Mac data doesn't have yet."""
    low = folder.lower()
    if any(w in low for w in ("photo", "camera", "raw", "drone", "edited", "album", "scan")):
        bucket = "pict"
    elif "video" in low or "export" in low:
        bucket = "video"
    elif "music" in low or "stem" in low:
        bucket = "music"
    else:
        bucket = "other"
    mean = _REAL_TYPES[bucket][1] / _REAL_TYPES[bucket][0]
    start, names, out = r.randint(10, 7000), set(), []
    for i in range(max(40, min(1500, count // 10))):
        name = _filler_name(bucket, i, start, r)
        p = folder + "/" + name
        if name.lower() in names or engine.classify(p, v["mount"])[0]:
            continue
        names.add(name.lower())
        out.append((p, max(1, int(r.lognormvariate(math.log(mean) - 0.72, 1.2))), now - r.uniform(1, 40) * _DAY))
    v["backed"].extend(out)
    return [p for p, _s, _t in out]


def _recent_tail(packed: dict, platform: str, user: str, engine, now: float, r: random.Random) -> list[str]:
    """The newest file ids: the pinned Recently added folders with their counts, then other folders
    (each below the smallest pin) until the tail fills the window Recently added reads."""
    from ..backblaze.queries import _CHURN_TAIL
    mac = platform == "mac"
    sep = "/" if mac else _BS
    pins = []
    for folder, count in (_MAC_RECENT if mac else _WIN_RECENT):
        folder = folder.replace("{user}", user)
        drive = (folder.split("/")[2] if folder.startswith("/Volumes/") else "root") if mac else folder[0]
        v = packed.get(drive)
        if not v:
            continue
        lo = folder.lower() + sep
        files = [f[0] for f in v["backed"] if f[0].lower().startswith(lo)]
        if not files and mac:
            files = _add_folder(v, folder, count, engine, now, r)
        if files:
            pins.append((folder.lower(), files, int(count * r.uniform(0.96, 1.04))))
    out, size = [], 0
    for _f, files, n in pins:
        for _ in range(n):
            p = r.choice(files)
            out.append(p)
            size += 18 + len(p.encode("utf-8"))
    pinned = {f for f, _fs, _n in pins}
    cap = int(min((n for _f, _fs, n in pins), default=5000) * 0.8)
    pool = [f[0] for v in packed.values() for f in v["backed"] if _top2(f[0], sep).lower() not in pinned]
    per: dict[str, int] = {}
    tries = 0
    while pool and size < _CHURN_TAIL + 65536 and tries < 3_000_000:
        tries += 1
        p = r.choice(pool)
        k = _top2(p, sep).lower()
        if per.get(k, 0) >= cap:
            continue
        per[k] = per.get(k, 0) + 1
        out.append(p)
        size += 18 + len(p.encode("utf-8"))
    r.shuffle(out)
    return out


def write(root: str, files: list[tuple[str, int, float]], *, user: str, host: str,
          now: float | None = None, seed: int = 1, platform: str = "windows",
          profile=None) -> dict:
    """Write ``root`` (a ``bzdata`` folder) for the targets of ``profile`` (None = full scale).
    ``files``: the demo's own files as (path, size, first seen)."""
    from ..backblaze import paths, rules
    r = random.Random(f"bz-{seed}-{platform}")
    now = now or _dt.datetime.now().timestamp()
    full = profile is None
    targets = _targets(profile, platform)
    if full:
        targets = {d: {k: _moved(v, r) for k, v in t.items()} for d, t in targets.items()}
    for sub in ("bzfilelists", "bzreports", os.path.join("bzbackup", "bzdatacenter"),
                os.path.join("bzlogs", "bzreports_eventlog"), os.path.join("bzlogs", "bzfilelist"),
                "bzupdates"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    mac = platform == "mac"
    sep = "/" if mac else _BS
    areas_of = _MAC_AREAS if mac else _WIN_AREAS
    if not mac:   # made-up folders take the demo files' spelling of the user folder (one case on C:)
        home = ("c:" + _BS + "users" + _BS + user + _BS).lower()
        user = next((f[0].split(_BS)[2] for f in files if f[0].lower().startswith(home)), user)

    # Folder rules: one per excluded area that Backblaze's own rules don't already cover, plus
    # spare rules that match nothing (real lists have many).
    paths.set_bzdata_root(root)
    _write_rules(root, platform, user, host, [], full)
    base_engine = rules.load_engine("mac" if mac else "win")
    folders, specs = [], {}
    for drive, areas in sorted(areas_of.items()):
        mount = ("/" if drive == "root" else f"/Volumes/{drive}") if mac else f"{drive}:" + _BS
        for kind, _c, _b in areas:
            spec = _area_spec(kind, user, sep)
            specs[(drive, kind)] = spec
            top = _rule_top(spec)
            probe = (mount.rstrip("/") + "/" + top + "/x/y.dat") if mac else (mount + top + _BS + "x" + _BS + "y.dat")
            if base_engine.classify(probe, mount)[0]:
                continue   # Backblaze's own rules exclude it already
            if mac:
                rel = (mount.rstrip("/") + "/" + top + "/") if drive != "root" else "/" + top + "/"
            else:
                rel = (_BS + top + _BS).lower()
            if rel.lower() not in {f.lower() for f in folders}:
                folders.append(rel)
    spare = [("/Users/" + user + "/" + s) if mac else (_BS + s + _BS)
             for s in (_MAC_SPARE_RULES if mac else _WIN_SPARE_RULES)]
    folders += spare if full else spare[:3]
    if not mac and "I" in targets:   # the real list excludes a whole-drive copy kept on I:
        folders.append("i:" + _BS + "c drive backup" + _BS)
    _write_rules(root, platform, user, host, folders, full)
    engine = rules.load_engine("mac" if mac else "win")

    by_drive: dict[str, dict] = {}
    if not mac and files:
        for f in files:
            p = f[0]
            if len(p) > 2 and p[1] == ":" and p[0].upper() in targets:
                by_drive.setdefault(p[0].upper(), {}).setdefault(p.lower(), (p, int(f[1]), float(f[2])))

    def mount_of(drive: str) -> str:
        return ("/" if drive == "root" else f"/Volumes/{drive}") if mac else f"{drive}:" + _BS

    # Pass 1: the demo's own files; pass 2: word-list files filling each type up to the real mix.
    picked = {d: _pick_kopia(by_drive, d, targets[d]["backed"], engine, mount_of(d)) if by_drive else []
              for d in targets}
    used = {p.lower() for sel in picked.values() for p, _s, _t in sel}
    picked = {d: [(_believable(p, sep, used, r), s, t) for p, s, t in sel] for d, sel in picked.items()}
    total_backed = sum(t["backed"] for t in targets.values())
    real_total = sum(c for c, _b in _REAL_TYPES.values())
    have = {b: 0 for b in _REAL_TYPES}
    for sel in picked.values():
        for p, _s, _t in sel:
            have[_bucket_of(_leaf(p))] += 1
    if full:   # the demo's own files over-supply some types (archives, email): keep the real mix
        for b, (c, _bb) in _REAL_TYPES.items():
            extra = have[b] - int(c / real_total * total_backed * 1.05)
            if extra > 0:
                of_b = [(d, f[0]) for d, sel in picked.items() for f in sel if _bucket_of(_leaf(f[0])) == b]
                drop = set(r.sample(of_b, min(extra, len(of_b))))
                picked = {d: [f for f in sel if (d, f[0]) not in drop] for d, sel in picked.items()}
                have[b] -= len(drop)
    weights = {b: max(0.0, c / real_total * total_backed - have[b]) for b, (c, _bb) in _REAL_TYPES.items()}
    if sum(weights.values()) <= 0:
        weights = {b: c for b, (c, _bb) in _REAL_TYPES.items()}

    vols, packed = [], {}
    for drive in sorted(targets):
        t = targets[drive]
        mount = mount_of(drive)
        key = drive
        token = drive.lower() if not mac else drive
        guid = "v" + f"{r.getrandbits(92):023x}"
        selected = picked[drive]
        need = max(0, t["backed"] - len(selected))
        need_bytes = max(1, t["backed_bytes"] - sum(f[1] for f in selected))
        filler = _make_filler(platform, drive, user, need, need_bytes, weights, engine, mount, now, r,
                              capped=not full)
        backed, seen = [], set()
        for p, s, ts in [*selected, *filler]:   # a word-list name can land on a demo file's path
            if p.lower() not in seen:
                seen.add(p.lower())
                backed.append((p, int(s), float(ts)))
        # A drive with almost nothing added lately gets a few active folders, like any real drive.
        recent = sum(1 for f in backed if f[2] > now - 45 * _DAY)
        if filler and recent < len(backed) * 0.003:
            hot = {f[0].rsplit(sep, 1)[0] for f in r.sample(filler, min(len(filler), 3))}
            budget = max(2, int(len(backed) * 0.004))
            moved = []
            for p, s, ts in backed:
                if budget and p.rsplit(sep, 1)[0] in hot:
                    budget -= 1
                    moved.append((p, s, now - r.uniform(0.2, 44) * _DAY))
                else:
                    moved.append((p, s, ts))
            backed = moved
        packed[key] = {"guid": guid, "backed": backed, "mount": mount, "token": token}
        vols.append((guid, mount, key))

    tail = _recent_tail(packed, platform, user, engine, now, r) if full else None

    # The upload queue: changed files everywhere (already counted by Backblaze) plus a few new
    # ones on one or two drives - so most drives read +0 and the Diff shows only those.
    keys = sorted(packed, key=lambda k: -sum(1 for f in packed[k]["backed"] if f[2] > now - 3 * _DAY))
    new_drives = set(keys[: (2 if full else 1)])
    for key, v in packed.items():
        newest = sorted(v["backed"], key=lambda f: -f[2])
        n_new = (r.randint(4, 40) if full else 2) if key in new_drives else 0
        v["new"] = newest[:n_new]
        pool = [f for f in newest[n_new: n_new + 4000] if f[1] < 400_000_000]
        n_changed = min(len(pool), int(len(v["backed"]) * r.uniform(0.0008, 0.0025))) if full else min(len(pool), 1)
        v["changed"] = r.sample(pool, n_changed)
        new_set = {p for p, _s, _t in v["new"]}
        v["uploaded"] = [f for f in v["backed"] if f[0] not in new_set]

    for key, v in packed.items():
        t = targets[key]
        fp = os.path.join(root, "bzfilelists",
                          f"{v['guid']}_{v['token']}____filelist.dat" if not mac else f"{v['guid']}_{v['token']}_filelist.dat")
        scan = now - r.uniform(0.3, 1.8) * _DAY
        excl_n = excl_b = 0
        with open(fp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"# GmtMillisThisListWasStarted: {int(scan * 1000):016x}, GmtDateTime: {_gmt(scan)}\n"
                     f"#\n# Dir: {v['mount']}\n")
            for path, size, ts in v["backed"]:
                fh.write(f"f\t{int(ts * 1000):x}\t{size}\t{path}\n")
            fixed = _fixed_excluded(platform, key, r)
            rest = max(0, int(t["excluded_bytes"]) - sum(s for _p, s in fixed))
            for path, size in fixed:
                fh.write(f"f\t{int((now - r.uniform(1, 30) * _DAY) * 1000):x}\t{size}\t{path}\n")
                excl_n += 1
                excl_b += size
            areas = areas_of.get(key) or areas_of[sorted(areas_of)[0]]
            share_n = sum(a[1] for a in areas)
            share_b = sum(a[2] for a in areas)
            left = max(0, t["excluded"] - len(fixed))
            for kind, cshare, bshare in areas:
                spec = specs.get((key, kind)) or _area_spec(kind, user, sep)
                count = int(left * cshare / share_n)
                for path, size in _area_files(spec, v["mount"], count, int(rest * bshare / share_b), r):
                    fh.write(f"f\t{int((now - r.uniform(1, 1200) * _DAY) * 1000):x}\t{size}\t{path}\n")
                    excl_n += 1
                    excl_b += size
            if len(v["backed"]) + excl_n < 20:
                fh.write("# " + "x" * 1200 + "\n")   # never read as an aborted (stub) scan
        os.utime(fp, (scan + 3600, scan + 3600))
        v["excluded_bytes"] = excl_b

    used = {k: sum(f[1] for f in v["backed"]) + v["excluded_bytes"] for k, v in packed.items()}
    _volumes(root, vols, now, used)
    _reports(root, packed, now, platform)
    uploaded = [f for v in packed.values() for f in v["uploaded"]]
    _filestats(root, uploaded, host, now, platform)
    queued = [f for v in packed.values() for f in v["new"] + v["changed"]]
    _history(root, [f for v in packed.values() for f in v["backed"]], queued, now, full, r, tail)
    _versions(root, platform)
    nfiles = len(uploaded)
    nbytes = sum(f[1] for f in uploaded)
    log.info("demo bzdata: %d drives, %d backed up (%d bytes), platform %s", len(packed), nfiles, nbytes, platform)
    return {"drives": sorted(packed), "files": sum(len(v["backed"]) for v in packed.values()),
            "backed": nfiles, "bytes": nbytes, "platform": platform}


def _volumes(root: str, vols: list, now: float, used: dict) -> None:
    lines = [f'<bzvol_metadata lastAssignedBzVolShortId="{len(vols)}" '
             f'gmtMillisThisFileWasWritten="{int(now * 1000)}" gmtDateTimeFileWasWritten="{_stamp(now)}" />']
    for guid, mount, key in vols:
        u = int(used.get(key, 0))
        total = max(int(u * 1.6), 500 * 10**9)
        lines.append(f'<bzvolume bzVolumeGuid="{guid}" mountPointPathHex="{_hex(mount)}" '
                     f'typeOfVolumeTwoCharCode="hd" lastTimeVolumeWasSeenAttachedGmtMillis="{int(now * 1000)}" '
                     f'numBytesFreeOnVolume="{total - u}" numBytesUsedOnVolume="{u}" '
                     f'numBytesTotalOnVolume="{total}" />')
    _xml(root, "bzvolumes.xml", "contents", lines)


def _reports(root: str, packed: dict, now: float, platform: str) -> None:
    totals, rem = [], []
    grand = [0, 0]
    rem_all = [0, 0]
    for _key, v in packed.items():
        up_n, up_b = len(v["uploaded"]), sum(f[1] for f in v["uploaded"])
        q = v["new"] + v["changed"]
        q_n, q_b = len(q), sum(f[1] for f in q)
        grand[0] += up_n
        grand[1] += up_b
        rem_all[0] += q_n
        rem_all[1] += q_b
        totals.append(f'<bzvolume bzVolumeGuid="{v["guid"]}" pervol_sel_for_backup_numfiles="{up_n}" '
                      f'pervol_sel_for_backup_numbytes="{up_b}" />')
        rem.append(f'<bzvolume bzVolumeGuid="{v["guid"]}" pervol_remaining_files_numfiles="{q_n}" '
                   f'pervol_remaining_files_numbytes="{q_b}" />')
    _xml(root, os.path.join("bzreports", "bzstat_totalbackup.xml"), "contents",
         [f'<totals totnumfilesforbackup="{grand[0]}" totnumbytesforbackup="{grand[1]}" />'] + totals)
    _xml(root, os.path.join("bzreports", "bzstat_remainingbackup.xml"), "contents",
         [f'<remaining remainingnumfilesforbackup="{rem_all[0]}" '
          f'remainingnumbytesforbackup="{rem_all[1]}" />'] + rem)
    last = now - 1500
    _xml(root, os.path.join("bzreports", "bzstat_lastbackupcompleted.xml"), "contents",
         [f'<lastbackupcompleted gmt_millis="{int(last * 1000)}" localdatetime="{_stamp(last)}" />'])
    _xml(root, "overviewstatus.xml", "status",
         [f'<bztransmit cur_state="idle_waiting" current_file="none" current_file_fullpath="none" '
          f'millisFileWasWritten="{int(now * 1000)}" />'])


def _versions(root: str, platform: str) -> None:
    win, mac = "10.0.4.1081", "10.0.4.1082"
    _xml(root, os.path.join("bzupdates", "clientversion.xml"), "clientversion",
         [f'<release win32_version="{win}" mac_version="{mac}" />',
          '<autoupdate win32_version="9.2.2.897" mac_version="9.2.2.898" />'])
    os.makedirs(os.path.join(root, "bzlogs", "bzfilelist"), exist_ok=True)
    with open(os.path.join(root, "bzlogs", "bzfilelist", "bzfilelist01.log"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(f"2026-11-10 00:00:00 - bzfilelist starting, version={mac if platform == 'mac' else win} "
                 f"os={'mac' if platform == 'mac' else 'win'}\n")


def trim(root: str, keep_lists: bool = False, keep_ids: bool = False) -> int:
    """After indexing, drop what only the indexer reads: per-day upload records, the file-list
    bodies unless ``keep_lists`` (a folder switch rebuilds from them), and all but the tail of the
    file ids unless ``keep_ids`` (a rebuild then counts the full history again). Returns bytes freed."""
    from ..backblaze.queries import _CHURN_TAIL
    freed = 0
    dc = os.path.join(root, "bzbackup", "bzdatacenter")
    for name in os.listdir(dc):
        if name.startswith("bz_done_"):
            freed += os.path.getsize(os.path.join(dc, name))
            os.remove(os.path.join(dc, name))
    if not keep_lists:
        lists = os.path.join(root, "bzfilelists")
        for name in os.listdir(lists):
            if name.endswith("filelist.dat"):
                path = os.path.join(lists, name)
                before = os.path.getsize(path)
                with open(path, encoding="utf-8") as fh:
                    head = [next(fh, "") for _ in range(4)]
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("".join(head))
                    fh.write("# " + "x" * 1200 + "\n")
                freed += before - os.path.getsize(path)
    ids = os.path.join(root, "bzbackup", "bzfileids.dat")
    size = os.path.getsize(ids)
    if not keep_ids and size > _CHURN_TAIL:
        with open(ids, "rb") as fh:
            fh.seek(size - _CHURN_TAIL)
            fh.readline()   # a partial first line
            tail = fh.read()
        with open(ids, "wb") as fh:
            fh.write(tail)
        freed += size - len(tail)
    return freed


def index(root: str) -> dict:
    """Build the demo folder's Backblaze index from ``root`` with the real indexer."""
    from ..backblaze import indexer, paths
    paths.forget_platform(root)
    paths.set_bzdata_root(root)
    if os.path.normcase(os.path.abspath(paths.bzdata_root())) != os.path.normcase(os.path.abspath(root)):
        raise RuntimeError("demo Backblaze index: the reader is not pointed at the demo's bzdata")
    return indexer.refresh(force=True)
