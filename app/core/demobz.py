"""Fake Backblaze data for a demo folder, read by the real Backblaze indexer.

``write`` lays out one bzdata folder (Windows, or a Mac's when ``platform="mac"``) from per-drive
targets shaped like a real Backblaze setup, each moved by about 10-20%. Everyday files come from
the demo's own already-faked Kopia names; the rest are made from the word lists in ``fakenames``.
Excluded files sit in the places Backblaze excludes (Windows, Program Files, AppData, Temp, Games,
and a Mac's system folders).

Every displayed number comes from those fake files: the report XMLs are counted from the same
rule-engine classification the indexer uses, so per-drive Diff is +0 except for the upload queue,
the type categories add up to the backed total, and the file-id history is several times the live
file count the way Backblaze's own is.

``index`` runs the real indexer. ``trim`` drops what only indexing reads (per-day upload records;
file-list bodies too unless ``keep_lists``, which a demo needs so switching folders rebuilds).
"""

from __future__ import annotations

import datetime as _dt
import os
import random

from . import fakenames
from .applog import get_logger

log = get_logger("demobz")

BZ_RECORD = {"id": "bz", "name": "Backblaze", "short_name": "Backblaze", "kind": "backblaze"}
_DAY = 86400.0
_EXCLUDED_TYPES = "sys,tmp,lck,iso,dmg,vhd,vhdx,vmdk,swapfile"
_CATEGORY = {  # filestats.xml buckets
    "docs": {"doc", "docx", "pdf", "txt", "md", "odt", "rtf", "epub"},
    "spreadsheets": {"xls", "xlsx", "csv", "ods"},
    "presentations": {"ppt", "pptx", "odp"},
    "pict": {"jpg", "jpeg", "png", "gif", "heic", "dng", "svg", "webp", "bmp", "tif"},
    "music": {"mp3", "wav", "flac", "ogg", "m4a", "aac", "mid"},
    "video": {"mp4", "mkv", "mov", "avi", "webm"},
    "zips": {"zip", "7z", "rar", "gz", "tar", "msi", "exe"},
}
_SAFE_EXTS = {c: sorted(e for e in exts if e not in _EXCLUDED_TYPES.split(","))
              for c, exts in fakenames.CATEGORY_EXTS.items()}

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


def _stamp(t: float) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%Y%m%d%H%M%S")


def _gmt_day(t: float) -> str:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y%m%d")


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


def _sizes(count: int, total: int, r: random.Random) -> list[int]:
    """`count` file sizes adding up to `total`, clustered around the average."""
    if count <= 0:
        return []
    if count == 1:
        return [max(1, int(total))]
    mean = max(1.0, total / count)
    out = [max(1, int(r.uniform(0.2, 1.8) * mean)) for _ in range(count)]
    scale = total / max(1, sum(out))
    out = [max(1, int(x * scale)) for x in out]
    diff = total - sum(out)
    if diff:
        out[-1] = max(1, out[-1] + diff)
    return out


def _template_name(r: random.Random, cat: str) -> str:
    tpl = r.choice(fakenames.FILE_TEMPLATES.get(cat) or fakenames.FILE_TEMPLATES["Mixed"])
    year = r.randint(2019, 2025)
    name = tpl.format(n4=f"{r.randint(1, 9999):04d}", n3=f"{r.randint(1, 999):03d}",
                      n2=f"{r.randint(1, 99):02d}", date=f"{year}-{r.randint(1, 12):02d}",
                      yr=year)
    exts = _SAFE_EXTS.get(cat) or _SAFE_EXTS["Documents"]
    return name + "." + r.choice(exts)


