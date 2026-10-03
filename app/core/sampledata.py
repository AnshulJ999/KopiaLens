"""Sample data: a made-up repository shaped like real ones, indexed by the real indexer.

``Sample`` builds sources, folder trees and a snapshot history from ``sample_profile.json``
(numbers only, see ``sampleprofile``) and a seed. Its ``answer`` plays kopia for that model:
the snapshot list, folder and split-file objects, the content list, policies, status and
stats. During a build it is the demo replies backend, so the real worker writes every table
and cache; afterwards the read commands are saved as an ordinary demo repo's replies.

Names come only from the word lists below and in ``fakenames``; ``check_names`` refuses a
build holding any other word.

    python -m app.core.sampledata [--files N] [--seed N] [--out DIR]
"""

from __future__ import annotations

import bisect
import datetime as _dt
import hashlib
import json
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

from config import APP_VERSION, CACHEDIR, DATA_DIR, HERE, SELF_EXE

from . import demoreplies, fakenames
from .applog import get_logger
from .sampleprofile import PROFILE_PATH

log = get_logger("sampledata")

DEFAULT_FILES = 50_000
DEFAULT_SEED = 20261003
SAMPLE_DIR = "sample"
REPO_ID = "1"
_DAY = 86400.0
_MIB = 1 << 20
_SPLIT_ABOVE = 6 * _MIB          # kopia's DYNAMIC-4M splitter: larger files become split objects
_PACK_SIZE = 20 * _MIB
_MAX_DEPTH = 14

# Sources: (key, path, share of files, file categories, files-per-folder bin shift).
THEMES = (
    ("projects", r"D:\Projects", 0.50, None, 0),
    ("documents", r"C:\Users\{user}\Documents", 0.18, {"Documents", "Books"}, 0),
    ("photos", r"C:\Users\{user}\Pictures", 0.16, {"Photos", "Videos"}, 2),
    ("music", r"D:\Music", 0.09, {"Music"}, 1),
    ("downloads", r"C:\Users\{user}\Downloads", 0.07, {"Archives", "Documents", "Books"}, 1),
)
# Snapshot schedule per source: days between snapshots, history length, per-source retention.
SCHEDULE = {
    "projects": (1, 420, None),
    "documents": (1, 360, None),
    "photos": (7, 540, {"keepLatest": 10, "keepHourly": 0, "keepDaily": 0, "keepWeekly": 12,
                        "keepMonthly": 24, "keepAnnual": 3}),
    "music": (3, 300, None),
    "downloads": (1, 120, {"keepLatest": 5, "keepHourly": 0, "keepDaily": 7, "keepWeekly": 4,
                           "keepMonthly": 0, "keepAnnual": 0}),
}
_GLOBAL_RETENTION = {"keepLatest": 10, "keepHourly": 48, "keepDaily": 7, "keepWeekly": 4,
                     "keepMonthly": 24, "keepAnnual": 3}

# Types the profile may lack, per theme: (ext, weight, median bytes, log spread).
FALLBACK_TYPES = {
    "documents": [("docx", 0.22, 38_000, 1.0), ("xlsx", 0.12, 30_000, 1.1), ("pdf", 0.26, 420_000, 1.4),
                  ("txt", 0.12, 3_000, 1.2), ("csv", 0.07, 60_000, 1.5), ("pptx", 0.05, 2_400_000, 1.0),
                  ("odt", 0.03, 25_000, 0.9), ("epub", 0.05, 1_800_000, 0.8), ("md", 0.08, 4_000, 1.0)],
    "photos": [("jpg", 0.66, 3_200_000, 0.5), ("png", 0.12, 900_000, 1.0), ("heic", 0.12, 2_100_000, 0.4),
               ("dng", 0.04, 24_000_000, 0.3), ("mp4", 0.06, 45_000_000, 1.2)],
    "music": [("wav", 0.35, 38_000_000, 0.8), ("mp3", 0.30, 7_000_000, 0.4), ("flac", 0.20, 28_000_000, 0.4),
              ("ogg", 0.05, 5_000_000, 0.5), ("mid", 0.05, 30_000, 0.8), ("txt", 0.05, 2_000, 0.8)],
    "downloads": [("zip", 0.24, 6_000_000, 1.3), ("exe", 0.20, 9_000_000, 1.1), ("msi", 0.08, 20_000_000, 0.9),
                  ("pdf", 0.33, 1_200_000, 1.1), ("iso", 0.005, 3_100_000_000, 0.2), ("7z", 0.06, 15_000_000, 1.2),
                  ("jpg", 0.085, 1_500_000, 0.8)],
}
# Ratio of stored to original bytes when the profile has no figure for a type.
_PACKED = {"jpg": 1.0, "png": 0.99, "heic": 1.0, "dng": 0.9, "mp4": 1.0, "mp3": 1.0, "flac": 1.0,
           "ogg": 1.0, "wav": 0.82, "zip": 1.0, "7z": 1.0, "iso": 0.97, "exe": 0.6, "msi": 0.95,
           "pdf": 0.9, "docx": 0.98, "xlsx": 0.97, "pptx": 0.98, "epub": 0.97}

CODE_DIRS = ["src", "lib", "components", "utils", "assets", "tests", "docs", "scripts", "config",
             "models", "views", "static", "public", "images", "styles", "templates", "api", "core",
             "services", "handlers", "fixtures", "migrations", "locales", "icons", "data", "examples"]
CODE_ROOT_FILES = ["README.md", "LICENSE", "Makefile", "Dockerfile", ".gitignore", ".editorconfig",
                   "package.json", "requirements.txt", "pyproject.toml", "CHANGELOG.md"]
PACKAGES = ["react", "lodash", "express", "axios", "chalk", "debug", "uuid", "semver", "glob",
            "minimist", "commander", "yargs", "dotenv", "ws", "cors", "dayjs", "zod", "vite",
            "eslint", "typescript", "prettier", "jest", "rimraf", "mkdirp", "picocolors"]
PACKAGE_FILES = ["index", "cli", "utils", "helpers", "parse", "format", "core", "types", "main",
                 "browser", "node", "esm", "compat", "errors", "constants", "options"]
