"""Deterministic, one-way fake names for demo copies.

Every fake name is picked from fixed word lists by an HMAC of the real name, so the same real
name always gives the same fake name, and without the key nothing can be tested against it.
The key lives in the data folder and never goes into a copy or an export.

Three styles:
- ``codename``: obviously a demo ("Amber Fox", "amber-fox-27.jpg").
- ``generic``: strict. A name made only of common words is kept with its numbers re-rolled;
  anything else becomes a file-type or contents-based name ("IMG_4821.jpg", "Books 2023").
- ``balanced``: names stay real. Only risky names are replaced (the strict way): the ``replace``
  set the copy maker builds (hidden areas, the always-hide list), names carrying a piracy or NSFW
  marker, disc and ROM images. Account numbers, phone numbers, emails and browser site folders
  are rewritten in place, keeping their shape.

Mapping is by name alone (never by place), which is what keeps Tree, Search and paths in step.
Callers resolve sibling collisions by asking again with a higher ``attempt``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets

STYLES = ("codename", "generic", "balanced")
KEY_FILE = os.path.join("demo", "name.key")

# Kept as-is in both styles: technical names that say nothing about their owner.
TECHNICAL = {
    ".git", ".github", ".vscode", ".idea", "node_modules", "__pycache__", "__macosx", ".venv",
    "venv", "site-packages", "appdata", "local", "roaming", "locallow", "program files",
    "program files (x86)", "programdata", "windows", "system32", "users", "public", "temp",
    "tmp", "cache", "$recycle.bin", "system volume information", "savedata", "bin", "obj",
    "lib", "include", "share", "etc", "var", "opt", "usr", "home", "root", "dist", "build",
}

GENERIC = set("""
a an and the of for to in on at by with from my our new old copy final draft temp misc stuff
file files data doc docs document documents note notes readme changelog license todo report
reports invoice invoices receipt receipts statement statements photo photos picture pictures pics
image images img dsc screenshot screenshots camera wallpaper wallpapers video videos movie movies
clip clips music audio song songs track tracks recording recordings podcast podcasts project
projects work personal shared archive archives download downloads desktop library src source
sources lib bin build dist asset assets static public test tests config configs setting settings
setup install installer script scripts tool tools util utils helper helpers main app apps server
client web site website game games save saves mod mods log logs cache export exports import
imports template templates book books ebook ebooks chapter part vol volume series season episode
disc sample samples demo resource resources font fonts icon icons ui style styles backup backups
untitled folder home index default output input raw edited original originals scan scans
""".split())

CATEGORY_EXTS = {
    "Photos": {"jpg", "jpeg", "png", "heic", "raw", "cr2", "nef", "arw", "dng", "gif", "webp", "bmp", "tif", "tiff"},
    "Videos": {"mp4", "mkv", "mov", "avi", "webm", "m4v", "wmv", "mts"},
    "Music": {"mp3", "flac", "wav", "m4a", "ogg", "aac", "aiff", "opus"},
    "Books": {"epub", "mobi", "pdf", "azw3", "cbz", "cbr"},
    "Code": {"py", "js", "ts", "tsx", "jsx", "cs", "cpp", "c", "h", "java", "go", "rs", "json",
             "md", "sh", "bat", "cmd", "ps1", "yml", "yaml", "toml", "html", "css", "lua", "xml"},
    "Archives": {"zip", "7z", "rar", "tar", "gz", "xz", "iso", "msi", "exe", "dmg"},
    "Documents": {"doc", "docx", "xls", "xlsx", "csv", "txt", "ppt", "pptx", "odt", "ods", "rtf"},
}
_EXT_CATEGORY = {e: c for c, es in CATEGORY_EXTS.items() for e in es}

FOLDER_WORDS = {
    "Photos": ["Photos", "Camera Roll", "Pictures", "Trip", "Family", "Screenshots", "Holiday",
               "Wedding", "Phone Photos", "Edited", "Raw", "Prints", "Camera Uploads", "Exports",
               "Weekend", "Birthday", "Scans", "Wallpapers", "Road Trip", "Beach", "Selected",
               "Album", "Shoot", "Portraits"],
    "Videos": ["Videos", "Clips", "Recordings", "Movies", "Footage", "Edits", "Phone Videos",
               "Captures", "Renders", "Highlights", "Screen Recordings", "Raw Footage", "Exports",
               "Vlogs", "Timelapse", "Gameplay"],
    "Music": ["Music", "Albums", "Playlists", "Audio", "Stems", "Mixes", "Samples", "Sessions",
              "Bounces", "Demos", "Loops", "Takes", "Masters", "Recordings", "Drums", "Guitars"],
    "Books": ["Books", "Library", "Reading", "Ebooks", "Manuals", "Papers", "Magazines", "Guides",
              "Courses", "Notes", "Comics", "Textbooks"],
    "Code": ["Projects", "Source", "Scripts", "Tools", "Repos", "Experiments", "Prototypes",
             "Playground", "Sandbox", "Utilities", "Plugins", "Templates"],
    "Archives": ["Archives", "Installers", "Packages", "Old Backups", "Setup Files", "Drivers",
                 "Disk Images", "Firmware", "Downloads", "Releases", "Bundles", "Portable"],
    "Documents": ["Documents", "Reports", "Paperwork", "Spreadsheets", "Taxes", "Statements",
                  "Letters", "Invoices", "Receipts", "Forms", "Contracts", "Notes", "Plans",
                  "Records", "Bills", "Manuals"],
    "Mixed": ["Files", "Shared", "Misc", "Work", "Personal", "Old Laptop", "Data", "Inbox",
              "Sorted", "Unsorted", "Transfer", "Old PC", "Phone Backup", "Keep", "Current",
              "Set A", "Set B", "Part 2", "Batch", "Exports", "Library", "Collection", "Storage",
              "Stuff", "Extras", "Media", "Content", "Workspace", "Resources", "Saved", "Main",
              "Imported"],
}
QUALIFIERS = [""] * 12 + [" 2019", " 2020", " 2021", " 2022", " 2023", " 2024", " 2025", " (old)",
                          " Backup", " v2", " - Copy", " New", " Archive", " Final", " 2"]
PROJECT_WORDS = ["inventory", "budget", "weather", "notes", "tracker", "photo", "media", "home",
                 "task", "recipe", "habit", "chat", "invoice", "music", "garden", "travel"]
PROJECT_SUFFIXES = ["-app", "-tool", "-sync", "-site", "-bot", "-api", "-cli", "-web"]
FILE_TEMPLATES = {
    "Photos": ["IMG_{n4}", "DSC_{n4}", "PXL_{date}_{n4}", "photo-{n3}", "IMG_{date}_{n4}"],
    "Videos": ["VID_{date}_{n4}", "clip-{n3}", "recording-{n3}", "MOV_{n4}"],
    "Music": ["track-{n2}", "{n2} - Untitled", "take-{n2}", "mix-{n3}"],
    "Books": ["book-{n3}", "volume-{n2}", "chapter-{n2}", "manual-{n3}", "paper-{n3}"],
    "Code": ["main", "utils", "config", "helpers", "index", "setup", "notes", "build", "run",
             "app", "models", "routes", "test_{n2}", "module_{n2}"],
    "Archives": ["backup-{date}", "setup-{n3}", "archive-{n3}", "files-{n3}", "installer-{n3}"],
    "Documents": ["report-{date}", "invoice-{n4}", "notes-{n3}", "export-{n3}", "budget-{yr}",
                  "statement-{date}", "letter-{n3}"],
    "Mixed": ["file-{n3}", "data-{n3}", "item-{n3}", "doc-{n3}"],
}
# Disc and ROM images (IMAGE_EXTS): plain installer and backup discs.
DISC_TEMPLATES = ["Windows 10 21H2", "Windows 11 23H2", "Ubuntu 22.04", "Ubuntu 24.04", "Office 2019",
                  "Linux Mint 21", "Debian 12", "Fedora 39", "Driver Disc {n2}", "Recovery Disc {n2}",
                  "Backup Disc {yr}", "Install Disc {n2}", "Photos {yr}", "Archive Disc {n2}"]
CODE_ADJ = ["amber", "azure", "brisk", "cedar", "copper", "coral", "crimson", "dusky", "ember",
            "fable", "frosty", "gentle", "glacial", "golden", "hazel", "ivory", "jade", "lunar",
            "maple", "misty", "mossy", "nimble", "olive", "pearl", "quartz", "rapid", "rustic",
            "sable", "silver", "solar", "stormy", "sunny", "tidal", "velvet", "violet", "willow"]
CODE_NOUN = ["badger", "beacon", "canyon", "comet", "falcon", "fern", "fox", "gecko", "harbor",
             "heron", "koi", "lantern", "lynx", "meadow", "moth", "nebula", "orbit", "otter",
             "pebble", "pine", "puffin", "raven", "reef", "river", "summit", "thistle", "tundra",
             "valley", "wren", "yak", "atlas", "bison", "cobalt", "delta", "glade", "isle"]
FIRST_NAMES = ["alex", "sam", "jordan", "taylor", "casey", "morgan", "riley", "jamie", "robin",
               "drew", "quinn", "avery", "kai", "noor", "maya", "leo"]

# Balanced style: a name with one of these words is replaced whole (lowercase match).
MARKERS = set("""
fitgirl elamigos razor1911 skidrow onlinefix keygen keygens crack cracked crackfix cracks repack
repacks yts yify rarbg 1337x piratebay steamless torrent torrents debrid realdebrid torbox zilean
bitmagnet magnetarr prowlarr jackett sonarr radarr qbittorrent torrentio mediafusion aiostreams
stremio nuvio zurg hydralauncher ascendara scrawler popcorn stash dmmhashlists emulator emulators
pcsx2 rpcs3 ppsspp vita3k yuzu ryujinx citron sudachi cemu bios roms
porn porno xxx nsfw nude nudes hentai onlyfans fetish erotic sex
""".split())
# Scene group names that are ordinary words in lowercase: matched only as an all-caps word.
MARKERS_UPPER = {"RUNE", "CODEX", "PLAZA", "CPY", "FLT", "EMPRESS", "TENOKE", "DODI", "GSE", "R2R",
                 "HOODLUM", "RELOADED", "PROPHET", "DARKSIDERS"}
# Disc and ROM images: always replaced.
IMAGE_EXTS = {"iso", "cso", "chd", "nsp", "xci", "nsz", "wbfs", "wud", "wux", "rpx", "vpk", "gba",
              "nds", "3ds", "cia", "pbp", "gcm", "rvz", "z64", "n64", "sfc", "smc", "gb", "gbc"}
COMMON_SITES = ["www.google.com", "www.youtube.com", "github.com", "en.wikipedia.org", "www.reddit.com",
                "www.amazon.com", "mail.google.com", "docs.google.com", "drive.google.com",
                "www.microsoft.com", "outlook.live.com", "stackoverflow.com", "www.netflix.com",
                "open.spotify.com", "www.bbc.com", "www.nytimes.com", "news.ycombinator.com",
                "www.linkedin.com", "x.com", "www.twitch.tv", "store.steampowered.com",
                "www.dropbox.com", "web.whatsapp.com", "translate.google.com", "maps.google.com",
                "www.imdb.com", "www.apple.com", "chatgpt.com", "claude.ai", "www.notion.so",
                "calendar.google.com", "photos.google.com", "www.figma.com", "www.canva.com",
                "medium.com", "www.pinterest.com", "www.instagram.com", "www.facebook.com",
                "www.ebay.com", "weather.com", "www.speedtest.net", "app.slack.com"]
EMAIL_DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "proton.me", "icloud.com"]
_ORIGIN = re.compile(r"^(https?|chrome-extension)_(.+?)_(\d+)(\..*)?$", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+(com|net|org|in|io|me|co|ai|dev|app|edu|"
                    r"gov|uk|us|de|info|biz|xyz)\b", re.IGNORECASE)
_STEAM64 = re.compile(r"7656119(\d{10})")
_PHONE = re.compile(r"(?<!\d)(\+?91[ -]?)?([6-9]\d{9})(?!\d)")
_WORD = re.compile(r"[A-Za-z0-9]+")

_TOKEN_SPLIT = re.compile(r"[\s_\-.()\[\],&+#'!~@]+")


def load_key(data_dir: str) -> bytes:
    """The install's naming key, created on first use. It never leaves this file."""
    path = os.path.join(data_dir, KEY_FILE)
    try:
        with open(path, encoding="ascii") as fh:
            return bytes.fromhex(fh.read().strip())
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_bytes(32)
    with open(path, "x", encoding="ascii") as fh:
        fh.write(key.hex())
    return key