def _folder_pool(platform: str, drive: str, user: str, r: random.Random) -> list[tuple[str, str]]:
    """[(category, folder)] for the synthetic everyday files on this drive."""
    out, seen = [], set()
    categories = ["Photos", "Videos", "Music", "Books", "Code", "Archives", "Documents", "Mixed"]
    for _ in range(180):
        cat = r.choice(categories)
        base = r.choice(fakenames.FOLDER_WORDS[cat])
        qual = r.choice(fakenames.QUALIFIERS)
        if platform == "windows":
            if drive == "C":
                top = "C:" + chr(92) + "Users" + chr(92) + user + chr(92) + r.choice(
                    ["Documents", "Downloads", "Pictures", "Videos", "Music", "Desktop", "Projects"])
            else:
                top = f"{drive}:" + chr(92) + base + qual
            folder = top
            if r.random() < 0.35:
                folder += chr(92) + r.choice(fakenames.FOLDER_WORDS["Mixed"])
            if r.random() < 0.5:
                folder += chr(92) + base + qual
        else:
            top = ("/Users/" + user + "/" + r.choice(
                ["Documents", "Pictures", "Movies", "Music", "Desktop", "Projects"])
                   if drive == "root" else f"/Volumes/{drive}/{base}{qual}")
            folder = top
            if r.random() < 0.35:
                folder += "/" + r.choice(fakenames.FOLDER_WORDS["Mixed"])
            if r.random() < 0.5:
                folder += "/" + base + qual
        if folder not in seen:
            seen.add(folder)
            out.append((cat, folder))
    return out


def _area_pool(platform: str, drive: str, user: str, r: random.Random) -> list[tuple[str, str]]:
    """[(kind, folder)] for excluded files on this drive. The paths sit under the rules below."""
    sep = chr(92) if platform == "windows" else "/"
    if platform == "windows":
        if drive == "C":
            return [("win", "C:\\Windows\\System32"), ("win", "C:\\Windows\\WinSxS"),
                    ("pf", "C:\\Program Files"), ("pf", "C:\\Program Files (x86)"),
                    ("pd", "C:\\ProgramData"),
                    ("appdata", "C:\\Users\\" + user + "\\AppData\\Local"),
                    ("games", "C:\\Games"), ("recycle", "C:\\$Recycle.Bin")]
        return [("games", f"{drive}:\\Games"), ("recycle", f"{drive}:\\$Recycle.Bin"),
                ("svi", f"{drive}:\\System Volume Information")]
    if drive == "root":
        return [("system", "/System/Library"), ("system", "/System/Volumes"),
                ("library", "/Library/Application Support"), ("apps", "/Applications"),
                ("tmp", "/private/var/tmp"),
                ("cache", "/Users/" + user + "/Library/Caches"),
                ("trash", "/Users/" + user + "/.Trash"),
                ("spot", "/.Spotlight-V100"), ("private", "/private/var/db")]
    return [("spot", f"/Volumes/{drive}/.Spotlight-V100"),
            ("cache", f"/Volumes/{drive}/.Trashes")]


def _excluded_name(kind: str, r: random.Random) -> str:
    if kind == "win":
        stem = r.choice(["ntoskrnl", "win32k", "kernel32", "ntdll", "apphelp", "sechost",
                         "msvcrt", "shell32", "user32", "gdi32", "combase", "bcrypt"])
        return f"{stem}{r.randint(1, 9999)}." + r.choice(["dll", "sys", "mui", "cat"])
    if kind == "pf":
        return f"{r.choice(['lib', 'module', 'plugin', 'app', 'helper'])}{r.randint(1, 999)}." + r.choice(
            ["dll", "exe", "dat", "pak", "json", "msi"])
    if kind == "pd":
        return f"{r.choice(['cache', 'settings', 'store', 'meta', 'state'])}_{r.getrandbits(32):08x}." + r.choice(
            ["dat", "db", "json", "log", "bak"])
    if kind == "appdata":
        return f"{r.choice(['cache', 'index', 'session', 'blob'])}_{r.getrandbits(32):08x}." + r.choice(
            ["dat", "db", "tmp", "bin", "log"])
    if kind == "games":
        return r.choice(["pak0.pak", f"data_{r.randint(0, 99)}.bin", f"shader_{r.randint(1, 500)}.cache",
                         f"audio_{r.randint(1, 99)}.bank", f"level_{r.randint(1, 80)}.map",
                         f"asset_{r.getrandbits(24):06x}.pak"])
    if kind in ("recycle", "svi"):
        return f"{r.getrandbits(32):08x}_{r.randint(1, 999)}." + r.choice(["tmp", "dat", "bin", "vhd"])
    if kind == "system":
        return f"{r.choice(['libSystem', 'libobjc', 'libc++', 'CoreFoundation', 'IOKit'])}_{r.randint(1, 999)}.dylib"
    if kind == "library":
        return f"{r.choice(['cache', 'index', 'store', 'settings'])}.{r.choice(['db', 'plist', 'dat', 'json'])}"
    if kind == "apps":
        return f"{r.choice(['Assets', 'Resources', 'Frameworks'])}/{r.choice(['data', 'config', 'index'])}.{r.choice(['dat', 'plist', 'pak'])}"
    if kind == "tmp":
        return f"tmp_{r.getrandbits(32):08x}.{r.choice(['tmp', 'log', 'dat'])}"
    if kind == "cache":
        return f"{r.choice(['com.apple', 'com.google', 'org.mozilla'])}.{r.getrandbits(24):06x}." + r.choice(
            ["cache", "db", "tmp", "dat"])
    if kind == "trash":
        return f"{r.choice(['IMG', 'DOC', 'MOV', 'backup'])}-{r.randint(1, 9999)}.{r.choice(['jpg', 'pdf', 'mov', 'zip'])}"
    if kind == "spot":
        return f"store_{r.getrandbits(32):08x}.db"
    return f"private_{r.getrandbits(32):08x}.{r.choice(['db', 'dat', 'log'])}"


