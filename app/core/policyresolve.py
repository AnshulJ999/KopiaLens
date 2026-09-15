"""Policy inheritance resolver: a line-by-line port of kopia v0.23.1's merge (snapshot/policy).

Resolves a target's effective policy and where each field came from, from the defined policies
(`policy export`), so the editor can preview a draft before it is saved. `show_lines` renders the
result the way plain `kopia policy show` prints it; `check` compares the two for real policies.

Policies are the export's JSON dicts. A definition maps a field's JSON path to the target key that
supplied it; a field absent from the map was never set and kopia reports it as "(global)".
"""

from __future__ import annotations

import copy
import datetime as dt

from .applog import get_logger

log = get_logger("policyresolve")

GLOBAL = "(global)"

DEFAULTS: dict = {
    "retention": {"keepLatest": 10, "keepHourly": 48, "keepDaily": 7, "keepWeekly": 4,
                  "keepMonthly": 24, "keepAnnual": 3, "ignoreIdenticalSnapshots": False},
    "files": {"ignoreDotFiles": [".kopiaignore"]},
    "errorHandling": {"ignoreFileErrors": False, "ignoreDirectoryErrors": False, "ignoreUnknownTypes": True},
    "scheduling": {"runMissed": True},
    "compression": {"compressorName": "none"},
    "metadataCompression": {"compressorName": "zstd-fastest"},
    "splitter": {},
    "actions": {},
    "osSnapshots": {"volumeShadowCopy": {"enable": 0}},
    "logging": {"directories": {"snapshotted": 5, "ignored": 5},
                "entries": {"snapshotted": 0, "ignored": 5, "cacheHit": 0, "cacheMiss": 0}},
    "upload": {"maxParallelSnapshots": 1, "parallelUploadAboveSize": 2 << 30},
}

COMPRESSORS = ["none", "zstd", "zstd-fastest", "zstd-better-compression", "s2-default", "s2-better",
               "s2-parallel-4", "s2-parallel-8", "gzip", "gzip-best-speed", "gzip-best-compression",
               "pgzip", "pgzip-best-speed", "pgzip-best-compression", "deflate-best-speed",
               "deflate-default", "deflate-best-compression"]
SPLITTERS = [f"{kind}-{size}{suffix}" for kind, suffix in (("FIXED", ""), ("DYNAMIC", "-BUZHASH"),
                                                            ("DYNAMIC", "-RABINKARP"))
             for size in ("128K", "256K", "512K", "1M", "2M", "4M", "8M")]
VSS_MODES = {0: "never", 1: "always", 2: "when-available"}


# ---- dict helpers ---------------------------------------------------------------------------

