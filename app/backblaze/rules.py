"""The 4-layer Backblaze exclusion engine.

A file present in a per-volume filelist is BACKED UP only if NO exclusion rule removes it.
Backblaze's four layers (outermost to innermost):

  1. bzexcluderules_mandatory.xml  - system-enforced `excludefname_rule` patterns (windows,
                                     program files, pagefile, Backblaze's own data, ...).
  2. bzexcluderules_editable.xml   - same `excludefname_rule` grammar, user-editable (advanced).
  3. bzinfo.xml <globalexcludes>   - excluded file extensions (all drives).
  4. bzinfo.xml <bzdirfilter none> - the user's excluded folders (matched RELATIVE to drive root,
                                     so they apply on every drive - the corrected 2026-06-14 model).

`excludefname_rule` semantics (from the file's own header comments):
  - case-insensitive; a file is excluded only if it matches ALL specified criteria (AND).
  - "*" means "skip this criterion".
  - skipFirstCharThenStartsWith: drop the first char (drive letter) then the rest must start with this.
  - contains_1 / contains_2: substring must be present.
  - doesNotContain: "|"-separated alternatives; if ANY is present the rule does NOT match.
  - endsWith: path must end with this.
  - hasFileExtension: the file's extension must equal this.
  - plat: the data's own platform ("win" or "mac"). Windows: osVers "*" (or matching "eleven")
    applies, "xp" is skipped. Mac: only osVers "*" (the rest name long-gone macOS releases).
  - On a Mac, rules see the path relative to its volume's mount ("/Volumes/X/a" -> "/a"), and
    skipFirstCharThenStartsWith drops the leading "/" the way Windows drops the drive letter.

This is what bbtoolkit DID NOT do (it only matched bare folder names + extensions, so its C:/E:
counts ran high). Validate the result per-drive against bzstat_totalbackup.xml.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from . import paths
from .safety import open_ro

_OS_OK = ("*", "eleven", "ten")  # we run Windows 11; treat ten/* as applicable, skip xp/seven-only


@dataclass
class PatternRule:
    skip: str          # skipFirstCharThenStartsWith (lowercased, "" if "*")
    c1: str
    c2: str
    dnc: tuple         # doesNotContain alternatives (lowercased), () if "*"
    ends: str
    ext: str           # hasFileExtension (lowercased, "" if "*")
    label: str         # human label for the impact view


def _attrs(element_text: str) -> dict:
    return {k.lower(): v for k, v in re.findall(r'([a-zA-Z_0-9]+)="([^"]*)"', element_text)}


def _read(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open_ro(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def _star(v: str) -> bool:
    return v == "" or v == "*"


def _load_pattern_rules(xml_text: str, source_label: str, plat: str = "win") -> list[PatternRule]:
    rules: list[PatternRule] = []
    for m in re.finditer(r"<excludefname_rule\b([^>]*)/?>", xml_text):
        a = _attrs(m.group(1))
        if a.get("plat", "").lower() != plat:
            continue
        osv = a.get("osvers", "*").lower()
        if plat == "mac":
            if osv != "*":
                continue
        elif osv not in _OS_OK and "eleven" not in osv:
            continue
        skip = a.get("skipfirstcharthenstartswith", "*")
        c1 = a.get("contains_1", "*")
        c2 = a.get("contains_2", "*")
        dnc = a.get("doesnotcontain", "*")
        ends = a.get("endswith", "*")
        ext = a.get("hasfileextension", "*")
        # Skip a fully-wildcard rule (would match everything - never present, but be safe).
        if all(_star(v) for v in (skip, c1, c2, ends, ext)) and _star(dnc):
            continue
        lbl = ends if not _star(ends) else (c1 if not _star(c1) else (skip if not _star(skip) else "ext:" + ext))
        rules.append(PatternRule(
            skip="" if _star(skip) else skip.lower(),
            c1="" if _star(c1) else c1.lower(),
            c2="" if _star(c2) else c2.lower(),
            dnc=() if _star(dnc) else tuple(x.lower() for x in dnc.split("|") if x),
            ends="" if _star(ends) else ends.lower(),
            ext="" if _star(ext) else ext.lower(),
            label=f"{source_label}:{lbl}",
        ))
    return rules


def _load_bzinfo(xml_text: str, mac: bool = False) -> tuple[list[str], set[str]]:
    """Return (excluded-folder prefixes lowercased, excluded-extension set). Windows prefixes are
    relative to the drive root; a Mac's keep their leading "/" and always end with one."""
    dirs: list[str] = []
    for m in re.finditer(r'<bzdirfilter\s+dir="([^"]+)"\s+whichfiles="none"', xml_text):
        p = m.group(1).lower()
        if mac:
            dirs.append(p if p.endswith("/") else p + "/")
            continue
        rel = p[2:] if len(p) > 2 and p[1] == ":" else p  # strip "x:" -> "\folder\..."
        dirs.append(rel)
    exts: set[str] = set()
    g = re.search(r'excludefiletypes="([^"]*)"', xml_text)
    if g:
        exts = {e.strip().lower() for e in g.group(1).split(",") if e.strip()}
    return sorted(set(dirs)), exts