def _fixed_excluded(platform: str, drive: str, total: int, r: random.Random) -> list[tuple[str, int]]:
    """A few big, rule-excluded files (pagefile, volume shadow, game data) so the bytes look real."""
    if platform == "windows":
        if drive == "C":
            return [("C:\\pagefile.sys", 16 * 1024**3), ("C:\\hiberfil.sys", 12 * 1024**3),
                    ("C:\\swapfile.sys", 4 * 1024**3), ("C:\\Windows\\MEMORY.DMP", 3 * 1024**3)]
        if drive in ("G", "H"):
            n = 3
            per = max(1, int(total * 0.12 / n))
            return [(f"{drive}:\\Games\\Archived Game {i + 1}\\data{chr(92)}game_data.bin", per)
                    for i in range(n)]
        n = 2
        per = max(1, int(total * 0.10 / n))
        return [(f"{drive}:\\System Volume Information\\{{guid}}\\shadow{i}.bin", per) for i in range(n)]
    if drive == "root":
        return [("/private/var/vm/swapfile0", 8 * 1024**3),
                ("/private/var/vm/sleepimage", 4 * 1024**3),
                ("/System/Library/Caches/com.apple.coresymbolicationd/data", 3 * 1024**3)]
    n = 2
    per = max(1, int(total * 0.12 / n))
    return [(f"/Volumes/{drive}/.Spotlight-V100/shadow{i}.db", per) for i in range(n)]


def _pick_kopia(by_drive: dict, drive: str, target: int, engine, mount: str, r: random.Random):
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


def _make_backed(platform: str, drive: str, target: int, target_bytes: int,
                 selected: list, user: str, r: random.Random) -> list[tuple[str, int, float]]:
    """Selected demo files + synthetic everyday files, reaching the target count and size."""
    out = [(p, int(s), float(m)) for p, s, m in selected]
    seen = {p.lower() for p, _s, _m in out}
    extra = max(0, target - len(out))
    extra_bytes = max(1, target_bytes - sum(f[1] for f in out))
    folders = _folder_pool(platform, drive, user, r)
    sep = chr(92) if platform == "windows" else "/"
    for i, size in enumerate(_sizes(extra, extra_bytes, r)):
        cat, folder = r.choice(folders)
        if size > 300_000_000:   # a big file gets a big-media name
            cat = r.choice(["Videos", "Archives"])
        path = folder + sep + _template_name(r, cat)
        if path.lower() in seen:
            base, dot, ext = path.rpartition(".")
            path = f"{base}_{i:06d}.{ext}" if dot else f"{path}_{i:06d}"
        while path.lower() in seen:
            path += "_"
        seen.add(path.lower())
        out.append((path, size, (r.uniform(0, 900) * _DAY)))
    return out