def get(d: dict, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def put(d: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def drop(d: dict, path: str) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.get(part)
        if not isinstance(cur, dict):
            return
    cur.pop(parts[-1], None)


# ---- source info ----------------------------------------------------------------------------

def parse_key(key: str) -> tuple[str, str, str]:
    """Target key -> (user, host, path); the global policy is ("", "", "")."""
    if key in (GLOBAL, "global", ""):
        return "", "", ""
    head, _, path = key.partition(":")
    user, _, host = head.partition("@")
    return user, host, path


def make_key(user: str, host: str, path: str) -> str:
    if not user and not host and not path:
        return GLOBAL
    return f"{user}@{host}" if not path else f"{user}@{host}:{path}"


def parent_path(p: str) -> str:
    """Port of getParentPathOSIndependent."""
    vol, pth = (p[:2], p[2:]) if len(p) >= 3 and p[1] == ":" and p[2] in "/\\" else ("", p)
    last = max(pth.rfind("/"), pth.rfind("\\"))
    if last == len(pth) - 1 and last != 0:
        pth = pth[:last]
        last = max(pth.rfind("/"), pth.rfind("\\"))
    if last < 0:
        return p
    if last == 0:
        return vol + pth[0:1]
    return vol + pth[:last]


def hierarchy(defined: dict, key: str) -> list[tuple[str, dict]]:
    """Port of GetPolicyHierarchy: (target key, policy) most specific first."""
    user, host, path = parse_key(key)
    found: list[tuple[str, dict]] = []
    tmp = path
    while path != "":
        k = make_key(user, host, tmp)
        if k in defined:
            found.append((k, defined[k]))
        parent = parent_path(tmp)
        if parent == tmp:
            break
        tmp = parent
    for k in (make_key(user, host, ""), make_key("", host, ""), GLOBAL):
        if k in defined:
            found.append((k, defined[k]))
    target = make_key(user, host, path)
    if not found or found[0][0] != target:
        found.insert(0, (target, {}))
    return found


# ---- merge ----------------------------------------------------------------------------------

def _set_def(df: dict, path: str, si: str) -> None:
    df[path] = si


def _merge_optional(m: dict, s: dict, df: dict, si: str, path: str) -> None:
    if get(m, path) is None and get(s, path) is not None:
        put(m, path, copy.deepcopy(get(s, path)))
        _set_def(df, path, si)


def _merge_truthy(m: dict, s: dict, df: dict, si: str, path: str) -> None:
    """mergeInt64 / mergeBool / mergeString: zero values count as unset on both sides."""
    if not get(m, path) and get(s, path):
        put(m, path, copy.deepcopy(get(s, path)))
        _set_def(df, path, si)


def _merge_list_replace(m: dict, s: dict, df: dict, si: str, path: str) -> None:
    if not get(m, path) and get(s, path):
        put(m, path, list(get(s, path)))
        _set_def(df, path, si)


def _merge_strings(m: dict, s: dict, df: dict, si: str, path: str, flag: str) -> None:
    """mergeStrings: union, sorted, unless an earlier policy stopped merging."""
    if get(m, flag):
        return
    merged = set(get(m, path) or [])
    src = get(s, path) or []
    for v in src:
        merged.add(v)
        _set_def(df, path, si)
    if merged:
        put(m, path, sorted(merged))
    else:
        drop(m, path)
    if get(s, flag):
        put(m, flag, True)


def _tod_key(t: dict) -> tuple[int, int]:
    return int(t.get("hour", 0)), int(t.get("min", 0))


def _merge_group(m: dict, s: dict, df: dict, si: str) -> None:
    for f in ("keepLatest", "keepHourly", "keepDaily", "keepWeekly", "keepMonthly", "keepAnnual",
              "ignoreIdenticalSnapshots"):
        _merge_optional(m, s, df, si, "retention." + f)

    _merge_list_replace(m, s, df, si, "files.ignore")
    _merge_truthy(m, s, df, si, "files.noParentIgnore")
    _merge_list_replace(m, s, df, si, "files.ignoreDotFiles")
    _merge_truthy(m, s, df, si, "files.noParentDotFiles")
    _merge_optional(m, s, df, si, "files.ignoreCacheDirs")
    _merge_truthy(m, s, df, si, "files.maxFileSize")
    _merge_optional(m, s, df, si, "files.oneFileSystem")

    for f in ("ignoreFileErrors", "ignoreDirectoryErrors", "ignoreUnknownTypes"):
        _merge_optional(m, s, df, si, "errorHandling." + f)

    _merge_truthy(m, s, df, si, "scheduling.intervalSeconds")
    src_tod = get(s, "scheduling.timeOfDay") or []
    if src_tod:
        _set_def(df, "scheduling.timeOfDay", si)
        if not get(m, "scheduling.noParentTimeOfDay"):
            both = [dict(t) for t in src_tod] + [dict(t) for t in (get(m, "scheduling.timeOfDay") or [])]
            dedup: dict = {}
            for t in sorted(both, key=_tod_key):
                dedup.setdefault(_tod_key(t), {"hour": _tod_key(t)[0], "min": _tod_key(t)[1]})
            put(m, "scheduling.timeOfDay", list(dedup.values()))
    _merge_list_replace(m, s, df, si, "scheduling.cron")
    if get(s, "scheduling.noParentTimeOfDay"):
        put(m, "scheduling.noParentTimeOfDay", True)
    _merge_truthy(m, s, df, si, "scheduling.manual")
    _merge_optional(m, s, df, si, "scheduling.runMissed")

    for f in ("maxParallelSnapshots", "maxParallelFileReads", "parallelUploadAboveSize"):
        _merge_optional(m, s, df, si, "upload." + f)

    _merge_truthy(m, s, df, si, "compression.compressorName")
    _merge_truthy(m, s, df, si, "compression.minSize")
    _merge_truthy(m, s, df, si, "compression.maxSize")
    _merge_strings(m, s, df, si, "compression.onlyCompress", "compression.noParentOnlyCompress")
    _merge_strings(m, s, df, si, "compression.neverCompress", "compression.noParentNeverCompress")
    _merge_truthy(m, s, df, si, "metadataCompression.compressorName")
    _merge_truthy(m, s, df, si, "splitter.algorithm")

    _merge_optional(m, s, df, si, "actions.beforeSnapshotRoot")
    _merge_optional(m, s, df, si, "actions.afterSnapshotRoot")
    _merge_optional(m, s, df, si, "osSnapshots.volumeShadowCopy.enable")
    for f in ("directories.snapshotted", "directories.ignored", "entries.snapshotted",
              "entries.ignored", "entries.cacheHit", "entries.cacheMiss"):
        _merge_optional(m, s, df, si, "logging." + f)


def merge(policies: list[tuple[str, dict]]) -> tuple[dict, dict]:
    """Port of MergePolicies. Returns (effective policy, definition)."""
    merged: dict = {}
    df: dict = {}
    for si, pol in policies:
        _merge_group(merged, pol or {}, df, si)
        if (pol or {}).get("noParent"):
            return merged, df
    _merge_group(merged, DEFAULTS, df, GLOBAL)
    if policies:
        first = policies[0][1] or {}
        for f in ("beforeFolder", "afterFolder"):
            val = get(first, "actions." + f)
            if val is not None:
                put(merged, "actions." + f, copy.deepcopy(val))
            else:
                drop(merged, "actions." + f)
    return merged, df


def resolve(defined: dict, key: str, draft: dict | None = None) -> tuple[dict, dict]:
    """Effective policy + definition for ``key``; ``draft`` replaces the target's stored policy
    (the state after a save)."""
    target = make_key(*parse_key(key))
    pols = dict(defined)
    if draft is not None:
        pols[target] = draft
    return merge(hierarchy(pols, target))


# ---- plain-text `policy show` rendering (cli/command_policy_show.go) --------------------------

def bytes_string(n: float) -> str:
    """units.BytesString (base 10)."""
    f = float(n)
    prefixes = ["", "K", "M", "G", "T", "P", "E"]
    for p in prefixes:
        if f < 0.9 * 1000:
            return f"{_nice(f)} {p}B"
        f /= 1000
    return f"{_nice(f)} {prefixes[-1]}B"


def _nice(f: float) -> str:
    return (f"{f:.1f}").rstrip("0").rstrip(".")


def go_duration(seconds: int) -> str:
    """time.Duration.String for whole seconds."""
    if seconds == 0:
        return "0s"
    neg, s = seconds < 0, abs(int(seconds))
    h, rem = divmod(s, 3600)
    mi, sec = divmod(rem, 60)
    if h:
        out = f"{h}h{mi}m{sec}s"
    elif mi:
        out = f"{mi}m{sec}s"
    else:
        out = f"{sec}s"
    return "-" + out if neg else out


def tod_string(t: dict) -> str:
    return f"{int(t.get('hour', 0))}:{int(t.get('min', 0)):02d}"


def _b(v) -> str:
    return "true" if v else "false"


def show_lines(eff: dict, df: dict, key: str) -> list[str]:
    """What plain `kopia policy show` prints for this target, whitespace-normalized."""
    target = make_key(*parse_key(key))
    rows: list[tuple[str, str, str]] = []

    def d(path: str) -> str:
        src = df.get(path, GLOBAL)
        return "(defined for this target)" if src == target else "inherited from " + src

    def opt(path: str) -> str:
        v = get(eff, path)
        return "-" if v is None else str(v)

    def optb(path: str, default: bool) -> str:
        v = get(eff, path)
        return _b(default if v is None else v)

    rows.append(("Retention:", "", ""))
    for label, f in (("Annual", "keepAnnual"), ("Monthly", "keepMonthly"), ("Weekly", "keepWeekly"),
                     ("Daily", "keepDaily"), ("Hourly", "keepHourly"), ("Latest", "keepLatest")):
        rows.append((f"{label} snapshots:", opt("retention." + f), d("retention." + f)))
    rows.append(("Ignore identical snapshots:", optb("retention.ignoreIdenticalSnapshots", False),
                 d("retention.ignoreIdenticalSnapshots")))

    rows.append(("Files policy:", "", ""))
    rows.append(("Ignore cache directories:", optb("files.ignoreCacheDirs", True), d("files.ignoreCacheDirs")))
    rules = get(eff, "files.ignore") or []
    if rules:
        rows.append(("Ignore rules:", "", d("files.ignore")))
        rows.extend((r, "", "") for r in rules)
    else:
        rows.append(("No ignore rules:", "", ""))
    dots = get(eff, "files.ignoreDotFiles") or []
    if dots:
        rows.append(("Read ignore rules from files:", "", d("files.ignoreDotFiles")))
        rows.extend((r, "", "") for r in dots)
    if (get(eff, "files.maxFileSize") or 0) > 0:
        rows.append(("Ignore files above:", bytes_string(get(eff, "files.maxFileSize")), d("files.maxFileSize")))
    rows.append(("Scan one filesystem only:", optb("files.oneFileSystem", False), d("files.oneFileSystem")))

    rows.append(("Error handling policy:", "", ""))
    for label, f, default in (("Ignore file read errors:", "ignoreFileErrors", False),
                              ("Ignore directory read errors:", "ignoreDirectoryErrors", False),
                              ("Ignore unknown types:", "ignoreUnknownTypes", True)):
        rows.append((label, optb("errorHandling." + f, default), d("errorHandling." + f)))

    rows.append(("Scheduling policy:", "", ""))
    rows.append(("Scheduled snapshots:", "", ""))
    has_any = False
    iv = int(get(eff, "scheduling.intervalSeconds") or 0)
    if iv:
        rows.append(("Snapshot interval:", go_duration(iv), d("scheduling.intervalSeconds")))
        has_any = True
    tods = get(eff, "scheduling.timeOfDay") or []
    if tods:
        rows.append(("Run missed snapshots:", optb("scheduling.runMissed", False), d("scheduling.runMissed")))
        rows.append(("Snapshot times:", "", d("scheduling.timeOfDay")))
        rows.extend((tod_string(t), "", "") for t in tods)
        has_any = True
    crons = get(eff, "scheduling.cron") or []
    if crons:
        rows.append(("Crontab expressions:", "", d("scheduling.cron")))
        rows.extend((c, "", "") for c in crons)
        has_any = True
    if not has_any:
        rows.append(("None.", "", ""))
    rows.append(("Manual snapshot:", _b(get(eff, "scheduling.manual")), d("scheduling.manual")))

    rows.append(("Uploads:", "", ""))
    rows.append(("Max parallel snapshots (server/UI):", opt("upload.maxParallelSnapshots"),
                 d("upload.maxParallelSnapshots")))
    rows.append(("Max parallel file reads:", opt("upload.maxParallelFileReads"), d("upload.maxParallelFileReads")))
    pua = get(eff, "upload.parallelUploadAboveSize")
    rows.append(("Parallel upload above size:", "-" if pua is None else bytes_string(pua),
                 d("upload.parallelUploadAboveSize")))

    comp = get(eff, "compression.compressorName") or ""
    if comp in ("", "none"):
        rows.append(("Compression disabled.", "", ""))
    else:
        rows.append(("Compression:", "", ""))
        rows.append(("Compressor:", comp, d("compression.compressorName")))
        only, never = get(eff, "compression.onlyCompress") or [], get(eff, "compression.neverCompress") or []
        if only:
            rows.append(("Only compress files with the following extensions:", "", d("compression.onlyCompress")))
            rows.extend(("- " + r, "", "") for r in only)
        elif never:
            rows.append(("Compress all files except the following extensions:", "", d("compression.neverCompress")))
            rows.extend((r, "", "") for r in never)
        else:
            rows.append(("Compress files regardless of extensions.", "", ""))
        mn, mx = int(get(eff, "compression.minSize") or 0), int(get(eff, "compression.maxSize") or 0)
        if mx > 0:
            rows.append((f"Only compress files between {bytes_string(mn)} and {bytes_string(mx)}.", "", ""))
        elif mn > 0:
            rows.append((f"Only compress files bigger than {bytes_string(mn)}.", "", ""))
        else:
            rows.append(("Compress files of all sizes.", "", ""))

    mcomp = get(eff, "metadataCompression.compressorName") or ""
    if mcomp in ("", "none"):
        rows.append(("Metadata compression disabled.", "", ""))
    else:
        rows.append(("Metadata compression:", "", ""))
        rows.append(("Compressor:", mcomp, d("metadataCompression.compressorName")))

    rows.append(("Splitter:", "", ""))
    rows.append(("Algorithm override:", get(eff, "splitter.algorithm") or "(repository default)", d("splitter.algorithm")))

    any_actions = False
    for label, f, inheritable in (("Run command before snapshot root:", "beforeSnapshotRoot", True),
                                  ("Run command after snapshot root:", "afterSnapshotRoot", True),
                                  ("Run command before this folder:", "beforeFolder", False),
                                  ("Run command after this folder:", "afterFolder", False)):
        h = get(eff, "actions." + f)
        if h is None:
            continue
        rows.append((label, "", d("actions." + f) if inheritable else "(non-inheritable)"))
        if h.get("script"):
            rows.append(("Embedded script (stored in repository):", "", ""))
            rows.extend((ln, "", "") for ln in str(h["script"]).splitlines())
        else:
            rows.append(("Command:", "", ""))
            rows.append((" ".join([h.get("path") or ""] + list(h.get("args") or [])), "", ""))
        rows.append(("Mode:", h.get("mode") or "sync", ""))
        rows.append(("Timeout:", go_duration(int(h.get("timeout") or 0)), ""))
        any_actions = True
    if not any_actions:
        rows.append(("No actions defined.", "", ""))

    rows.append(("OS-level snapshot support:", "", ""))
    vss = get(eff, "osSnapshots.volumeShadowCopy.enable")
    rows.append(("Volume Shadow Copy:", VSS_MODES.get(int(vss or 0), "never"), d("osSnapshots.volumeShadowCopy.enable")))

    rows.append(("Logging details (0-none, 10-maximum):", "", ""))
    for label, f in (("Directory snapshotted:", "directories.snapshotted"), ("Directory ignored:", "directories.ignored"),
                     ("Entry snapshotted:", "entries.snapshotted"), ("Entry ignored:", "entries.ignored"),
                     ("Entry cache hit:", "entries.cacheHit"), ("Entry cache miss:", "entries.cacheMiss")):
        v = get(eff, "logging." + f)
        rows.append((label, str(0 if v is None else v), d("logging." + f)))

    return [norm(" ".join(p for p in r if p)) for r in rows if any(r)]


def norm(line: str) -> str:
    return " ".join(line.split())


def parse_show_text(text: str) -> list[str]:
    """Plain `kopia policy show` output -> normalized non-empty lines, header dropped."""
    out = []
    for ln in (text or "").splitlines():
        n = norm(ln)
        if not n or n.startswith("Policy for "):
            continue
        out.append(n)
    return out


def diff_lines(ours: list[str], theirs: list[str]) -> list[str]:
    """First few differing lines, for a log message."""
    diffs = []
    for i in range(max(len(ours), len(theirs))):
        a = ours[i] if i < len(ours) else "<missing>"
        b = theirs[i] if i < len(theirs) else "<missing>"
        if a != b:
            diffs.append(f"line {i + 1}: ours '{a}' vs kopia '{b}'")
            if len(diffs) >= 5:
                break
    return diffs


# ---- validation (kopia refuses these at `policy import`) --------------------------------------

def validate(key: str, pol: dict) -> list[str]:
    from .schedule import CronExpr, strip_cron_comment
    errors: list[str] = []
    sched = pol.get("scheduling") or {}
    if sched.get("manual"):
        others = {k: v for k, v in sched.items() if k != "manual" and v not in (None, False, 0, "", [])}
        if others:
            errors.append("Manual only can't be combined with other schedule settings.")
    for expr in sched.get("cron") or []:
        line = strip_cron_comment(expr)
        if line:
            try:
                CronExpr(line)
            except ValueError as exc:
                errors.append(f"Cron line \"{expr}\" is not valid: {exc}.")
    for t in sched.get("timeOfDay") or []:
        if not (0 <= int(t.get("hour", -1)) <= 23 and 0 <= int(t.get("min", -1)) <= 59):
            errors.append(f"Time of day {t.get('hour')}:{t.get('min')} is not valid.")
    _user, _host, path = parse_key(key)
    if path and get(pol, "upload.maxParallelSnapshots") is not None:
        errors.append("Snapshots at the same time can only be set on Global, a user or a computer policy.")
    if path and path[-1] in "/\\" and not _is_root(path):
        errors.append("A policy path cannot end with a slash.")
    return errors


def _is_root(p: str) -> bool:
    if len(p) >= 2 and p[1] == ":":
        return p[2:] in ("", "\\", "/")
    return p in ("/", "\\")


# ---- editor field catalogue ---------------------------------------------------------------------
# kind: optional = pointer field (Inherit / value); plain = zero means unset, so a child can change
# but never clear an ancestor's value; list = a non-empty list replaces the parent's;
# concat = merged with the parent's unless the noParent flag is set.

FIELDS: list[dict] = [
    {"path": "retention.keepLatest", "section": "retention", "label": "Keep latest", "help": "The newest snapshots, whatever their age", "type": "int", "kind": "optional"},
    {"path": "retention.keepHourly", "section": "retention", "label": "Keep hourly", "help": "One per hour, for this many hours", "type": "int", "kind": "optional"},
    {"path": "retention.keepDaily", "section": "retention", "label": "Keep daily", "help": "One per day, for this many days", "type": "int", "kind": "optional"},
    {"path": "retention.keepWeekly", "section": "retention", "label": "Keep weekly", "help": "One per week, for this many weeks", "type": "int", "kind": "optional"},
    {"path": "retention.keepMonthly", "section": "retention", "label": "Keep monthly", "help": "One per month, for this many months", "type": "int", "kind": "optional"},
    {"path": "retention.keepAnnual", "section": "retention", "label": "Keep annual", "help": "One per year, for this many years", "type": "int", "kind": "optional"},
    {"path": "retention.ignoreIdenticalSnapshots", "section": "retention", "label": "Skip identical snapshots", "help": "Don't keep a new snapshot when nothing changed", "type": "bool", "kind": "optional", "default": False},

    {"path": "scheduling.intervalSeconds", "section": "schedule", "label": "Snapshot every", "help": "A fixed interval", "type": "interval", "kind": "plain"},
    {"path": "scheduling.timeOfDay", "section": "schedule", "label": "Times of day", "help": "Added to the times set above this policy", "type": "tod", "kind": "concat", "flag": "scheduling.noParentTimeOfDay"},
    {"path": "scheduling.noParentTimeOfDay", "section": "schedule", "label": "Ignore times set above", "help": "Use only this policy's times of day", "type": "flag", "kind": "plain"},
    {"path": "scheduling.cron", "section": "schedule", "label": "Cron", "help": "One line per schedule. Replaces the lines set above.", "type": "cron", "kind": "list"},
    {"path": "scheduling.runMissed", "section": "schedule", "label": "Run missed snapshots", "help": "Run once after the computer was off at a scheduled time", "type": "bool", "kind": "optional", "default": False},
    {"path": "scheduling.manual", "section": "schedule", "label": "Manual only", "help": "Never runs on a schedule. Can't be combined with other schedule settings.", "type": "flag", "kind": "plain"},

    {"path": "files.ignoreDotFiles", "section": "files", "label": "Rule files", "help": "File names that hold extra exclusion rules, like .kopiaignore", "type": "strlist", "kind": "list"},
    {"path": "files.noParentDotFiles", "section": "files", "label": "Ignore rule files set above", "help": "", "type": "flag", "kind": "plain"},
    {"path": "files.ignoreCacheDirs", "section": "files", "label": "Skip cache folders", "help": "Folders marked with a CACHEDIR.TAG file", "type": "bool", "kind": "optional", "default": True},
    {"path": "files.maxFileSize", "section": "files", "label": "Skip files larger than", "help": "Bigger files are left out", "type": "bytes", "kind": "plain"},
    {"path": "files.oneFileSystem", "section": "files", "label": "Stay on one drive", "help": "Don't follow into other mounted drives", "type": "bool", "kind": "optional", "default": False},

    {"path": "compression.compressorName", "section": "compression", "label": "Compression", "help": "How file data is compressed", "type": "select", "options": "compressors", "kind": "plain"},
    {"path": "metadataCompression.compressorName", "section": "compression", "label": "Metadata compression", "help": "Folder listings and other metadata", "type": "select", "options": "compressors", "kind": "plain"},
    {"path": "compression.onlyCompress", "section": "compression", "label": "Only compress", "help": "File extensions to compress. Added to the list set above.", "type": "strlist", "kind": "concat", "flag": "compression.noParentOnlyCompress"},
    {"path": "compression.noParentOnlyCompress", "section": "compression", "label": "Ignore the Only compress list set above", "help": "", "type": "flag", "kind": "plain"},
    {"path": "compression.neverCompress", "section": "compression", "label": "Never compress", "help": "File extensions to store as they are. Added to the list set above.", "type": "strlist", "kind": "concat", "flag": "compression.noParentNeverCompress"},
    {"path": "compression.noParentNeverCompress", "section": "compression", "label": "Ignore the Never compress list set above", "help": "", "type": "flag", "kind": "plain"},
    {"path": "compression.minSize", "section": "compression", "label": "Smallest file to compress", "help": "", "type": "bytes", "kind": "plain"},
    {"path": "compression.maxSize", "section": "compression", "label": "Largest file to compress", "help": "", "type": "bytes", "kind": "plain"},

    {"path": "errorHandling.ignoreFileErrors", "section": "advanced", "group": "Error handling", "label": "Ignore file read errors", "help": "A file that can't be read doesn't fail the snapshot", "type": "bool", "kind": "optional", "default": False},
    {"path": "errorHandling.ignoreDirectoryErrors", "section": "advanced", "group": "Error handling", "label": "Ignore folder read errors", "help": "", "type": "bool", "kind": "optional", "default": False},
    {"path": "errorHandling.ignoreUnknownTypes", "section": "advanced", "group": "Error handling", "label": "Ignore unknown file types", "help": "Sockets, devices and similar", "type": "bool", "kind": "optional", "default": True},
    {"path": "upload.maxParallelSnapshots", "section": "advanced", "group": "Upload", "label": "Snapshots at the same time", "help": "Global, user and computer policies only", "type": "int", "kind": "optional", "global_only": True},
    {"path": "upload.maxParallelFileReads", "section": "advanced", "group": "Upload", "label": "Files read at the same time", "help": "Blank means automatic", "type": "int", "kind": "optional"},
    {"path": "upload.parallelUploadAboveSize", "section": "advanced", "group": "Upload", "label": "Upload in parts above", "help": "Bigger files upload in parallel parts", "type": "bytes", "kind": "optional"},
    {"path": "actions.beforeSnapshotRoot", "section": "advanced", "group": "Snapshot scripts", "label": "Before snapshot", "help": "Runs before the whole snapshot starts", "type": "action", "kind": "optional"},
    {"path": "actions.afterSnapshotRoot", "section": "advanced", "group": "Snapshot scripts", "label": "After snapshot", "help": "Runs after it finishes", "type": "action", "kind": "optional"},
    {"path": "actions.beforeFolder", "section": "advanced", "group": "Snapshot scripts", "label": "Before this folder", "help": "Only on this policy, never inherited", "type": "action", "kind": "local"},
    {"path": "actions.afterFolder", "section": "advanced", "group": "Snapshot scripts", "label": "After this folder", "help": "Only on this policy, never inherited", "type": "action", "kind": "local"},
    {"path": "osSnapshots.volumeShadowCopy.enable", "section": "advanced", "group": "Windows shadow copy", "label": "Use shadow copy", "help": "Backs up open or locked files from a frozen copy of the drive. Needs admin rights.", "type": "vss", "kind": "optional", "default": 0},
    {"path": "splitter.algorithm", "section": "advanced", "group": "Splitter", "label": "Splitter override", "help": "How files are cut into chunks", "type": "select", "options": "splitters", "kind": "plain"},
    {"path": "logging.directories.snapshotted", "section": "advanced", "group": "Logging", "label": "Folders backed up", "help": "Detail level 0-10", "type": "loglevel", "kind": "optional"},
    {"path": "logging.directories.ignored", "section": "advanced", "group": "Logging", "label": "Folders skipped", "help": "", "type": "loglevel", "kind": "optional"},
    {"path": "logging.entries.snapshotted", "section": "advanced", "group": "Logging", "label": "Files backed up", "help": "", "type": "loglevel", "kind": "optional"},
    {"path": "logging.entries.ignored", "section": "advanced", "group": "Logging", "label": "Files skipped", "help": "", "type": "loglevel", "kind": "optional"},
    {"path": "logging.entries.cacheHit", "section": "advanced", "group": "Logging", "label": "Files unchanged", "help": "", "type": "loglevel", "kind": "optional"},
    {"path": "logging.entries.cacheMiss", "section": "advanced", "group": "Logging", "label": "Files read again", "help": "", "type": "loglevel", "kind": "optional"},
    {"path": "noParent", "section": "advanced", "group": "Inheritance", "label": "Stop applying parent policies", "help": "Nothing above this policy applies here, kopia's own defaults included", "type": "flag", "kind": "plain"},
]
FIELD_PATHS = {f["path"] for f in FIELDS}


def apply_fields(pol: dict, fields: dict) -> dict:
    """A copy of ``pol`` with field edits applied; None (or an empty value) removes the field."""
    out = copy.deepcopy(pol or {})
    for path, value in (fields or {}).items():
        if path not in FIELD_PATHS:
            raise ValueError(f"unknown policy field: {path}")
        if value is None or value == [] or value is False and _kind(path) == "plain":
            drop(out, path)
        else:
            put(out, path, copy.deepcopy(value))
    return out


def _kind(path: str) -> str:
    return next((f["kind"] for f in FIELDS if f["path"] == path), "optional")


def check(repo: str, defined: dict, targets: list[str]) -> dict:
    """Compare the resolver with plain `kopia policy show` for each target (read-only kopia calls).
    Returns {checked, mismatches: [{target, diff}], failed: [target]}."""
    from .kopia import kopia
    out: dict = {"checked": 0, "mismatches": [], "failed": []}
    for key in targets:
        args = ["policy", "show", "--global"] if key in (GLOBAL, "global") else ["policy", "show", key]
        try:
            res = kopia(repo, args)
        except Exception as exc:  # noqa: BLE001 - one bad target must not stop the check
            log.warning("repo=%s policy check: show failed for %s: %s", repo, key, exc)
            out["failed"].append(key)
            continue
        if res.returncode != 0:
            out["failed"].append(key)
            continue
        eff, df = resolve(defined, key)
        diffs = diff_lines(show_lines(eff, df, key), parse_show_text(res.stdout))
        out["checked"] += 1
        if diffs:
            out["mismatches"].append({"target": key, "diff": diffs})
    return out


def next_snapshots(eff: dict, last_dt: dt.datetime | None, count: int = 3) -> list[str]:
    from .schedule import next_times
    now = dt.datetime.now()
    return [t.isoformat() for t in next_times(eff.get("scheduling") or {}, last_dt, now, count)]