def ext_category(ext: str) -> str:
    return _EXT_CATEGORY.get(ext.lower().lstrip("."), "Mixed")


def category_of(ext_counts: dict[str, int]) -> str:
    """A folder's category: the type holding at least half of the files beneath it."""
    total = sum(ext_counts.values())
    if not total:
        return "Mixed"
    by_cat: dict[str, int] = {}
    for ext, n in ext_counts.items():
        cat = ext_category(ext)
        if cat != "Mixed":
            by_cat[cat] = by_cat.get(cat, 0) + n
    if not by_cat:
        return "Mixed"
    cat, n = max(by_cat.items(), key=lambda kv: (kv[1], kv[0]))
    return cat if n * 2 >= total else "Mixed"


def _words(stem: str) -> list[str]:
    stem = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    return [t for t in _TOKEN_SPLIT.split(stem) if t]


def is_generic(stem: str) -> bool:
    """True when every word is on the generic list (digits aside): such names reveal nothing."""
    words = [w.lower() for w in _words(stem) if not w.isdigit()]
    words = [re.sub(r"\d+$", "", w) for w in words]
    return bool(words) and all(w in GENERIC for w in words if w)


def marked(name: str) -> bool:
    """True when a word of the name is a piracy or NSFW marker."""
    words = _WORD.findall(name)
    return any(w.lower() in MARKERS or w in MARKERS_UPPER for w in words)