def _make_excluded(platform: str, drive: str, user: str, count: int, total_bytes: int,
                   r: random.Random):
    """Yield (path, size, age_seconds) for excluded files, from this drive's excluded areas."""
    fixed = _fixed_excluded(platform, drive, total_bytes, r)
    remaining = max(0, int(total_bytes) - sum(s for _p, s in fixed))
    sizes = _sizes(max(0, count - len(fixed)), remaining, r)
    areas = _area_pool(platform, drive, user, r)
    sep = chr(92) if platform == "windows" else "/"
    seen = set()
    def unique(path: str) -> str:
        if path.lower() not in seen:
            seen.add(path.lower())
            return path
        base, dot, ext = path.rpartition(".")
        p2 = (f"{base}_{len(seen):07d}.{ext}" if dot else f"{path}_{len(seen):07d}")
        while p2.lower() in seen:
            p2 += "_"
        seen.add(p2.lower())
        return p2
    for path, size in fixed:
        seen.add(path.lower())
        yield path, size, r.uniform(10, 180) * _DAY
    for size in sizes:
        kind, folder = r.choice(areas)
        yield unique(folder + sep + _excluded_name(kind, r)), size, r.uniform(1, 1200) * _DAY


def _rule(attrs: str = "", *, plat: str = "win", optional: bool = False) -> str:
    base = {"plat": plat, "osVers": "*", "ruleIsOptional": "t" if optional else "f",
            "skipFirstCharThenStartsWith": "*", "contains_1": "*", "contains_2": "*",
            "doesNotContain": "*", "endsWith": "*", "hasFileExtension": "*"}
    if attrs:
        for kv in attrs.split("|"):
            k, _, v = kv.partition("=")
            base[k] = v
    return "<excludefname_rule " + " ".join(f'{k}="{v}"' for k, v in base.items()) + " />"


def _rules(root: str, platform: str, user: str, host: str) -> None:
    if platform == "windows":
        mand = [
            _rule("skipFirstCharThenStartsWith=:\\windows\\"),
            _rule("skipFirstCharThenStartsWith=:\\program files\\"),
            _rule("skipFirstCharThenStartsWith=:\\program files (x86)\\"),
            _rule("skipFirstCharThenStartsWith=:\\programdata\\"),
            _rule("skipFirstCharThenStartsWith=:\\$recycle.bin\\"),
            _rule("skipFirstCharThenStartsWith=:\\system volume information\\"),
            _rule("skipFirstCharThenStartsWith=:\\games\\"),
            _rule("hasFileExtension=sys"),
        ]
        edit = [
            _rule("contains_1=\\appdata\\local\\"),
            _rule("contains_1=\\node_modules\\.cache\\", optional=True),
            _rule("endsWith=.tmp", optional=True),
        ]
        info = (f'<myidentity online_hostname="{host}" bzlogin="{user}@example.com" />'
                f'<globalexcludes excludefiletypes="{_EXCLUDED_TYPES}" />'
                f'<do_backup><bzdirfilter dir="\\Games\\" whichfiles="none" /></do_backup>')
    else:
        mand = [
            _rule("skipFirstCharThenStartsWith=private/var/tmp/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=.spotlight-v100/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=.trashes/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=.fseventsd/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=system/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=library/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=applications/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=usr/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=bin/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=sbin/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=cores/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=private/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=users/|contains_1=/library/caches/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=users/|contains_1=/.trash/", plat="mac"),
            _rule("skipFirstCharThenStartsWith=users/|contains_1=/library/saved application state/", plat="mac"),
        ]
        edit = [_rule("contains_1=/node_modules/.cache/", plat="mac", optional=True)]
        info = (f'<myidentity online_hostname="{host}" bzlogin="{user}@example.com" />'
                f'<globalexcludes excludefiletypes="{_EXCLUDED_TYPES}" />'
                f'<do_backup><bzdirfilter dir="/Users/{user}/Downloads/" whichfiles="none" /></do_backup>')
    _xml(root, "bzexcluderules_mandatory.xml", "bzexclusions", ["<!-- Mandatory Exclusions -->"] + mand)
    _xml(root, "bzexcluderules_editable.xml", "bzexclusions", ["<!-- Editable Exclusions -->"] + edit)
    _xml(root, "bzinfo.xml", "bzinfo", [info])