PHOTO_EVENTS = ["Trip", "Family", "Holiday", "Wedding", "Birthday", "Garden", "Beach", "Hike",
                "Concert", "Graduation", "Weekend", "Road Trip", "City Walk", "New Year"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
MUSIC_DIRS = ["Albums", "Mixes", "Stems", "Samples", "Recordings", "Demos", "Live Sets", "Loops",
              "Projects", "Masters", "Drafts"]
DOWNLOAD_DIRS = ["Installers", "Drivers", "Manuals", "Wallpapers", "Fonts", "Archives", "Receipts",
                 "Tools", "Setup Files", "Papers"]
_IGNORE_TEXT = "# Not worth backing up\n*.log\n*.tmp\ncoverage/\n.cache/\n"
_HOSTS = ["DESKTOP-4QK7M2P", "DESKTOP-9TR3XW8", "STUDIO-PC", "WORKSTATION-01", "HOME-PC"]


def load_profile(path: str = PROFILE_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _hex(rng: random.Random, bits: int = 128) -> str:
    return f"{rng.getrandbits(bits):0{bits // 4}x}"


def _ts(t: float) -> str:
    """kopia's timestamp: UTC with 7 fractional digits, as written on Windows."""
    d = _dt.datetime.fromtimestamp(t, _dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S") + f".{d.microsecond * 10:07d}Z"


def _from_log2_bin(rng: random.Random, b: int) -> int:
    return 0 if b <= 0 else rng.randint(1 << (b - 1), (1 << b) - 1)


class _Hist:
    """Weighted draws from a ``{bin: share}`` histogram."""

    def __init__(self, hist: dict[str, float], shift: int = 0):
        self.bins = [int(k) + (shift if int(k) > 0 else 0) for k in hist]
        cum, total = [], 0.0
        for v in hist.values():
            total += v
            cum.append(total)
        self.cum, self.total = cum, total

    def draw(self, rng: random.Random) -> int:
        return self.bins[min(bisect.bisect_left(self.cum, rng.random() * self.total), len(self.bins) - 1)]


class _SizeDist:
    """Inverse CDF through the profile's quantiles, log-linear between knots."""

    def __init__(self, quants: list[float], sizes: list[int]):
        lo, hi = max(sizes[0], 1), max(sizes[-1], 1)
        self.u = [0.0, *quants, 1.0]
        self.v = [math.log(max(lo / 4, 1))] + [math.log(max(s, 1)) for s in sizes] + [math.log(hi * 4)]

    @classmethod
    def lognormal(cls, median: float, spread: float, quants: list[float]) -> "_SizeDist":
        z = {0.05: -1.645, 0.25: -0.674, 0.5: 0.0, 0.75: 0.674, 0.95: 1.645, 0.99: 2.326}
        return cls(quants, [int(median * math.exp(spread * z[q])) for q in quants])

    def draw(self, rng: random.Random) -> int:
        u = rng.random()
        i = min(bisect.bisect_right(self.u, u), len(self.u) - 1)
        u0, u1, v0, v1 = self.u[i - 1], self.u[i], self.v[i - 1], self.v[i]
        frac = (u - u0) / (u1 - u0) if u1 > u0 else 0.0
        return int(math.exp(v0 + (v1 - v0) * frac))


class _Type:
    __slots__ = ("ext", "weight", "sizes", "packed")

    def __init__(self, ext: str, weight: float, sizes: _SizeDist, packed: float):
        self.ext, self.weight, self.sizes, self.packed = ext, weight, sizes, packed


class _File:
    __slots__ = ("name", "ext", "size", "mtime", "obj", "chunks", "heat", "parent", "idx", "packed")

    def __init__(self, name: str, ext: str, size: int, mtime: float, heat: float, packed: float):
        self.name, self.ext, self.size, self.mtime, self.heat = name, ext, size, mtime, heat
        self.packed = packed
        self.obj = ""
        self.chunks: list[tuple[str, int]] | None = None
        self.parent: _Dir | None = None
        self.idx = -1


class _Dir:
    __slots__ = ("name", "dirs", "files", "mtime", "parent", "depth", "cache", "idx", "kind")

    def __init__(self, name: str, parent: "_Dir | None", mtime: float, kind: str = ""):
        self.name, self.parent, self.mtime, self.kind = name, parent, mtime, kind
        self.depth = parent.depth + 1 if parent else 0
        self.dirs: dict[str, _Dir] = {}
        self.files: dict[str, _File] = {}
        self.cache: tuple[str, dict, float] | None = None
        self.idx = -1

    def dirty(self) -> None:
        d = self
        while d is not None and d.cache is not None:
            d.cache = None
            d = d.parent


class _Source:
    def __init__(self, key: str, path: str, user: str, host: str):
        self.key, self.path, self.user, self.host = key, path, user, host
        self.root: _Dir | None = None
        self.files: list[_File] = []
        self.dirs: list[_Dir] = []
        self.heated: list[_File] = []
        self.snapshots: list[dict] = []

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}:{self.path}"


def retention(times: list[float], policy: dict) -> dict[int, list[str]]:
    """kopia's retention over snapshot start times: index -> retention reasons. For each period
    the newest snapshot of each of the last N distinct periods is kept."""
    order = sorted(range(len(times)), key=lambda i: -times[i])
    keys = {
        "latest": lambda i: i,
        "hourly": lambda i: _dt.datetime.fromtimestamp(times[i], _dt.timezone.utc).strftime("%Y%m%d%H"),
        "daily": lambda i: _dt.datetime.fromtimestamp(times[i], _dt.timezone.utc).strftime("%Y%m%d"),
        "weekly": lambda i: "%d-%02d" % _dt.datetime.fromtimestamp(times[i], _dt.timezone.utc).isocalendar()[:2],
        "monthly": lambda i: _dt.datetime.fromtimestamp(times[i], _dt.timezone.utc).strftime("%Y%m"),
        "annual": lambda i: _dt.datetime.fromtimestamp(times[i], _dt.timezone.utc).strftime("%Y"),
    }
    keep: dict[int, list[str]] = {}
    for kind, fn in keys.items():
        limit = int(policy.get("keep" + kind.capitalize()) or 0)
        seen: list = []
        for i in order:
            if len(seen) >= limit:
                break
            k = fn(i)
            if k in seen:
                continue
            seen.append(k)
            keep.setdefault(i, []).append(f"{kind}-{len(seen)}")
    return keep


class Sample:
    """The whole made-up repository, generated up front from the profile and a seed."""

    def __init__(self, profile: dict, files: int = DEFAULT_FILES, seed: int = DEFAULT_SEED,
                 now: float | None = None, taken=None):
        self.p = profile
        self.rng = random.Random(seed)
        self.now = now if now is not None else _dt.datetime.now(_dt.timezone.utc).timestamp()
        self.identity = identity(seed, taken)
        self.objects: dict[str, str] = {}               # k... folders and x... split-object indexes
        self.contents: dict[str, tuple[int, int, float]] = {}   # id -> (original, packed, written)
        self.texts: dict[str, str] = {}                 # ignore-file contents, for `kopia show`
        self.quants = profile["quantiles"]
        self.files_hist = _Hist(profile["files_per_dir"])
        self.subdirs_hist = _Hist(profile["subdirs_per_dir"])
        self.dupe_hist = _Hist(profile["dupes"]["group_size"])
        self.age = _SizeDist(self.quants, [max(int(v * 86400), 60) for v in profile["mtime_age_days"]])
        self.types = self._types()
        self.packages: dict[str, list[tuple[str, int, str, float]]] = {}
        self.empty = _hex(self.rng)
        self.contents[self.empty] = (0, 0, self.now)
        user, host = self.identity["user"], self.identity["host"]
        self.sources = [_Source(k, path.format(user=user), user, host) for k, path, *_ in THEMES]
        for src, (key, _path, share, _cats, shift) in zip(self.sources, THEMES):
            self._grow(src, max(50, int(files * share)), shift)
        self.policies = self._policies()

    # ---- types and names ---------------------------------------------------------------

    def _types(self) -> dict[str, list[_Type]]:
        """Projects take the profile's whole type mix; the media and document sources use the
        built-in types (the profile's photos are mostly icons), with the profile's stored-size
        ratio where it has one."""
        prof = {t["ext"]: t for t in self.p["types"]}
        out: dict[str, list[_Type]] = {"projects": [
            _Type(t["ext"], t["share"], _SizeDist(self.quants, t["sizes"]), t["packed_ratio"])
            for t in self.p["types"]]}
        for key, items in FALLBACK_TYPES.items():
            out[key] = [_Type(ext, w, _SizeDist.lognormal(median, spread, self.quants),
                              prof[ext]["packed_ratio"] if ext in prof else _PACKED.get(ext, 0.6))
                        for ext, w, median, spread in items]
        return out

    def _pick_type(self, key: str) -> _Type:
        types = self.types[key]
        return self.rng.choices(types, weights=[t.weight for t in types])[0]

    def _fill(self, template: str, t: float) -> str:
        d = _dt.datetime.fromtimestamp(t, _dt.timezone.utc)
        r = self.rng
        return template.format(n2=f"{r.randint(1, 99):02d}", n3=f"{r.randint(1, 999):03d}",
                               n4=f"{r.randint(1, 9999):04d}", date=d.strftime("%Y%m%d"), yr=d.year)

    def _file_name(self, key: str, ext: str, t: float) -> str:
        r = self.rng
        if key == "projects" and ext == "":
            return r.choice(["LICENSE", "Makefile", "Dockerfile", "README", "Procfile", "VERSION",
                             "CODEOWNERS", "AUTHORS"])
        cat = fakenames.ext_category(ext) if ext else "Mixed"
        if key == "projects" and cat in ("Mixed", "Photos"):
            stem = r.choice(fakenames.FILE_TEMPLATES["Code"] + ["icon-{n2}", "logo", "banner", "bg-{n2}"])
        elif key == "photos":
            stem = r.choice(fakenames.FILE_TEMPLATES["Photos"])
        else:
            stem = r.choice(fakenames.FILE_TEMPLATES.get(cat) or fakenames.FILE_TEMPLATES["Mixed"])
        name = self._fill(stem, t)
        return f"{name}.{ext}" if ext else name

    def _folder_name(self, src: _Source, parent: _Dir, t: float) -> tuple[str, str]:
        """(name, kind) for a new folder; kind marks project roots."""
        r, key, depth = self.rng, src.key, parent.depth + 1
        q = r.choice(fakenames.QUALIFIERS)
        if key == "projects":
            if depth == 1:
                return r.choice(fakenames.PROJECT_WORDS) + r.choice(fakenames.PROJECT_SUFFIXES), "project"
            return r.choice(CODE_DIRS), ""
        if key == "photos":
            year = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).year
            if depth == 1:
                taken = set(parent.dirs)
                free = [str(y) for y in range(year - 12, year + 1) if str(y) not in taken]
                return (max(free) if free else str(year)), ""
            year -= r.randint(0, 6)
            if depth == 2:
                return f"{r.choice(MONTHS)} {r.choice(PHOTO_EVENTS)}", ""
            return r.choice(fakenames.FOLDER_WORDS["Photos"]), ""
        if key == "music":
            return r.choice(MUSIC_DIRS) + q, ""
        if key == "downloads":
            return r.choice(DOWNLOAD_DIRS) + q, ""
        words = fakenames.FOLDER_WORDS["Documents"] + fakenames.FOLDER_WORDS["Books"] + \
            fakenames.FOLDER_WORDS["Mixed"]
        return r.choice(words) + q, ""

    @staticmethod
    def _unique(names: dict, name: str) -> str:
        if name not in names:
            return name
        stem, dot, ext = name.rpartition(".") if "." in name[1:] else (name, "", "")
        n = 2
        while f"{stem} ({n}){dot}{ext}" in names:
            n += 1
        return f"{stem} ({n}){dot}{ext}"

    # ---- contents ----------------------------------------------------------------------

    def _store(self, cid: str, original: int, packed: int, t: float) -> None:
        if cid not in self.contents:
            self.contents[cid] = (original, packed, t)

    def _new_content(self, f: _File, t: float, keep_chunks: bool = False) -> None:
        """A fresh object for the file's current size; a changed split file keeps most pieces."""
        r = self.rng
        if f.size == 0:
            f.obj, f.chunks = self.empty, None
            return
        if f.size <= _SPLIT_ABOVE:
            cid = _hex(r)
            self._store(cid, f.size, max(32, int(f.size * f.packed * r.uniform(0.96, 1.02))), t)
            f.obj, f.chunks = cid, None
            return
        old = f.chunks if keep_chunks and f.chunks else None
        chunks: list[tuple[str, int]] = []
        left = f.size
        i = 0
        while left > 0:
            n = min(left, r.randint(2 * _MIB, 8 * _MIB))
            if old and i < len(old) and r.random() < 0.9 and old[i][1] == n:
                chunks.append(old[i])
            else:
                chunks.append((_hex(r), n))
                self._store(chunks[-1][0], n, max(32, int(n * f.packed * r.uniform(0.97, 1.01))), t)
            left -= n
            i += 1
        entries, off = [], 0
        for cid, n in chunks:
            entries.append({"l": n, "o": cid} if off == 0 else {"s": off, "l": n, "o": cid})
            off += n
        line = json.dumps({"stream": "kopia:indirect", "entries": entries}, separators=(",", ":"))
        xid = "x" + hashlib.blake2b(line.encode(), digest_size=16).hexdigest()
        if xid not in self.objects:
            self.objects[xid] = line
            self._store(xid, len(line), len(line) // 3 + 40, t)
        f.obj, f.chunks = "I" + xid, chunks

    # ---- the initial tree --------------------------------------------------------------

    def _add_file(self, src: _Source, d: _Dir, f: _File) -> None:
        f.name = self._unique(d.files.keys() | d.dirs.keys(), f.name)
        f.parent = d
        d.files[f.name] = f
        f.idx = len(src.files)
        src.files.append(f)
        if f.heat > 0:
            src.heated.append(f)
        d.dirty()

    def _add_dir(self, src: _Source, parent: _Dir, t: float, name: str | None = None,
                 kind: str = "") -> _Dir:
        if name is None:
            name, kind = self._folder_name(src, parent, t)
        name = self._unique(parent.dirs.keys() | parent.files.keys(), name)
        d = _Dir(name, parent, t, kind)
        parent.dirs[name] = d
        d.idx = len(src.dirs)
        src.dirs.append(d)
        parent.dirty()
        return d

    def _heat(self, key: str) -> float:
        r = self.rng.random()
        hot, warm = (0.01, 0.04) if key in ("photos", "music") else (0.08, 0.17)
        if r < hot:
            return self.rng.uniform(0.03, 0.3)
        if r < hot + warm:
            return self.rng.uniform(0.002, 0.02)
        return 0.0

    def _make_file(self, src: _Source, t: float, ext_type: _Type | None = None) -> _File:
        tp = ext_type or self._pick_type(src.key)
        size = 0 if self.rng.random() < self.p["zero_size_share"] else tp.sizes.draw(self.rng)
        mtime = t - self.age.draw(self.rng)
        f = _File(self._file_name(src.key, tp.ext, t), tp.ext, size, mtime, self._heat(src.key),
                  tp.packed)
        self._new_content(f, t)
        return f

    def _grow(self, src: _Source, n_files: int, shift: int) -> None:
        """The source as it stood at its first snapshot, then its history."""
        every, days, _ret = SCHEDULE[src.key]
        start = self.now - days * _DAY
        # The source grows to n_files by its last snapshot; it starts a bit over half that size.
        n0 = max(30, int(n_files * 0.55))
        src.root = _Dir(src.path.rstrip("\\").split("\\")[-1] or src.path, None, start - 400 * _DAY)
        mean_files = self._mean(self.p["files_per_dir"], shift)
        self._tree(src, max(4, int(n0 / mean_files)), start)
        self._place_files(src, n0, shift, start)
        if src.key == "projects":
            self._project_extras(src, start)
        self._dupes(src, start)
        self._history(src, start, every, days, n_files)

    @staticmethod
    def _mean(hist: dict[str, float], shift: int) -> float:
        tot = 0.0
        for k, v in hist.items():
            b = int(k) + (shift if int(k) > 0 else 0)
            tot += v * (0 if b <= 0 else 0.75 * (1 << b))
        return max(tot, 1.0)

    def _tree(self, src: _Source, folders: int, t: float) -> None:
        queue = [src.root]
        made = 0
        while made < folders:
            if not queue:
                queue = [d for d in [src.root, *src.dirs] if d.depth < 6]
            d = queue.pop(0)
            k = _from_log2_bin(self.rng, self.subdirs_hist.draw(self.rng))
            if d.depth == 0:
                k = max(k, self.rng.randint(4, 9))
            # The profile's rare 200+ subfolder draws near the root would put most of a source
            # under one top-level folder.
            k = min(k, 0 if d.depth >= _MAX_DEPTH else 12 if d.depth <= 1 else 60)
            for _ in range(min(k, folders - made)):
                queue.append(self._add_dir(src, d, t - self.rng.uniform(30, 900) * _DAY))
                made += 1

    def _place_files(self, src: _Source, n: int, shift: int, t: float) -> None:
        hist = _Hist(self.p["files_per_dir"], shift)
        dirs = [src.root, *src.dirs]
        counts = [_from_log2_bin(self.rng, hist.draw(self.rng)) for _ in dirs]
        total = sum(counts) or 1
        cap = max(20, n // 8)
        counts = [min(cap, round(c * n / total)) for c in counts]
        short = n - sum(counts)
        for _ in range(max(0, short)):
            counts[self.rng.randrange(len(counts))] += 1
        for d, c in zip(dirs, counts):
            for _ in range(c):
                self._add_file(src, d, self._make_file(src, t))

    def _project_extras(self, src: _Source, t: float) -> None:
        """Each project's root files, an ignore file, and node_modules shared between projects
        (the same package folders, so the same objects: real-world dedup)."""
        js = next((tp for tp in self.types["projects"] if tp.ext == "js"), None)
        for proj in [d for d in src.root.dirs.values() if d.kind == "project"]:
            for name in self.rng.sample(CODE_ROOT_FILES, self.rng.randint(3, 7)):
                f = _File(name, name.rpartition(".")[2] if "." in name[1:] else "",
                          self.rng.randint(200, 9000), t - self.age.draw(self.rng), self._heat("projects"), 0.4)
                self._new_content(f, t)
                self._add_file(src, proj, f)
            if self.rng.random() < 0.5:
                ign = _File(".kopiaignore", "kopiaignore", len(_IGNORE_TEXT), t - 200 * _DAY, 0.0, 1.0)
                cid = hashlib.blake2b(_IGNORE_TEXT.encode(), digest_size=16).hexdigest()
                self._store(cid, len(_IGNORE_TEXT), len(_IGNORE_TEXT) + 40, t)
                self.texts[cid] = _IGNORE_TEXT
                ign.obj = cid
                self._add_file(src, proj, ign)
            if js is not None and self.rng.random() < 0.6:
                nm = self._add_dir(src, proj, t, "node_modules")
                for pkg in self.rng.sample(PACKAGES, self.rng.randint(4, 12)):
                    self._package(src, nm, pkg, t, js)

    def _package(self, src: _Source, parent: _Dir, pkg: str, t: float, js: _Type) -> None:
        files = self.packages.get(pkg)
        if files is None:
            files = []
            stamp = t - self.rng.uniform(60, 700) * _DAY
            for name in ["package.json", "README.md", "LICENSE"] + [
                    f"{n}.{e}" for n in self.rng.sample(PACKAGE_FILES, self.rng.randint(3, 10))
                    for e in ("js", "d.ts")[: self.rng.randint(1, 2)]]:
                size = js.sizes.draw(self.rng) if name.endswith(".js") else self.rng.randint(300, 12000)
                cid = _hex(self.rng)
                self._store(cid, size, max(32, int(size * 0.35)), t)
                files.append((name, size, cid, stamp))
            self.packages[pkg] = files
        d = self._add_dir(src, parent, t, pkg)
        for name, size, cid, stamp in files:
            ext = name.rpartition(".")[2] if "." in name else ""
            f = _File(name, ext, size, stamp, 0.0, 0.35)
            f.obj = cid
            self._add_file(src, d, f)
        d.mtime = files[0][3]

    def _dupes(self, src: _Source, t: float) -> None:
        """Copies of existing files elsewhere in the source ("(2)", backups, exports)."""
        share = self.p["dupes"]["share"] * (0.5 if src.key == "projects" else 0.15)
        target = int(len(src.files) * share / 2.5)
        pool = [f for f in src.files if f.size > 0]
        for _ in range(target):
            if not pool:
                break
            orig = self.rng.choice(pool)
            for _ in range(max(1, _from_log2_bin(self.rng, self.dupe_hist.draw(self.rng)) - 1)):
                d = self.rng.choice(src.dirs) if src.dirs else src.root
                f = _File(orig.name, orig.ext, orig.size, orig.mtime, 0.0, orig.packed)
                f.obj, f.chunks = orig.obj, orig.chunks
                self._add_file(src, d, f)

    # ---- history -----------------------------------------------------------------------

    def _remove_file(self, src: _Source, f: _File) -> None:
        last = src.files.pop()
        if last is not f:
            src.files[f.idx] = last
            last.idx = f.idx
        d = f.parent
        if d is not None and d.files.get(f.name) is f:
            del d.files[f.name]
            d.dirty()
        f.parent = None

    def _history(self, src: _Source, start: float, every: int, days: int, n_final: int) -> None:
        r = self.rng
        rates = self.p["rates"]
        ratio = max(rates["added"] / max(rates["deleted"], 1e-6), 1.5)
        growth = math.log(max(n_final, 2) / max(len(src.files), 1)) / max(days, 1)
        d_rate = max(growth / (ratio - 1), 0.0)
        a_rate = ratio * d_rate
        hh = r.choice([10, 12, 16]) * 3600 + r.randint(0, 59) * 60
        day0 = math.floor(start / _DAY) * _DAY
        snap_days = [day0 + i * _DAY + hh + r.uniform(0, 300) for i in range(0, days + 1, every)
                     if r.random() > 0.06 or i == days]
        snap_days = [s for s in snap_days if s <= self.now]
        policy = SCHEDULE[src.key][2] or _GLOBAL_RETENTION
        keep = retention(snap_days, policy)
        prev_files: set[str] = set()
        snap_i = 0
        day = snap_days[0] if snap_days else self.now
        while snap_i < len(snap_days):
            t = snap_days[snap_i]
            if snap_i in keep:
                prev_files = self._snapshot(src, t, keep[snap_i], prev_files)
            snap_i += 1
            if snap_i >= len(snap_days):
                break
            nxt = snap_days[snap_i]
            while day < nxt:
                day += _DAY
                self._day(src, min(day, nxt - 60), a_rate, d_rate)
        src.snapshots.sort(key=lambda s: s["startTime"])

    def _day(self, src: _Source, t: float, a_rate: float, d_rate: float) -> None:
        r = self.rng
        for f in src.heated:
            if f.parent is not None and r.random() < f.heat:
                f.size = max(0, int(f.size * r.lognormvariate(0.0, 0.08)) + r.randint(-200, 2000))
                f.mtime = t - r.uniform(0, 3600)
                self._new_content(f, t, keep_chunks=True)
                f.parent.dirty()
        n = len(src.files)
        for _ in range(_poisson(r, n * d_rate)):
            if src.files:
                self._remove_file(src, r.choice(src.files))
        adds = _poisson(r, n * a_rate)
        if adds and r.random() < 0.15 and src.dirs:
            parent = r.choice([src.root] + [d for d in src.dirs if d.depth < 4 and d.name != "node_modules"])
            target = self._add_dir(src, parent, t)
        else:
            target = None
        for _ in range(adds):
            d = target or r.choice([src.root] + src.dirs)
            if d.name == "node_modules" or (d.parent is not None and d.parent.name == "node_modules"):
                d = src.root
            f = self._make_file(src, t)
            f.mtime = t - r.uniform(0, 6 * 3600)
            self._add_file(src, d, f)

    def _serialize(self, d: _Dir) -> tuple[str, dict, float]:
        """(object id, summary, newest mtime) of a folder; unchanged folders keep their object."""
        if d.cache is not None:
            return d.cache
        entries = []
        size = files = 0
        dirs = 1
        maxt = d.mtime
        for name in sorted(d.dirs.keys() | d.files.keys()):
            if name in d.dirs:
                sub = d.dirs[name]
                oid, summ, mt = self._serialize(sub)
                size += summ["size"]
                files += summ["files"]
                dirs += summ["dirs"]
                maxt = max(maxt, mt)
                entries.append({"name": name, "type": "d", "mode": "0777", "mtime": _ts(sub.mtime),
                                "obj": oid, "summ": summ})
            else:
                f = d.files[name]
                size += f.size
                files += 1
                maxt = max(maxt, f.mtime)
                entries.append({"name": name, "type": "f", "mode": "0666", "size": f.size,
                                "mtime": _ts(f.mtime), "obj": f.obj})
        summ = {"size": size, "files": files, "symlinks": 0, "dirs": dirs, "maxTime": _ts(maxt),
                "numFailed": 0}
        line = json.dumps({"stream": "kopia:directory", "entries": entries, "summary": summ},
                          separators=(",", ":"))
        oid = "k" + hashlib.blake2b(line.encode(), digest_size=16).hexdigest()
        if oid not in self.objects:
            self.objects[oid] = line
            self._store(oid, len(line), len(line) // 3 + 48, self._clock)
        d.cache = (oid, summ, maxt)
        return d.cache

    _clock = 0.0

    def _snapshot(self, src: _Source, t: float, reasons: list[str], prev: set[str]) -> set[str]:
        self._clock = t
        oid, summ, _mt = self._serialize(src.root)
        objs = {f.obj for f in src.files}
        new = len(objs - prev)
        new_bytes = sum(f.size for f in src.files if f.obj not in prev)
        secs = 4 + summ["files"] * 0.00025 + new_bytes / (90 * _MIB)
        root = {"name": src.root.name, "type": "d", "mode": "0777", "mtime": _ts(src.root.mtime),
                "obj": oid, "summ": summ}
        snap = {
            "id": _hex(self.rng), "source": {"host": src.host, "userName": src.user, "path": src.path},
            "description": "", "startTime": _ts(t), "endTime": _ts(t + secs),
            "stats": {"totalSize": summ["size"], "excludedTotalSize": 0, "fileCount": summ["files"],
                      "cachedFiles": max(0, summ["files"] - new), "nonCachedFiles": min(new, summ["files"]),
                      "dirCount": summ["dirs"], "excludedFileCount": 0, "excludedDirCount": 0,
                      "ignoredErrorCount": 0, "errorCount": 0},
            "rootEntry": root, "retentionReason": reasons,
        }
        src.snapshots.append(snap)
        return objs

    # ---- policies and the read commands ------------------------------------------------

    def _policies(self) -> dict[str, dict]:
        """``policy export`` shape: global plus the per-source overrides."""
        glob = {"retention": dict(_GLOBAL_RETENTION, ignoreIdenticalSnapshots=False),
                "files": {"ignoreDotFiles": [".kopiaignore"]},
                "errorHandling": {"ignoreFileErrors": False, "ignoreDirectoryErrors": False,
                                  "ignoreUnknownTypes": True},
                "scheduling": {"intervalSeconds": 86400, "runMissed": True},
                "compression": {"compressorName": "zstd-fastest"},
                "metadataCompression": {"compressorName": "zstd-fastest"}, "splitter": {}, "actions": {},
                "osSnapshots": {"volumeShadowCopy": {"enable": 0}},
                "logging": {"directories": {"snapshotted": 5, "ignored": 5},
                            "entries": {"snapshotted": 0, "ignored": 5, "cacheHit": 0, "cacheMiss": 0}},
                "upload": {"maxParallelSnapshots": 1, "parallelUploadAboveSize": 2147483648}}
        out = {"(global)": glob}
        for src in self.sources:
            every, _days, ret = SCHEDULE[src.key]
            pol: dict = {}
            if ret:
                pol["retention"] = ret
            if every != 1:
                pol["scheduling"] = {"intervalSeconds": every * 86400}
            if src.key == "projects":
                pol["files"] = {"ignore": ["node_modules/.cache", "*.pyc", "__pycache__/", "dist/"]}
            if src.key in ("photos", "music"):
                pol["compression"] = {"compressorName": "none"}
            if pol:
                out[src.target] = pol
        return out

    def _effective(self, target: str) -> dict:
        glob = json.loads(json.dumps(self.policies["(global)"]))
        for section, values in self.policies.get(target, {}).items():
            glob.setdefault(section, {}).update(values)
        return glob

    def answer(self, repo: str, args: list[str]) -> tuple[int, str, str]:
        """kopia's reply to one command, for this model."""
        a = [str(x) for x in args]
        if a[:2] == ["snapshot", "list"]:
            snaps = sorted((s for src in self.sources for s in src.snapshots),
                           key=lambda s: (s["source"]["path"], s["startTime"]))
            return 0, json.dumps(snaps), ""
        if a[:2] == ["content", "show"]:
            out = []
            for oid in a[2:]:
                line = self.objects.get(oid)
                if line is None:
                    return 1, "\n".join(out) + ("\n" if out else ""), f"content {oid} not found"
                out.append(line)
            return 0, "\n".join(out) + "\n", ""
        if a[:2] == ["content", "list"]:
            return 0, self.content_list(), ""
        if a[:1] == ["show"] and len(a) > 1:
            if a[1] in self.texts:
                return 0, self.texts[a[1]], ""
            line = self.objects.get(a[1][1:] if a[1].startswith("I") else a[1])
            return (0, line + "\n", "") if line else (1, "", f"object {a[1]} not found")
        if a[:2] == ["policy", "export"]:
            return 0, json.dumps(self.policies), ""
        if a[:2] == ["policy", "show"] and "--json" in a:
            target = a[2]
            if target == "--global":
                return 0, json.dumps(self._effective("(global)")), ""
            return 0, json.dumps(self._effective(target)), ""
        if a[:3] == ["repository", "status", "--json"]:
            return 0, json.dumps(self.repo_status()), ""
        if a[:3] == ["maintenance", "info", "--json"]:
            return 0, json.dumps(self.maintenance()), ""
        if a[:2] == ["content", "stats"]:
            return 0, self.content_stats(), ""
        if a[:2] == ["cache", "info"]:
            return 0, self.cache_info(), ""
        return 1, "", demoreplies.UNAVAILABLE

    def latest_files(self) -> list[tuple[str, int, float]]:
        """(full path, size, mtime) of every file as the sources stand now."""
        out = []

        def walk(d: _Dir, path: str) -> None:
            for f in d.files.values():
                out.append((path + "\\" + f.name, f.size, f.mtime))
            for sub in d.dirs.values():
                walk(sub, path + "\\" + sub.name)
        for src in self.sources:
            walk(src.root, src.path.rstrip("\\"))
        return out

    def replies(self) -> dict[str, dict]:
        """The read commands a demo repo's screens use, as recorded replies."""
        cmds = [["snapshot", "list", "--json"], ["policy", "export"],
                ["policy", "show", "--global", "--json"], ["repository", "status", "--json"],
                ["maintenance", "info", "--json"], ["content", "stats"], ["cache", "info"]]
        cmds += [["policy", "show", s.target, "--json"] for s in self.sources]
        cmds += [["show", cid] for cid in self.texts]
        out = {}
        for c in cmds:
            rc, text, err = self.answer(REPO_ID, c)
            out[demoreplies.key(c)] = {"rc": rc, "out": text, "err": err}
        return out

    def _packs(self) -> list[tuple[str, int, float, str, int, int]]:
        """(id, original, written, pack, offset, packed) per content, packed in write order."""
        r = random.Random(self.rng.random())
        rows = []
        data_pack, meta_pack, data_off, meta_off = "", "", _PACK_SIZE, _PACK_SIZE
        for cid, (orig, packed, t) in sorted(self.contents.items(), key=lambda kv: (kv[1][2], kv[0])):
            meta = cid[:1] in ("k", "x")
            if meta:
                if meta_off >= _PACK_SIZE:
                    meta_pack, meta_off = f"q{_hex(r)}-s{_hex(r, 64)[:16]}", 0
                rows.append((cid, orig, t, meta_pack, meta_off, packed))
                meta_off += packed
            else:
                if data_off >= _PACK_SIZE:
                    data_pack, data_off = f"p{_hex(r)}-s{_hex(r, 64)[:16]}", 0
                rows.append((cid, orig, t, data_pack, data_off, packed))
                data_off += packed
        return rows

    def content_list(self) -> str:
        if getattr(self, "_content_list", None) is None:
            lines = []
            for cid, orig, t, pack, off, packed in sorted(self._packs()):
                when = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"{cid} {orig} {when} UTC {pack} {off}+{packed} -")
            self._content_list = "\n".join(lines) + "\n"
        return self._content_list

    def stored_bytes(self) -> int:
        return sum(p for _o, p, _t in self.contents.values())

    def content_stats(self) -> str:
        vals = list(self.contents.values())
        total = sum(o for o, _p, _t in vals)
        comp = [(o, p) for o, p, _t in vals if 0 < o and p < o * 0.97]
        plain = len(vals) - len(comp)
        lines = [f"Count: {len(vals)}", f"Total Bytes: {_human(total)}", "By Method:",
                 f"  (uncompressed)         count: {plain} size: {_human(total - sum(o for o, _ in comp))}"]
        if comp:
            co, cp = sum(o for o, _ in comp), sum(p for _, p in comp)
            lines.append(f"  zstd-fastest           count: {len(comp)} size: {_human(co)} "
                         f"packed: {_human(cp)} compression: {100 * (1 - cp / co):.1f}%")
        lines += [f"Average: {_human(total / max(len(vals), 1))}", "Histogram:", ""]
        edges = [0, 10, 100, 1000, 10_000, 100_000, 1_000_000, 10_000_000, 100_000_000]
        for lo, hi in zip(edges, edges[1:]):
            sel = [o for o, _p, _t in vals if lo <= o < hi]
            lines.append(f"{len(sel):>9} between {_human(lo)} and {_human(hi)} (total {_human(sum(sel))})")
        return "\n".join(lines) + "\n"

    def cache_info(self) -> str:
        base = rf"C:\Users\{self.identity['user']}\AppData\Local\kopia\{self.identity['cache']}"
        return "\n".join([
            rf"{base}\blob-list: 4 files 4.9 KB (duration: 30s)",
            rf"{base}\contents: 1184 files 612.4 MB (soft limit: 5.2 GB, hard limit: none, min sweep age: 10m0s)",
            rf"{base}\index-blobs: 212 files 18.6 MB",
            rf"{base}\indexes: 96 files 21.3 MB",
            rf"{base}\metadata: 2210 files 148.2 MB (soft limit: 5.2 GB, hard limit: none, min sweep age: 24h0m0s)",
            rf"{base}\own-writes: 2 files 80 B"]) + "\n"

    def repo_status(self) -> dict:
        ident = self.identity
        cap = ident["capacity"]
        return {
            "configFile": demoreplies.DISPLAY_CONFIG, "uniqueIDHex": ident["unique_id"],
            "clientOptions": {"hostname": ident["host"].lower(), "username": ident["user"],
                              "description": "Sample backups", "enableActions": False,
                              "formatBlobCacheDuration": 900000000000},
            "storage": {"type": "filesystem", "config": {"path": ident["storage"], "dirShards": None}},
            "volume": {"capacity": cap, "available": max(cap // 10, cap - self.stored_bytes() - cap // 3)},
            "contentFormat": {"hash": "BLAKE2B-256-128", "encryption": "AES256-GCM-HMAC-SHA256",
                              "ecc": "REED-SOLOMON-CRC32", "eccOverheadPercent": 1, "version": 2,
                              "maxPackSize": _PACK_SIZE, "indexVersion": 2,
                              "epochParameters": {"Enabled": True, "EpochRefreshFrequency": 1200000000000,
                                                  "FullCheckpointFrequency": 7,
                                                  "CleanupSafetyMargin": 14400000000000,
                                                  "MinEpochDuration": 86400000000000,
                                                  "EpochAdvanceOnCountThreshold": 20,
                                                  "EpochAdvanceOnTotalSizeBytesThreshold": 10485760,
                                                  "DeleteParallelism": 4},
                              "enablePasswordChange": True},
            "objectFormat": {"splitter": "DYNAMIC-4M-BUZHASH"}, "blobRetention": {}}

    def maintenance(self) -> dict:
        ident = self.identity

        def at(t: float) -> str:
            return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat(timespec="microseconds")
        runs: dict[str, list] = {}
        quick_kinds = ("advance-epoch", "compact-single-epoch")
        full_kinds = ("snapshot-gc", "full-drop-deleted-content", "full-rewrite-contents",
                      "full-delete-blobs", "cleanup-logs", "cleanup-epoch-markers",
                      "delete-superseded-epoch-indexes", "generate-epoch-range-index")
        last_full = self.now - 5 * 3600
        for k in quick_kinds:
            runs[k] = [{"start": at(self.now - h * 3600 - 40), "end": at(self.now - h * 3600 - 39),
                        "success": True, "extra": []} for h in range(1, 13)]
        for k in full_kinds:
            runs[k] = [{"start": at(last_full - d * _DAY), "end": at(last_full - d * _DAY + 95),
                        "success": True, "extra": []} for d in range(7)]
        return {"owner": f"{ident['user']}@{ident['host'].lower()}",
                "quick": {"enabled": True, "interval": 3600000000000},
                "full": {"enabled": True, "interval": 86400000000000},
                "logRetention": {"maxTotalSize": 1073741824, "maxCount": 10000, "maxAge": 2592000000000000},
                "extendObjectLocks": False, "listParallelism": 0,
                "schedule": {"nextFullMaintenance": at(last_full + _DAY),
                             "nextQuickMaintenance": at(self.now + 1800), "runs": runs}}


def _poisson(r: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(r.gauss(lam, math.sqrt(lam)) + 0.5))
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= r.random()
        if p <= limit:
            return k
        k += 1


def _human(n: float) -> str:
    """kopia's decimal byte units ("2.4 GB")."""
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


def identity(seed: int, taken=None) -> dict:
    """The sample's invented user, computer and storage, fixed by the seed. ``taken(name)`` says
    a name belongs to this install's real repos; those are skipped."""
    r = random.Random(f"identity-{seed}")
    free = (lambda n: not taken(n.lower())) if taken else (lambda n: True)
    users = [u for u in fakenames.FIRST_NAMES if free(u)] or ["demo"]
    hosts = [h for h in _HOSTS if free(h)] or ["DEMO-PC"]
    return {"user": r.choice(users), "host": r.choice(hosts),
            "storage": r"E:\Kopia", "unique_id": _hex(r, 256), "cache": _hex(r, 64)[:16],
            "capacity": 4_000_787_030_016}


def vocabulary() -> set[str]:
    """Every word a sample name may contain."""
    words: set[str] = set()
    lists = [CODE_DIRS, CODE_ROOT_FILES, PACKAGES, PACKAGE_FILES, PHOTO_EVENTS, MONTHS, MUSIC_DIRS,
             DOWNLOAD_DIRS, fakenames.PROJECT_WORDS, fakenames.PROJECT_SUFFIXES, fakenames.QUALIFIERS,
             fakenames.FIRST_NAMES, _HOSTS, [p for _k, p, *_ in THEMES],
             ["LICENSE", "Makefile", "Dockerfile", "README", "Procfile", "VERSION", "CODEOWNERS",
              "AUTHORS", "icon", "logo", "banner", "bg", "node_modules", ".kopiaignore", "package.json",
              "users", "projects", "kopia"]]
    lists += list(fakenames.FOLDER_WORDS.values())
    lists += [[re.sub(r"\{[a-z0-9]+\}", " ", t) for t in ts] for ts in fakenames.FILE_TEMPLATES.values()]
    for seq in lists:
        for item in seq:
            words.update(w for w in _tokens(str(item)) if w)
    return words


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in re.split(r"[\s_\-.()\[\],&+#'!~@:\\/]+", text) if w]


def check_names(folder: str, repo: str = REPO_ID) -> dict:
    """Words in the sample's names and sources that are not from the generator's lists."""
    import duckdb
    vocab = vocabulary()
    base = os.path.join(folder, f"repo{repo}")
    with open(os.path.join(base, "manifest.json"), encoding="utf-8") as fh:
        artifacts = json.load(fh)["artifacts"]
    con = duckdb.connect()
    unknown: dict[str, int] = {}
    checked = 0
    for table, col in (("pathx", "name"), ("dir", "name"), ("src", "source"), ("tent", "name")):
        files = [os.path.join(base, f).replace("\\", "/") for f in artifacts.get(table, [])]
        if not files:
            raise RuntimeError(f"sample name check: the index has no {table} table")
        checked += 1
        for (value,) in con.execute(f"SELECT DISTINCT {col} FROM read_parquet(?)", [files]).fetchall():
            for w in _tokens(value or ""):
                if w not in vocab and not w.isdigit() and not re.fullmatch(r"[0-9a-f]{6,}", w) \
                        and not re.fullmatch(r"[a-z]?\d+[a-z]?", w):
                    unknown[w] = unknown.get(w, 0) + 1
    con.close()
    exts = {t["ext"] for t in load_profile()["types"]} | {e for v in FALLBACK_TYPES.values() for e, *_ in v}
    exts |= {w for e in exts for w in _tokens(e)}   # "rpp-bak" -> "rpp", "bak"
    unknown = {w: n for w, n in unknown.items() if w not in exts and w not in ("d", "ts", "kopiaignore")}
    return {"unknown_words": len(unknown), "examples": sorted(unknown)[:20], "tables": checked}


# ---- building a sample data folder ---------------------------------------------------------

def sample_dir(base: str | None = None) -> str:
    return os.path.join(base or DATA_DIR, "demo", SAMPLE_DIR)


def _record(seed: int, repo: str = REPO_ID, name: str = "Sample") -> dict:
    ident = identity(seed)   # storage facts only; they do not depend on the names
    return {"id": repo, "name": name, "short_name": "Sample", "kind": "kopia", "demo": True,
            "sample": True, "counts_as_backup": False, "source": "sample", "unique_id": ident["unique_id"],
            "storage": {"type": "filesystem", "config": {"path": ident["storage"]}},
            "capacity": {"capacity": ident["capacity"], "available": ident["capacity"] // 2},
            "status_at": _dt.datetime.now().timestamp()}


def build(out: str | None = None, *, files: int = DEFAULT_FILES, seed: int = DEFAULT_SEED,
          repo: str = REPO_ID, backblaze: bool = True) -> dict:
    """Write a complete sample data folder: index, caches and recorded replies."""
    from . import demo
    from .dataupgrade import DATA_VERSION
    target = out or sample_dir()
    building = target + ".building"
    if os.path.isdir(building):
        shutil.rmtree(building)
    os.makedirs(building)
    from .demobz import BZ_RECORD
    demo._dump(os.path.join(building, "repos.json"), {"version": 1, "cache_dir": None,
                                                      "repos": [_record(seed, repo)] + ([BZ_RECORD] if backblaze else [])})
    demo._dump(os.path.join(building, "data_version.json"),
               {"version": DATA_VERSION, "app": DATA_VERSION, "at": _dt.datetime.now().timestamp()})
    demo._dump(os.path.join(building, "settings.json"), demo._DEMO_SETTINGS)
    demo._dump(os.path.join(building, "demo.json"),
               {"kind": "sample", "seed": seed, "files": files, "app_version": APP_VERSION,
                "backblaze": backblaze,
                "profile_version": load_profile().get("version"),
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})
    forbidden = _real_identity()
    demo._step("generating and indexing the sample")
    salt = secrets.token_hex(8)
    check = building + ".check"
    demo._dump(check, sorted(demo._hash(salt, f) for f in forbidden))
    env = demo._child_env(building)
    env.update(KOPIALENS_DEMO_CHECK=check, KOPIALENS_DEMO_SALT=salt,
               KOPIALENS_SAMPLE_SEED=str(seed), KOPIALENS_SAMPLE_FILES=str(files),
               KOPIALENS_SAMPLE_REPO=repo, KOPIALENS_SAMPLE_BZ="1" if backblaze else "0")
    try:
        res = subprocess.run([SELF_EXE, "-m", "app.core.sampledata", "--child"], cwd=HERE,
                             env=env, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=3600)
    finally:
        os.remove(check)
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.startswith("{")]
    if res.returncode != 0 or not lines:
        raise RuntimeError(f"sample build failed (exit {res.returncode}): {(res.stderr or '')[-800:]}")
    child = json.loads(lines[-1])
    demo.prune(building, [repo])
    demo._step("checking names")
    names = check_names(building, repo)
    leaks = demo.leak_check(building, forbidden, demoreplies.machine_paths())
    if names["unknown_words"] or leaks["leaked"] or child.get("leaked"):
        # The flagged words come from this generator, never from a real repo, so naming them is safe.
        raise RuntimeError(f"sample failed its checks: {names['unknown_words']} unknown words "
                           f"{names['examples']}, "
                           f"{leaks['leaked']} identity or machine-path hits in files "
                           f"({leaks['files']}), {child.get('leaked', 0)} in screens ({child.get('routes')})")
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.replace(building, target)
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(target) for f in fs)
    return {"path": target, "bytes": size, **{k: child.get(k) for k in (
        "files", "folders", "snapshots", "contents", "stored", "stages", "errors", "backblaze",
        "swept", "failed")}}


SAMPLE_NAME = "Sample data"
_INSTALL: dict = {"state": "idle"}
_INSTALL_LOCK = threading.Lock()


def install_state() -> dict:
    return dict(_INSTALL)


def start_install() -> dict:
    """Build the sample on this computer in the background and add it as a demo repo."""
    with _INSTALL_LOCK:
        if _INSTALL.get("state") == "building":
            return install_state()
        _INSTALL.clear()
        _INSTALL.update(state="building", started=time.time())
    threading.Thread(target=_install_thread, name="sample-data", daemon=True).start()
    return install_state()


def _install_thread() -> None:
    try:
        rec = install()
        _INSTALL.update(state="done", repo=rec["id"], name=rec["name"])
    except Exception as exc:  # noqa: BLE001 - reported to the screen that started it
        log.exception("sample data could not be added")
        _INSTALL.update(state="error", error=str(exc)[:300])


def install(*, files: int = DEFAULT_FILES, seed: int = DEFAULT_SEED) -> dict:
    """Generate the sample under a fresh repo id, move its index, caches and replies into this
    install, and add the record (a demo repo that is not counted as a backup). No Backblaze:
    a normal install's Backblaze is the computer's own."""
    from ..index import store
    from . import repos
    rid = repos.reserve_id()
    tmp = os.path.join(DATA_DIR, "demo", f"sample-build-{rid}")
    try:
        build(tmp, files=files, seed=seed, repo=rid, backblaze=False)
        for entry in os.listdir(tmp):
            src = os.path.join(tmp, entry)
            if entry == f"repo{rid}":
                shutil.move(src, store.repo_dir(rid))
            elif entry == "cache":
                os.makedirs(CACHEDIR, exist_ok=True)
                for name in os.listdir(src):
                    if repos._belongs_to(name, rid):
                        shutil.move(os.path.join(src, name), os.path.join(CACHEDIR, name))
            elif repos._belongs_to(entry, rid):
                shutil.move(src, os.path.join(DATA_DIR, entry))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return repos.add_record(_record(seed, rid, name=SAMPLE_NAME))


def _real_identity() -> set[str]:
    """The user and computer names of this install's real repos: never allowed in a sample."""
    from config import REPOS

    from ..index import store
    from . import demo
    sources: list[str] = []
    for repo, cfg in REPOS.items():
        if cfg.get("kind", "kopia") != "kopia" or cfg.get("demo"):
            continue
        try:
            if store.read_manifest(repo) is None:
                continue
            gen = store.current(repo)
            try:
                sources += [r[0] for r in gen.cursor().execute("SELECT source FROM src").fetchall()]
            finally:
                gen.release()
        except Exception as exc:  # noqa: BLE001 - a repo that cannot be read adds no names
            log.warning("sample: repo %s identity not read: %s", repo, exc)
    return demo._identity(sources)


def _child() -> None:
    """Inside the sample folder: generate, index with the real worker, record replies, sweep."""
    from ..index import worker
    from . import demo
    salt = os.environ["KOPIALENS_DEMO_SALT"]
    with open(os.environ["KOPIALENS_DEMO_CHECK"], encoding="utf-8") as fh:
        real = set(json.load(fh))   # salted hashes of this install's real user and computer names
    sample = Sample(load_profile(), files=int(os.environ["KOPIALENS_SAMPLE_FILES"]),
                    seed=int(os.environ["KOPIALENS_SAMPLE_SEED"]),
                    taken=lambda name: demo._hash(salt, name) in real)
    repo = os.environ.get("KOPIALENS_SAMPLE_REPO", REPO_ID)
    demoreplies.set_backend(sample.answer)
    try:
        summary = worker.run(repo, full=True)
    finally:
        demoreplies.set_backend(None)
    demo._dump(demoreplies.replies_path(repo), sample.replies())
    bz, bz_index = {"files": 0, "backed": 0}, {}
    if os.environ.get("KOPIALENS_SAMPLE_BZ") == "1":
        from . import demobz
        bz_root = os.path.join(DATA_DIR, "bzdata")
        bz = demobz.write(bz_root, sample.latest_files(), user=sample.identity["user"],
                          host=sample.identity["host"], now=sample.now,
                          seed=int(os.environ["KOPIALENS_SAMPLE_SEED"]))
        bz_index = demobz.index(bz_root)
    sweep = demo._sweep(os.environ["KOPIALENS_DEMO_SALT"], os.environ["KOPIALENS_DEMO_CHECK"], [repo])
    print(json.dumps({
        "files": sum(len(s.files) for s in sample.sources),
        "folders": sum(len(s.dirs) + 1 for s in sample.sources),
        "snapshots": sum(len(s.snapshots) for s in sample.sources),
        "contents": len(sample.contents), "stored": sample.stored_bytes(),
        "stages": sorted(summary.get("stages") or {}), "errors": summary.get("errors") or None,
        "backblaze": {"files": bz["files"], "backed": bz["backed"], "ok": bz_index.get("ok", True)},
        **sweep}))


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Build the sample data folder.")
    ap.add_argument("--files", type=int, default=DEFAULT_FILES)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", help="target folder (default: <data>/demo/sample)")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child:
        _child()
        return 0
    r = build(args.out, files=args.files, seed=args.seed)
    print(json.dumps({k: v for k, v in r.items() if k != "path"}, indent=1))
    print("written to", r["path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