def split_ext(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1 or len(name) - dot > 9 or " " in name[dot:]:
        return name, ""
    return name[:dot], name[dot:]


class Namer:
    def __init__(self, key: bytes, style: str, replace=frozenset(), hide=frozenset(), keep=frozenset(),
                 users: dict | None = None, hosts: dict | None = None):
        if style not in STYLES:
            raise ValueError(f"unknown name style {style!r}")
        self._key = key
        self.style = style
        # Balanced style: whole names replaced the strict way, extra marker words, names never replaced.
        self.replace = {r.lower() for r in replace}
        self.hide = {h.lower() for h in hide}
        self.keep = {k.lower() for k in keep}
        # Shown user and computer names, any style (lowercased real -> shown).
        self.users = {k.lower(): v for k, v in (users or {}).items()}
        self.hosts = {k.lower(): v for k, v in (hosts or {}).items()}

    def risky(self, name: str, kind: str = "d") -> bool:
        """Balanced style: replaced whole, not kept."""
        low = name.lower()
        if low in self.keep:
            return False
        if low in self.replace or marked(name) or any(w.lower() in self.hide for w in _WORD.findall(name)):
            return True
        return kind == "f" and split_ext(name)[1][1:].lower() in IMAGE_EXTS

    def _digits(self, digits: str, seed: str, tag: str) -> str:
        first = "6789"[self._pick(seed, tag, "f", n=4)] if tag == "phone" else digits[0]
        rest = str(self._pick(seed, tag, n=10 ** (len(digits) - 1))).zfill(len(digits) - 1)
        return first + rest

    def in_place(self, name: str) -> str:
        """Balanced style: rewrite account numbers, phones, emails and browser site folders,
        keeping the name's shape."""
        m = _ORIGIN.match(name)
        if m and ("." in m.group(2) or m.group(2).lower() == "localhost") and m.group(1).lower() != "chrome-extension":
            site = self._choice(COMMON_SITES, m.group(2).lower(), "site")
            return f"{m.group(1)}_{site}_{m.group(3)}{m.group(4) or ''}"
        seed = name.lower()
        out = _EMAIL.sub(lambda e: (self._choice(FIRST_NAMES, e.group(0).lower(), "em")
                                    + str(self._pick(e.group(0).lower(), "en", n=100)) + "@"
                                    + self._choice(EMAIL_DOMAINS, e.group(0).lower(), "ed")), name)
        out = _STEAM64.sub(lambda s: "7656119" + self._digits(s.group(1), s.group(0), "steam"), out)

        wordy = bool(re.search(r"[g-zG-Z]{2}", out))

        def phone(tok: re.Match) -> str:
            word = tok.group(0)
            # Hex ids are not phones; a bare number counts only in a name that also has words.
            if not (re.search(r"[g-zG-Z]", word) or (word.isdigit() and wordy)):
                return word
            return _PHONE.sub(lambda p: (p.group(1) or "") + self._digits(p.group(2), seed, "phone"), word)
        return _WORD.sub(phone, out)

    def _strict_folder(self, name: str, category: str) -> str:
        seed = name.lower()
        if is_generic(name):
            return self._reroll_digits(name, seed)
        if category == "Code" and self._pick(seed, "proj", n=2):
            return self._choice(PROJECT_WORDS, seed, "pw") + self._choice(PROJECT_SUFFIXES, seed, "ps")
        words = FOLDER_WORDS.get(category, FOLDER_WORDS["Mixed"])
        return self._choice(words, seed, "f") + self._choice(QUALIFIERS, seed, "q")

    def _strict_file(self, stem: str, ext: str, seed: str) -> str:
        if ext[1:].lower() in IMAGE_EXTS:
            return self._fill(self._choice(DISC_TEMPLATES, seed, "t"), seed)
        if is_generic(stem):
            return self._reroll_digits(stem, seed)
        return self._fill(self._choice(FILE_TEMPLATES[ext_category(ext)], seed, "t"), seed)

    def _pick(self, *parts: object, n: int) -> int:
        msg = "\x1f".join(str(p) for p in parts).encode("utf-8")
        digest = hmac.new(self._key, msg, hashlib.sha256).digest()
        return int.from_bytes(digest[:8], "big") % n

    def _choice(self, seq, *parts):
        return seq[self._pick(*parts, n=len(seq))]

    def _reroll_digits(self, stem: str, seed: str) -> str:
        def one(m):
            d = m.group(0)
            if len(d) == 4 and d[:2] in ("19", "20"):
                return str(2015 + self._pick(seed, d, "yr", n=11))
            return str(self._pick(seed, d, len(d), n=10 ** len(d))).zfill(len(d))
        return re.sub(r"\d+", one, stem)

    def _fill(self, template: str, seed: str) -> str:
        p = self._pick
        return template.format(
            n2=str(1 + p(seed, "n2", n=60)).zfill(2), n3=str(p(seed, "n3", n=1000)).zfill(3),
            n4=str(p(seed, "n4", n=10000)).zfill(4), yr=2015 + p(seed, "yr", n=11),
            date=f"{2016 + p(seed, 'y', n=10)}{1 + p(seed, 'm', n=12):02d}{1 + p(seed, 'd', n=28):02d}")

    def _bump(self, stem: str, seed: str, attempt: int, sep: str = "-") -> str:
        if not attempt:
            return stem
        # Range grows x10 per attempt so a folder with thousands of same-named children settles.
        return f"{stem}{sep}{2 + self._pick(seed, 'again', attempt, n=10 ** min(attempt, 7))}"

    def folder(self, name: str, category: str = "Mixed", attempt: int = 0) -> str:
        if name.lower() in TECHNICAL:
            return self._bump(name, name.lower(), attempt, " ")
        seed = name.lower()
        if self.style == "codename":
            base = (self._choice(CODE_ADJ, seed, "a").title() + " "
                    + self._choice(CODE_NOUN, seed, "b").title())
        elif self.style == "balanced" and not self.risky(name):
            base = self.in_place(name)
        else:
            base = self._strict_folder(name, category)
        return self._bump(base, seed, attempt, " ")

    def file(self, name: str, attempt: int = 0) -> str:
        stem, ext = split_ext(name)
        seed = name.lower()
        if name.lower() in TECHNICAL:
            return self._bump(name, seed, attempt)
        if self.style == "codename":
            base = (self._choice(CODE_ADJ, seed, "a") + "-" + self._choice(CODE_NOUN, seed, "b")
                    + "-" + str(self._pick(seed, "n", n=100)).zfill(2))
            return self._bump(base, seed, attempt) + ext.lower()
        if self.style == "balanced" and not self.risky(name, "f"):
            return self._bump(self.in_place(stem), seed, attempt) + ext
        return self._bump(self._strict_file(stem, ext, seed), seed, attempt) + ext

    def user(self, name: str) -> str:
        if name.lower() in self.users:
            return self.users[name.lower()]
        if self.style == "balanced":
            return name
        return "demo" if self.style == "codename" else self._choice(FIRST_NAMES, name.lower(), "u")

    def host(self, name: str) -> str:
        seed = name.lower()
        if seed in self.hosts:
            return self.hosts[seed]
        if self.style == "balanced":
            return name
        if self.style == "codename":
            return self._choice(CODE_ADJ, seed, "ha") + "-" + self._choice(CODE_NOUN, seed, "hb") + "-pc"
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "DESKTOP-" + "".join(chars[self._pick(seed, "h", i, n=len(chars))] for i in range(7))