def _xml(root: str, rel: str, top: str, lines: list[str]) -> None:
    fp = os.path.join(root, rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'<?xml version="1.0" encoding="utf-8" ?>\n<{top}>\n')
        fh.write("\n".join(lines))
        fh.write(f"\n</{top}>\n")


def _category_of(path: str) -> str:
    name = path.rsplit(chr(92), 1)[-1] if chr(92) in path else path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for cat, exts in _CATEGORY.items():
        if ext in exts:
            return cat
    return "other"


def _filestats(root: str, files: list, host: str, now: float, platform: str) -> None:
    acc = {c: [0, 0] for c in (*_CATEGORY, "financial", "email", "browserfavorites", "other", "everything")}
    for path, size, _m in files:
        cat = _category_of(path)
        for c in (cat, "everything"):
            acc[c][0] += 1
            acc[c][1] += size
    # "other" = everything that is in no named bucket
    known = sum(acc[c][0] for c in _CATEGORY)
    acc["other"] = [acc["everything"][0] - known, acc["everything"][1] - sum(acc[c][1] for c in _CATEGORY)]
    attrs = " ".join(f'{c}num="{n}" {c}totbytes="{b}"' for c, (n, b) in acc.items())
    _xml(root, os.path.join("bzfilelists", "filestats.xml"), "filestats",
         ['<progress good_for_installer="true" totally_final="true" />',
          f'<info datetime="{_stamp(now)}" aonlinename="{host}" plat="{"mac" if platform == "mac" else "win64"}" '
          f'osdetail="{"macOS" if platform == "mac" else "Windows11"}" {attrs} />'])