def _ext_of(path_lc: str, sep: str = "\\") -> str:
    base = path_lc.rsplit(sep, 1)[-1]
    dot = base.rfind(".")
    return base[dot + 1:] if dot > 0 else ""


class RuleEngine:
    """Loaded exclusion rules + a fast classify(full_path) -> (excluded, rule_label)."""

    def __init__(self, dir_prefixes: list[str], ext_set: set[str], pattern_rules: list[PatternRule],
                 sep: str = "\\"):
        self.sep = sep
        self.dir_prefixes = dir_prefixes
        self.ext_set = ext_set
        self.pattern_rules = pattern_rules
        # Group pattern rules by their skip-prefix for cheap pruning (the file's own perf hint).
        self._always: list[PatternRule] = [r for r in pattern_rules if not r.skip]
        self._by_prefix: dict[str, list[PatternRule]] = {}
        for r in pattern_rules:
            if r.skip:
                self._by_prefix.setdefault(r.skip, []).append(r)
        self._prefix_items = list(self._by_prefix.items())

    def classify(self, full_path: str, mount: str | None = None) -> tuple[bool, str | None]:
        """`mount` is the volume's mount point (a Mac's "/Volumes/X"); Windows paths ignore it."""
        plc = full_path.lower()
        posix = self.sep == "/"
        # Layer 3: extension
        ext = _ext_of(plc, self.sep)
        if ext and ext in self.ext_set:
            return True, f"globalext:{ext}"
        # Layer 4: user folder exclusions (relative, all-drives)
        if posix:
            m = (mount or "/").lower().rstrip("/")
            rel = plc[len(m):] if m and plc.startswith(m + "/") else plc
        else:
            rel = plc[2:] if len(plc) > 2 and plc[1] == ":" else plc
        for d in self.dir_prefixes:
            if rel.startswith(d) or (posix and plc.startswith(d)):
                return True, f"folder:{d}"
        # Layers 1-2: pattern rules (a Mac's are matched on the volume-relative path)
        if posix:
            plc = rel
        tail = plc[1:]  # drop drive letter (Windows) or the leading "/" (Mac) for skipFirstChar tests
        candidates = self._always
        if self._prefix_items:
            extra = [r for pfx, rs in self._prefix_items if tail.startswith(pfx) for r in rs]
            if extra:
                candidates = candidates + extra
        for r in candidates:
            if r.c1 and r.c1 not in plc:
                continue
            if r.c2 and r.c2 not in plc:
                continue
            if r.dnc and any(x in plc for x in r.dnc):
                continue
            if r.ends and not plc.endswith(r.ends):
                continue
            if r.ext and ext != r.ext:
                continue
            return True, r.label
        return False, None


def engine_digest(engine: RuleEngine) -> str:
    """blake2b over the PARSED exclusion state classify() actually uses (all four layers).

    Hashing the parsed rules - not the raw XML files - keeps unrelated bzinfo.xml edits from
    forcing a spurious reclassify, while any real rule change always moves it. Field order is
    fixed and dir_prefixes/ext_set are sorted, so the digest is stable across loads."""
    h = hashlib.blake2b()
    h.update("\n".join(engine.dir_prefixes).encode("utf-8"))
    h.update("\n".join(sorted(engine.ext_set)).encode("utf-8"))
    for r in engine.pattern_rules:  # document order - stable across loads
        h.update("\n".join((r.skip, r.c1, r.c2, "|".join(r.dnc), r.ends, r.ext)).encode("utf-8"))
    return h.hexdigest()


def _plat() -> str:
    return "mac" if paths.platform() == "mac" else "win"


def load_engine() -> RuleEngine:
    """Load all four layers from the live bzdata (read-only), for the data's own platform."""
    plat = _plat()
    dirs, exts = _load_bzinfo(_read(paths.bzinfo_path()), plat == "mac")
    pats = _load_pattern_rules(_read(paths.mandatory_rules_path()), "mandatory", plat)
    pats += _load_pattern_rules(_read(paths.editable_rules_path()), "editable", plat)
    return RuleEngine(dirs, exts, pats, paths.sep())


def list_layers() -> dict:
    """Structured view of all 4 layers for the Exclusions tab (counts + raw entries)."""
    plat = _plat()
    dirs, exts = _load_bzinfo(_read(paths.bzinfo_path()), plat == "mac")
    mand = _load_pattern_rules(_read(paths.mandatory_rules_path()), "mandatory", plat)
    edit = _load_pattern_rules(_read(paths.editable_rules_path()), "editable", plat)
    return {
        "folders": dirs,            # layer 4 (user, editable via app UI)
        "extensions": sorted(exts), # layer 3 (user, editable via app UI)
        "editable_count": len(edit),# layer 2 (advanced, editable file)
        "mandatory_count": len(mand),  # layer 1 (system, read-only)
    }