def _history(root: str, backed: list, queued: list, now: float, r: random.Random) -> None:
    """bzfileids (cumulative, ~3.7x the live files), the upload queue, 45 days of bz_done records,
    the event log, and the upload-success history. All from the same fake files."""
    dc = os.path.join(root, "bzbackup", "bzdatacenter")
    os.makedirs(dc, exist_ok=True)
    seq = 0x1000000000000000
    current = sorted(backed, key=lambda f: f[2])
    target_total = max(len(current), int(len(backed) * 3.7))
    hist = max(0, target_total - len(current))
    with open(os.path.join(root, "bzbackup", "bzfileids.dat"), "w", encoding="utf-8", newline="\n") as fh:
        if current:
            for i in range(hist):
                p = current[i % len(current)][0]
                suffix = r.choice([f" (old {r.randint(1, 9)})", f"_{r.randint(2019, 2024)}",
                                   f" (copy {r.randint(1, 9)})", " (1)"])
                base, dot, ext = p.rpartition(".")
                hist_path = (f"{base}{suffix}.{ext}" if dot and not any(c in ext for c in "/\\") else p + suffix)
                fh.write(f"{seq + i:016x}\t{hist_path}\n")
        for j, (p, _s, _m) in enumerate(current):
            fh.write(f"{seq + hist + j:016x}\t{p}\n")
    with open(os.path.join(dc, f"bz_todo_{int(now * 1000)}_0.dat"), "w", encoding="utf-8", newline="\n") as fh:
        for p, s, m in queued:
            fh.write(f"1\t+\t{r.getrandbits(64):016x}\t{int(m * 1000):016x}\t{s}\t{p}\n")
    events, upload_days = [], []
    for d in range(45, 0, -1):
        day_start = now - d * _DAY
        start = day_start + r.uniform(8, 20) * 3600
        todays = [f for f in backed if day_start <= f[2] < day_start + _DAY]
        if not todays and r.random() < 0.6:
            todays = r.sample(backed, min(len(backed), r.randint(5, 60)))
        name = f"bz_done_{int(start * 1000)}_0.dat"
        with open(os.path.join(dc, name), "w", encoding="utf-8", newline="\n") as fh:
            for path, size, _m in todays:
                t = start + r.uniform(0, 1800)
                fh.write("\t".join(["5", "-", "---", _stamp(t), "f_" + f"{r.getrandbits(80):020x}", "n--",
                                    f"{size:016x}", "n-_a-----", "-" * 20, "-" * 16, "-" * 16, "-", "0",
                                    path]) + "\n")
        os.utime(os.path.join(dc, name), (start + 1800, start + 1800))
        mb = sum(f[1] for f in todays) // 1_000_000
        events.append(f"{_dt.datetime.fromtimestamp(start):%Y-%m-%d %H:%M:%S} - STARTBACKUP")
        if r.random() < 0.35:
            end = start + r.uniform(300, 3000)
            events.append(f"{_dt.datetime.fromtimestamp(end):%Y-%m-%d %H:%M:%S} - END_BACKUP: "
                          f"Backed up {len(todays)} FILES / {mb} MB")
        upload_days.append(f'<one_upload_success_stat date_gmt="{_gmt_day(start)}" '
                           f'num_upload_success="{len(todays)}" num_upload_fail_CvtTooBusy="{r.choice([0, 0, 0, 1])}" '
                           f'num_upload_fail_CvtNoRoom="0" num_upload_fail_UnknownReason="0" />')
    os.makedirs(os.path.join(root, "bzlogs", "bzreports_eventlog"), exist_ok=True)
    with open(os.path.join(root, "bzlogs", "bzreports_eventlog", "01.log"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("\n".join(events) + "\n")
    _xml(root, os.path.join("bzreports", "bzstat_upload_success.xml"), "upload_success_history", upload_days)
    _xml(root, os.path.join("bzreports", "zbytes_history.xml"), "zbytes_history",
         [f'<one_zbyte_stat date_time_written_gmt="{_stamp(now - d * _DAY)}" '
          f'last_rollback_instr="none{_stamp(now - d * _DAY)}_ok000a" num_z_lines="0" tot_bytes_on_z_lines="0" />'
          for d in range(10, 0, -1)])


def write(root: str, files: list[tuple[str, int, float]], *, user: str, host: str,
          now: float | None = None, seed: int = 1, platform: str = "windows",
          profile=None) -> dict:
    """Write ``root`` (a ``bzdata`` folder) for the targets of ``profile`` (None = full scale)."""
    from ..backblaze import paths, rules
    r = random.Random(f"bz-{seed}-{platform}")
    now = now or _dt.datetime.now().timestamp()
    targets = _targets(profile, platform)
    move = profile is None
    if move:
        targets = {d: {k: _moved(v, r) for k, v in t.items()} for d, t in targets.items()}
    for sub in ("bzfilelists", "bzreports", os.path.join("bzbackup", "bzdatacenter"),
                os.path.join("bzlogs", "bzreports_eventlog"), os.path.join("bzlogs", "bzfilelist"),
                "bzupdates"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)

    paths.set_bzdata_root(root)
    _rules(root, platform, user, host)
    engine = rules.load_engine("mac" if platform == "mac" else "win")

    by_drive = {}
    if platform == "windows" and files:
        for f in files:
            p = f[0]
            if len(p) > 2 and p[1] == ":" and p[0].upper() in targets:
                by_drive.setdefault(p[0].upper(), {}).setdefault(p.lower(), (p, int(f[1]), float(f[2])))

    vols, packed = [], {}
    all_backed, all_queued = [], []
    grand_uploaded = [0, 0]
    for drive in sorted(targets):
        t = targets[drive]
        if platform == "windows":
            key, mount = drive, f"{drive}:\\"
            token = drive.lower()
        else:
            key = "root" if drive == "root" else drive
            mount = "/" if drive == "root" else f"/Volumes/{drive}"
            token = "root" if drive == "root" else drive
        guid = "v" + f"{r.getrandbits(92):023x}"
        selected = _pick_kopia(by_drive, drive, t["backed"], engine, mount, r) if by_drive else []
        backed = _make_backed(platform, key, t["backed"], t["backed_bytes"], selected, user, r)
        # Keep the generated files off the rule-excluded paths, then fill any shortfall.
        safe = []
        for p, s, m in backed:
            if not engine.classify(p, mount)[0]:
                safe.append((p, s, m))
        if platform == "windows":
            fallback = (("C:" + chr(92) + "Users" + chr(92) + user + chr(92) + "Documents")
                        if key == "C" else f"{key}:" + chr(92) + "Documents")
        else:
            fallback = (f"/Users/{user}/Documents" if key == "root" else f"/Volumes/{key}/Documents")
        sep = chr(92) if platform == "windows" else "/"
        seen_safe = {p.lower() for p, _s, _m in safe}
        tries = 0
        while len(safe) < t["backed"] and tries < t["backed"] * 5 + 100:
            tries += 1
            cat, folder = r.choice(_folder_pool(platform, key, user, r))
            p = folder + sep + _template_name(r, cat)
            if engine.classify(p, mount)[0] or p.lower() in seen_safe:
                p = fallback + sep + f"file_{len(safe):06d}.dat"
            while p.lower() in seen_safe:
                p = fallback + sep + f"file_{len(safe):06d}_{tries}.dat"
            if not engine.classify(p, mount)[0]:
                seen_safe.add(p.lower())
                safe.append((p, max(1, t["backed_bytes"] // max(1, t["backed"])), now - r.uniform(0, 900) * _DAY))
        while len(safe) < t["backed"]:   # the fallback folder can never be excluded
            safe.append((fallback + (chr(92) if platform == "windows" else "/") + f"file_extra_{len(safe):06d}.dat",
                         max(1, t["backed_bytes"] // max(1, t["backed"])), now - r.uniform(0, 900) * _DAY))
        backed = safe[:t["backed"]]
        # A small upload queue, as Backblaze always has: those files are in the list but not counted.
        qn = min(len(backed), max(3, int(len(backed) * 0.004))) if profile != "test" else 2
        queued = sorted(backed, key=lambda f: -f[2])[:qn]
        queued_set = {p for p, _s, _m in queued}
        uploaded = [f for f in backed if f[0] not in queued_set]
        excluded_count = t["excluded"]
        excluded_bytes = t["excluded_bytes"]
        sep = chr(92) if platform == "windows" else "/"
        fp = os.path.join(root, "bzfilelists",
                          f"{guid}_{token.lower()}____filelist.dat" if platform == "windows"
                          else f"{guid}_{token}_filelist.dat")
        count = 0
        with open(fp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"# GmtMillisThisListWasStarted {int(now * 1000)}\n#\n# Dir: {mount}\n")
            for path, size, mtime in backed:
                fh.write(f"f\t{int(mtime * 1000):x}\t{size}\t{path}\n")
                count += 1
            for path, size, age in _make_excluded(platform, key, user, excluded_count, excluded_bytes, r):
                fh.write(f"f\t{int((now - age) * 1000):x}\t{size}\t{path}\n")
                count += 1
            if count < 20:
                fh.write("# " + "x" * 1200 + "\n")   # never read as an aborted (stub) scan
        vols.append((guid, mount, key))
        packed[key] = {"guid": guid, "backed": backed, "uploaded": uploaded, "queued": queued,
                       "backed_bytes": sum(f[1] for f in uploaded),
                       "queued_bytes": sum(f[1] for f in queued),
                       "excluded_bytes": int(excluded_bytes), "excluded": excluded_count}
        all_backed += backed
        all_queued += queued
        grand_uploaded[0] += len(uploaded)
        grand_uploaded[1] += sum(f[1] for f in uploaded)

    used = {k: v["backed_bytes"] + v["queued_bytes"] + v["excluded_bytes"] for k, v in packed.items()}
    _volumes(root, vols, now, used)
    _reports(root, packed, grand_uploaded, now, host, platform)
    _filestats(root, [f for k in packed for f in packed[k]["uploaded"]], host, now, platform)
    _history(root, all_backed, all_queued, now, r)
    _versions(root, platform)
    nfiles = sum(len(v["uploaded"]) for v in packed.values())
    nbytes = sum(v["backed_bytes"] for v in packed.values())
    log.info("demo bzdata: %d drives, %d backed up (%d bytes), platform %s", len(packed), nfiles, nbytes, platform)
    return {"drives": sorted(packed), "files": len(all_backed), "backed": nfiles, "bytes": nbytes,
            "platform": platform}


def _volumes(root: str, vols: list, now: float, used: dict) -> None:
    lines = [f'<bzvol_metadata lastAssignedBzVolShortId="{len(vols)}" '
             f'gmtMillisThisFileWasWritten="{int(now * 1000)}" gmtDateTimeFileWasWritten="{_stamp(now)}" />']
    for guid, mount, key in vols:
        u = int(used.get(key, 0))
        total = max(u * 3, 500 * 10**9)
        lines.append(f'<bzvolume bzVolumeGuid="{guid}" mountPointPathHex="{_hex(mount)}" '
                     f'typeOfVolumeTwoCharCode="hd" lastTimeVolumeWasSeenAttachedGmtMillis="{int(now * 1000)}" '
                     f'numBytesFreeOnVolume="{total - u}" numBytesUsedOnVolume="{u}" '
                     f'numBytesTotalOnVolume="{total}" />')
    _xml(root, "bzvolumes.xml", "contents", lines)


def _reports(root: str, packed: dict, grand_uploaded: tuple, now: float, host: str, platform: str) -> None:
    totals, rem = [], []
    for key, v in packed.items():
        totals.append(f'<bzvolume bzVolumeGuid="{v["guid"]}" pervol_sel_for_backup_numfiles="{len(v["uploaded"])}" '
                      f'pervol_sel_for_backup_numbytes="{v["backed_bytes"]}" />')
        rem.append(f'<bzvolume bzVolumeGuid="{v["guid"]}" pervol_remaining_files_numfiles="{len(v["queued"])}" '
                   f'pervol_remaining_files_numbytes="{v["queued_bytes"]}" />')
    _xml(root, os.path.join("bzreports", "bzstat_totalbackup.xml"), "contents",
         [f'<totals totnumfilesforbackup="{grand_uploaded[0]}" totnumbytesforbackup="{grand_uploaded[1]}" />'] + totals)
    _xml(root, os.path.join("bzreports", "bzstat_remainingbackup.xml"), "contents",
         [f'<remaining remainingnumfilesforbackup="{sum(len(v["queued"]) for v in packed.values())}" '
          f'remainingnumbytesforbackup="{sum(v["queued_bytes"] for v in packed.values())}" />'] + rem)
    last = now - 600
    _xml(root, os.path.join("bzreports", "bzstat_lastbackupcompleted.xml"), "contents",
         [f'<lastbackupcompleted gmt_millis="{int(last * 1000)}" localdatetime="{_stamp(last)}" />'])
    _xml(root, "overviewstatus.xml", "status",
         [f'<bztransmit cur_state="idle_waiting" current_file="none" current_file_fullpath="none" '
          f'millisFileWasWritten="{int(now * 1000)}" />'])


def _versions(root: str, platform: str) -> None:
    win, mac = "10.0.4.1081", "10.0.4.1082"
    _xml(root, os.path.join("bzupdates", "clientversion.xml"), "clientversion",
         [f'<release win32_version="{win}" mac_version="{mac}" />',
          f'<autoupdate win32_version="9.2.2.897" mac_version="9.2.2.898" />'])
    os.makedirs(os.path.join(root, "bzlogs", "bzfilelist"), exist_ok=True)
    with open(os.path.join(root, "bzlogs", "bzfilelist", "bzfilelist01.log"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(f"2026-11-10 00:00:00 - bzfilelist starting, version={mac if platform == 'mac' else win} "
                 f"os={'mac' if platform == 'mac' else 'win'}\n")


def trim(root: str, keep_lists: bool = False) -> int:
    """After indexing, drop what only the indexer reads: per-day upload records and all but the tail
    of the file ids. ``keep_lists`` keeps the per-drive file-list bodies, so switching to another
    folder and back can rebuild (a demo folder needs that). Returns bytes freed."""
    from ..backblaze.queries import _CHURN_TAIL
    freed = 0
    # The indexer has already counted the full file-id file; keep that number for a later rebuild
    # after this trim (a demo can switch folders and rebuild from the same read-only data).
    total = 0
    try:
        from ..backblaze import bbdb
        total = int(bbdb.kv_get(bbdb.connect(), "fileids_total") or 0)
    except Exception:  # noqa: BLE001 - count the lines below instead
        total = 0
    ids_path = os.path.join(root, "bzbackup", "bzfileids.dat")
    if not total and os.path.exists(ids_path):
        with open(ids_path, encoding="utf-8", errors="replace") as fh:
            total = sum(1 for _ in fh)
    if total:
        with open(os.path.join(root, "bzbackup", "bzfileids_total.txt"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(str(total))
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
    if size > _CHURN_TAIL:
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
