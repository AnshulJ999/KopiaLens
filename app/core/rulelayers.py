"""The rule lists that apply while a backup walks a source (kopia ignorefs semantics).

Kopia applies ignore rules in two dimensions (verified in ``fs/ignorefs/ignorefs.go`` v0.23.1 and
against a live repo):

1. A source's ROOT list comes from ONE policy - its own, else the nearest parent path policy, else
   ``user@host``, else ``@host``, else Global. Lists REPLACE, never merge.
2. As the walk enters a folder with its own defined policy, that policy's rules are added for that
   subtree, anchored at that folder; and every dot-ignore file (``.kopiaignore`` by default) whose
   NAME is in the folder's effective dot-ignore list is parsed and added, anchored there too.

The dot-ignore list is itself replaced by any defined policy at the folder: a policy that does not
set ``ignoreDotFiles`` (which is every policy unless someone set it) leaves the list EMPTY, so
dot-ignore files below it are never read. Verified on repo 3, 2026-09-23.

Evaluation is hierarchical, not one flat list: each level starts from its parent's decision and
only rules of the opposite polarity can flip it (``ignorefs.shouldIncludeByName``). ``decide``
reproduces that; ``excluded_in`` answers "is this path kept out of this source" the way kopia
would, including the rules of folder policies and of the ignore files found under it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from functools import lru_cache

from config import DATA_DIR

from . import paths as pathrules
from . import policy as policy_mod
from .applog import get_logger
from .ignorematch import compile_pattern
from .sources import sources_index

log = get_logger("rulelayers")

KIND_ROOT = "root"
KIND_POLICY = "policy"
KIND_FILE = "ignore_file"

DEFAULT_DOT_IGNORE = ".kopiaignore"

_MAP_NAME = "ignore_files_repo%s"
_MAP_VERSION = 1
_READ_MAX = 4 * 1024 * 1024          # a dot-ignore file is tiny; refuse anything absurd


# ---- path helpers (per SOURCE separator and case rule) ----------------------------------

def user_host_of(source_key: str) -> str:
    return source_key.split(":", 1)[0]


def norm(path: str, sep: str, fold: bool) -> str:
    path = path.replace(sep, "/").rstrip("/")
    return path.lower() if fold else path


def under(path: str, root: str, sep: str, fold: bool, strict: bool = False) -> bool:
    npath, nroot = norm(path, sep, fold), norm(root, sep, fold)
    if npath == nroot:
        return not strict
    return npath.startswith(nroot + "/") if nroot else True


def name_matches(name: str, want: str, case_sensitive: bool) -> bool:
    """Dot-ignore names match the way kopia asks the filesystem for them: Windows finds
    ``.KOPIAIGNORE`` for ``.kopiaignore``, a POSIX source does not."""
    return name == want if case_sensitive else name.lower() == want.lower()


def rel_of(path: str, root: str, sep: str, fold: bool) -> str:
    """``path`` relative to ``root`` with ``/`` separators ('' when equal). Case preserved."""
    npath, nroot = norm(path, sep, fold), norm(root, sep, fold)
    if npath == nroot:
        return ""
    if not npath.startswith(nroot + "/"):
        return ""
    return path.replace(sep, "/").rstrip("/")[len(nroot) + 1:]


def abs_of(root: str, rel: str, sep: str) -> str:
    return root.rstrip("\\/") + sep + rel.replace("/", sep) if rel else root


# ---- policies ---------------------------------------------------------------------------

def own_rules(pol: dict | None) -> list[str]:
    return list(((pol or {}).get("files") or {}).get("ignore") or [])


def _own_field(pol: dict | None, field: str) -> list[str] | None:
    val = ((pol or {}).get("files") or {}).get(field)
    return list(val) if val else None


def root_rules(repo: str, source_key: str, defined: dict) -> tuple[str, str | None, list[str]]:
    """(origin, owner, rules) for a source's ROOT list - same answer policytargets uses."""
    origin, owner = policy_mod.effective_ignore_origin(repo, source_key, defined)
    if origin == "global":
        return origin, owner, policy_mod.global_ignores(repo, defined)
    if owner is None:
        return origin, owner, []
    return origin, owner, own_rules(defined.get(owner))


def _target_index(defined: dict, sep: str, fold: bool) -> dict[str, tuple[str, dict]]:
    """{normalized target: (target, policy)} - so a folder can be looked up case-tolerantly."""
    out: dict[str, tuple[str, dict]] = {}
    for key, pol in (defined or {}).items():
        if key == policy_mod._GLOBAL_TARGET or ":" not in key:
            continue
        uh, path = key.split(":", 1)
        out.setdefault(f"{uh}:{norm(path, sep, fold)}", (key, pol))
    return out


def _nearest_field(repo: str, source_key: str, defined: dict, field: str) -> list[str] | None:
    """The value a source's policy chain defines for a ``files.*`` list field, nearest first.
    Kopia merges list fields by REPLACE, so the nearest definer is the effective value."""
    uh, sep, path = source_key.partition(":")

    def _get(target: str) -> list[str] | None:
        pol = defined.get(target)
        return _own_field(pol, field) if pol is not None else None

    own = _get(source_key)
    if own:
        return own
    if sep and path:
        sep_char = pathrules.path_sep(path)
        prefixes: list[str] = []
        cur = path.rstrip(sep_char)
        if sep_char in cur:
            while True:
                parent = cur.rsplit(sep_char, 1)[0] if sep_char in cur else ""
                if not parent or parent == sep_char:
                    break
                if not (len(parent) == 2 and parent[1] == ":"):
                    prefixes.append(f"{uh}:{parent}")
                cur = parent
            if sep_char == "\\":
                body = path.rstrip(sep_char)
                if len(body) >= 2 and body[1] == ":":
                    prefixes.append(f"{uh}:{body[:2]}{sep_char}")
            else:
                prefixes.append(f"{uh}:/")
        for prefix in sorted(set(prefixes), key=len, reverse=True):
            got = _get(prefix)
            if got:
                return got
        host = uh.partition("@")[2]
        for level in (uh, f"@{host}" if host else ""):
            if level:
                got = _get(level)
                if got:
                    return got
    return _get(policy_mod._GLOBAL_TARGET)


def root_dot_names(repo: str, source_key: str, defined: dict) -> list[str]:
    """The dot-ignore file names a source's root policy chain declares (kopia default when none)."""
    got = _nearest_field(repo, source_key, defined, "ignoreDotFiles")
    return list(got) if got else [DEFAULT_DOT_IGNORE]


def dot_names_at(repo: str, source_key: str, folder: str, defined: dict) -> list[str]:
    """The names kopia looks for IN ``folder``: the nearest defined policy at or above the folder
    replaces the list (an unset value therefore blanks it), otherwise it is inherited."""
    root = pathrules.source_root(source_key)
    sep = pathrules.sep(source_key)
    fold = not pathrules.case_sensitive(source_key)
    rel = rel_of(folder, root, sep, fold)
    names = root_dot_names(repo, source_key, defined)
    if not rel:
        return names
    uh = user_host_of(source_key)
    by_norm = _target_index(defined, sep, fold)
    segs = [s for s in rel.split("/") if s]
    for i in range(1, len(segs) + 1):
        # policy targets carry the drive/root ("user@host:C:\ProgramData"), not the relative path
        folder_path = root.rstrip("\\/") + sep + sep.join(segs[:i])
        hit = by_norm.get(f"{uh}:{norm(folder_path, sep, fold)}")
        if hit is not None:
            names = list(_own_field(hit[1], "ignoreDotFiles") or [])
    return names


def policy_layers(repo: str, source_key: str, defined: dict) -> list[dict]:
    """Defined path policies of this source's user@host whose folder is STRICTLY under its root
    and that own rules - the layer-2 rule lists the walk picks up."""
    root = pathrules.source_root(source_key)
    sep = pathrules.sep(source_key)
    fold = not pathrules.case_sensitive(source_key)
    uh = user_host_of(source_key)
    out: list[dict] = []
    for key, pol in (defined or {}).items():
        if key.startswith("(") or ":" not in key or key.split(":", 1)[0] != uh:
            continue
        rules = own_rules(pol)
        if not rules:
            continue
        folder = key.split(":", 1)[1]
        if not under(folder, root, sep, fold, strict=True):
            continue
        rel = rel_of(folder, root, sep, fold)
        out.append({"kind": KIND_POLICY, "owner": key, "anchor": folder, "rel": rel,
                    "name": None, "path": None, "rules": rules})
    out.sort(key=lambda l: (l["rel"].count("/"), norm(l["rel"], "/", fold)))
    return out


# ---- ignore files -----------------------------------------------------------------------

def declared_names(repo: str, defined: dict | None = None) -> list[str]:
    """Every dot-ignore name any defined policy declares, plus kopia's default name."""
    defined = defined if defined is not None else policy_mod.load_defined_policies(repo).get("policies") or {}
    names = {DEFAULT_DOT_IGNORE}
    for pol in (defined or {}).values():
        for name in _own_field(pol, "ignoreDotFiles") or []:
            if name:
                names.add(name)
    return sorted(names)


def parse_rules(text: str) -> list[str]:
    """kopia's ignore-file parser: skip comments (#) and blank lines; everything else is a rule."""
    out = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        out.append(line)
    return out


def read_rules(repo: str, content_id: str) -> tuple[list[str], str | None]:
    """Read one ignore file's contents through kopia (``show`` streams the file's bytes - the same
    call the restore download uses) and parse them. (rules, error)."""
    from .kopia import check_readonly, kopia
    if not content_id:
        return [], "no content id"
    args = ["show", content_id]
    try:
        check_readonly(args)
    except PermissionError as exc:
        return [], str(exc)
    res = kopia(repo, args)
    if res.returncode != 0:
        return [], f"kopia show failed (rc={res.returncode})"
    if len(res.stdout) > _READ_MAX:
        return [], "file too large to be an ignore file"
    return parse_rules(res.stdout), None


def discover_index(repo: str, names: list[str], source: str | None = None) -> list[dict]:
    """Index rows (in_latest) whose file NAME is one of ``names`` - one quick scan. The name
    match here is deliberately case-insensitive: discovery is a superset, and the in-effect
    check applies the source's own case rule."""
    from ..index.resolve import src_id as resolve_src_id
    from ..index.store import read_cursor
    if not names:
        return []
    lowered = sorted({n.lower() for n in names})
    ph = ",".join("?" * len(lowered))
    cur = read_cursor(repo)
    try:
        cond = f"p.in_latest AND lower(p.name) IN ({ph})"
        params: list = list(lowered)
        if source:
            sid = resolve_src_id(cur, source)
            if sid is None:
                return []
            cond += " AND p.src_id = ?"
            params.append(sid)
        rows = cur.execute(
            f"SELECT s.source, p.full_path, p.name, p.size, c.content_id, p.src_id "
            f"FROM pathx p JOIN src s ON s.src_id = p.src_id "
            f"LEFT JOIN cid c ON c.cid_id = p.cid_id WHERE {cond}", params).fetchall()
    finally:
        cur.close()
    return [{"source": r[0], "path": r[1], "name": r[2], "size": int(r[3] or 0),
             "content_id": r[4], "from": "index"} for r in rows]


def discover_disk(repo: str, names: list[str], source: str) -> list[dict]:
    """Dot-ignore files present ON DISK for a local source that the index does not hold (they are
    excluded from the backup, but kopia still reads them). Best-effort: needs a disk provider."""
    from . import disklookup
    root = pathrules.source_root(source)
    sep = pathrules.sep(source)
    case_sensitive = pathrules.case_sensitive(source)
    out: list[dict] = []
    for name in names:
        try:
            rows, _capped = disklookup.find(root, name, folders_only=False, limit=200)
        except Exception as exc:  # noqa: BLE001 - a disk provider hiccup must not fail discovery
            log.debug("disk lookup for %s under %s failed: %s", name, root, exc)
            return out
        for row in rows or []:
            path = row.get("path") or row.get("full_path") or ""
            if not path or not name_matches(os.path.basename(path), name, case_sensitive):
                continue
            folder = path.rsplit(sep, 1)[0] if sep in path else ""
            if folder and not under(path, root, sep, not pathrules.case_sensitive(source)):
                continue
            out.append({"source": source, "path": path, "name": os.path.basename(path),
                        "size": int(row.get("size") or 0), "content_id": None, "from": "disk"})
    return out


def _entry_key(entry: dict) -> tuple[str, str]:
    return (entry.get("source", ""), entry.get("path", ""))


def build_map(repo: str, names: list[str] | None = None) -> dict:
    """Discover every dot-ignore file (index first, disk for local sources), read the rules of the
    ones whose content changed, and keep the rest from the previous map. Returns the map payload."""
    defined = policy_mod.load_defined_policies(repo).get("policies") or {}
    names = names if names is not None else declared_names(repo, defined)
    prev = cached_map(repo) or {}
    prev_by_key = {_entry_key(e): e for e in prev.get("files") or []}

    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in discover_index(repo, names):
        key = _entry_key(row)
        if key in seen:
            continue
        seen.add(key)
        entries.append(row)
    sources = sorted(sources_index(repo))
    index_pairs = {_entry_key(r) for r in entries}
    local = [s for s in sources if pathrules.source_root(s)[:1].isalpha()
             and pathrules.source_root(s)[1:2] == ":"]
    for source in local:
        for row in discover_disk(repo, names, source):
            key = _entry_key(row)
            if key in index_pairs or key in seen:
                continue
            seen.add(key)
            entries.append(row)

    out: list[dict] = []
    read = 0
    for row in entries:
        key = _entry_key(row)
        prior = prev_by_key.get(key)
        same = bool(prior is not None and row.get("content_id")
                    and prior.get("content_id") == row.get("content_id"))
        if same:
            item = dict(prior)
            item.pop("in_effect", None)
        else:
            if row.get("content_id"):
                rules, err = read_rules(repo, row["content_id"])
            else:
                rules, err = _rules_from_disk(row["path"])
            read += 1
            item = {**row, "rules": rules, "error": err}
            item["priced"] = None
        out.append(item)

    payload = {"version": _MAP_VERSION, "ts": time.time(), "names": names, "files": out,
               "read": read}
    if not _write_map(repo, payload):
        log.error("ignore-file map write failed repo=%s", repo)
    log.info("ignore files repo=%s: %d found, %d read, %d reused", repo, len(out), read,
             len(out) - read)
    return payload


def _rules_from_disk(path: str) -> tuple[list[str], str | None]:
    try:
        if not os.path.exists(path):
            return [], "not readable"
        if os.path.getsize(path) > _READ_MAX:
            return [], "file too large to be an ignore file"
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_rules(fh.read()), None
    except OSError as exc:
        return [], str(exc)


def cached_map(repo: str) -> dict | None:
    from ..index import sidecache
    data = sidecache.read(_MAP_NAME % repo, base=DATA_DIR)
    if isinstance(data, dict) and data.get("version") == _MAP_VERSION:
        return data
    return None


def _write_map(repo: str, payload: dict) -> bool:
    from ..index import sidecache
    return bool(sidecache.write(_MAP_NAME % repo, payload, base=DATA_DIR))


_MAP_MEMO: dict[str, tuple[float, dict]] = {}
_MAP_MEMO_TTL = 60.0      # the map moves once a night; a request loop must not re-read it per call


def map_for(repo: str) -> dict:
    """The ignore-file map the nightly built, or an empty map when it has not run yet.

    Requests never read kopia: the rules come from the worker's artifact, memoized for a minute
    because the exclusion simulation asks per source and per candidate target."""
    now = time.time()
    hit = _MAP_MEMO.get(repo)
    if hit is not None and now - hit[0] < _MAP_MEMO_TTL:
        return hit[1]
    data = cached_map(repo)
    if data is None:
        data = {"version": _MAP_VERSION, "ts": None, "names": [], "files": []}
    _MAP_MEMO[repo] = (now, data)
    return data


def _file_layers(repo: str, source_key: str, defined: dict, ignore_map: dict) -> list[dict]:
    """The ignore-file layers that are IN EFFECT for this source, anchored at their folders."""
    root = pathrules.source_root(source_key)
    sep = pathrules.sep(source_key)
    fold = not pathrules.case_sensitive(source_key)
    case_sensitive = pathrules.case_sensitive(source_key)
    uh = user_host_of(source_key)
    out: list[dict] = []
    seen: set[str] = set()
    for entry in ignore_map.get("files") or []:
        path = entry.get("path") or ""
        if not path or user_host_of(entry.get("source") or "") != uh:
            continue
        if not under(path, root, sep, fold):
            continue
        key = norm(path, sep, fold)
        if key in seen:
            continue      # the same file found through two backups is one layer
        folder = path.rsplit(sep, 1)[0] if sep in path else path
        names = dot_names_at(repo, source_key, folder, defined)
        same_name = [n for n in names if name_matches(n, str(entry.get("name", "")), case_sensitive)]
        if not same_name or entry.get("error"):
            continue
        rules = list(entry.get("rules") or [])
        if not rules:
            continue
        seen.add(key)
        out.append({"kind": KIND_FILE, "owner": None, "anchor": folder,
                    "rel": rel_of(folder, root, sep, fold), "name": entry.get("name"),
                    "path": path, "rules": rules})
    out.sort(key=lambda l: (l["rel"].count("/"), norm(l["rel"], "/", fold)))
    return out


# ---- the layer set and its evaluation ----------------------------------------------------

def layers_for_source(repo: str, source_key: str, *, defined: dict | None = None,
                      ignore_map: dict | None = None) -> list[dict]:
    """Every rule list that applies while walking this source, in kopia's evaluation order:
    the root list first, then each directory's lists as the walk descends. Within ONE directory
    the policy's rules come before the dot-ignore files' rules (``ignorefs.buildContext``), so a
    root-level ignore file is evaluated before a policy on a folder below it."""
    defined = defined if defined is not None else policy_mod.load_defined_policies(repo).get("policies") or {}
    origin, owner, rules = root_rules(repo, source_key, defined)
    root = pathrules.source_root(source_key)
    layers = [{"kind": KIND_ROOT, "owner": owner, "anchor": root, "rel": "",
               "name": None, "path": None, "rules": rules, "origin": origin}]
    layers.extend(policy_layers(repo, source_key, defined))
    if ignore_map is None:
        try:
            ignore_map = map_for(repo)
        except Exception as exc:  # noqa: BLE001 - the map is an add-on, never a hard dependency
            log.warning("ignore-file map unavailable repo=%s: %s", repo, exc)
            ignore_map = {}
    layers.extend(_file_layers(repo, source_key, defined, ignore_map))
    fold = not pathrules.case_sensitive(source_key)
    # Depth first (rel "" is the walk root); at one depth the policy's rules precede the
    # dot-ignore file's. The root layer sorts first on its empty rel and root kind.
    layers.sort(key=lambda l: (len([s for s in l["rel"].split("/") if s]),
                               1 if l["kind"] == KIND_FILE else 0,
                               norm(l["rel"], "/", fold)))
    return layers


@lru_cache(maxsize=20000)
def _compiled(rule: str, base: str):
    return compile_pattern(rule, base_dir=base)


def _compiled_layers(layers: list[dict]) -> list[list]:
    out = []
    for layer in layers:
        base = "/" + layer["rel"] if layer.get("rel") else ""
        out.append([_compiled(r, base) for r in layer.get("rules") or []])
    return out


def _level_decide(rules: list, text: str, is_dir: bool, included_before: bool) -> bool:
    """One kopia level: start from the parent's decision, then only opposite-polarity rules can
    flip it. Returns the new "included" state."""
    should_ignore = not included_before
    for pat in rules:
        if (not should_ignore and not pat.negated) or (should_ignore and pat.negated):
            should_ignore = pat.match(text, is_dir)
    return not should_ignore


def _layers_at(layers: list[dict], item_rel: str) -> list[dict]:
    """The layers that apply to ``item_rel``: those anchored at its parent folder or above."""
    parent = item_rel.rsplit("/", 1)[0] if "/" in item_rel else ""
    out = []
    for layer in layers:
        anchor = layer.get("rel") or ""
        if not anchor or parent == anchor or parent.startswith(anchor + "/"):
            out.append(layer)
    return out


def _layers_in_dir(layers: list[dict], dir_rel: str) -> list[dict]:
    """The layers that apply to items INSIDE ``dir_rel``: anchored at that folder or above."""
    out = []
    for layer in layers:
        anchor = layer.get("rel") or ""
        if not anchor or dir_rel == anchor or dir_rel.startswith(anchor + "/"):
            out.append(layer)
    return out


def decide(layers: list[dict], item_rel: str, is_dir: bool) -> bool:
    """Is this item (path relative to the source root, '/'-separated, no leading slash) kept out?
    kopia's order: parent context first, then each level's own rules - a deeper level can NEGATE
    an ancestor level's decision, so every applicable level is folded."""
    text = "/" + item_rel
    included = True
    for rules in _compiled_layers(_layers_at(layers, item_rel)):
        included = _level_decide(rules, text, is_dir, included)
    return not included


def excluded_in(repo: str, source_key: str, rel: str, is_folder: bool = False, *,
                defined: dict | None = None, ignore_map: dict | None = None) -> bool:
    """Does this source keep this path out today (root list + folder policies + ignore files)?"""
    if not rel and not is_folder:
        return False
    layers = layers_for_source(repo, source_key, defined=defined, ignore_map=ignore_map)
    segs = [s for s in rel.strip("/").split("/") if s]
    for i in range(1, len(segs)):
        if decide(layers, "/".join(segs[:i]), True):
            return True
    if not segs:
        return decide(layers, "__kl_probe__", False)
    item = "/".join(segs)
    if is_folder:
        if decide(layers, item, True):
            return True
        return decide(layers, item + "/__kl_probe__", False)
    return decide(layers, item, False)


def _decide_why(layers: list[dict], item_rel: str, is_dir: bool) -> dict | None:
    """``decide`` that also names the rule and layer that left the item out (None when kept)."""
    text = "/" + item_rel
    ignored, why = False, None
    for layer in _layers_at(layers, item_rel):
        base = "/" + layer["rel"] if layer.get("rel") else ""
        for rule in layer.get("rules") or []:
            pat = _compiled(rule, base)
            if (not ignored and not pat.negated) or (ignored and pat.negated):
                hit = pat.match(text, is_dir)
                if hit and not ignored:
                    why = {"rule": rule, **{k: layer.get(k) for k in ("kind", "owner", "anchor", "name", "path")}}
                ignored = hit
    return why if ignored else None


def why_excluded(repo: str, source_key: str, rel: str, is_folder: bool = False, *,
                 defined: dict | None = None) -> dict | None:
    """The rule that keeps this path out (same walk as ``excluded_in``), with ``via`` naming the
    folder above it when that folder is what is left out. None when nothing does."""
    if not rel and not is_folder:
        return None
    layers = layers_for_source(repo, source_key, defined=defined)
    segs = [s for s in rel.strip("/").split("/") if s]
    for i in range(1, len(segs)):
        why = _decide_why(layers, "/".join(segs[:i]), True)
        if why:
            return {**why, "via": "/".join(segs[:i])}
    if not segs:
        return _decide_why(layers, "__kl_probe__", False)
    item = "/".join(segs)
    if is_folder:
        return _decide_why(layers, item, True) or _decide_why(layers, item + "/__kl_probe__", False)
    return _decide_why(layers, item, False)


def applies_at(repo: str, source_key: str, folder_rel: str, *, defined: dict | None = None,
               ignore_map: dict | None = None) -> list[dict]:
    """The rule layers that apply to items INSIDE ``folder_rel`` of this source, with their rules -
    the "why is this excluded" list (a folder's own policy counts here, unlike for the folder
    itself, which its PARENT's layers decide)."""
    layers = layers_for_source(repo, source_key, defined=defined, ignore_map=ignore_map)
    out = []
    for layer in _layers_in_dir(layers, folder_rel):
        out.append({k: layer.get(k) for k in ("kind", "anchor", "rel", "owner", "path", "name",
                                              "rules", "origin")})
    return out


# ---- scopes ------------------------------------------------------------------------------

def walkthrough_sources(repo: str, folder: str, *, user_host: str | None = None,
                        include_equal: bool = True) -> list[str]:
    """Sources whose walk reaches ``folder``: root at (or, with ``include_equal`` false, strictly
    above) the folder. Kopia only applies a policy to sources of the SAME user@host, so pass it."""
    out: list[str] = []
    for key in sorted(sources_index(repo)):
        if user_host is not None and user_host_of(key) != user_host:
            continue
        sep = pathrules.sep(key)
        fold = not pathrules.case_sensitive(key)
        root = pathrules.source_root(key)
        if not under(folder, root, sep, fold):
            continue
        if not include_equal and norm(folder, sep, fold) == norm(root, sep, fold):
            continue
        out.append(key)
    return out


def scope_for_target(repo: str, target: str, defined: dict | None = None) -> list[dict]:
    """Every backup a write to ``target`` can affect, each with the anchor its rules price at.

    Root layer: the target itself plus every source whose root list comes from it (their own root
    is the anchor). Folder layer: every source whose walk passes through the target's folder - its
    rules apply there anchored at that folder, which is the case plain per-source pricing misses.
    """
    defined = defined if defined is not None else policy_mod.load_defined_policies(repo).get("policies") or {}
    out: list[dict] = []
    seen: set[str] = set()
    if target == policy_mod._GLOBAL_TARGET or ":" not in target:
        for key in sorted(sources_index(repo)):
            origin, owner = policy_mod.effective_ignore_origin(repo, key, defined)
            if (target == policy_mod._GLOBAL_TARGET and origin == "global") or owner == target:
                out.append({"source": key, "anchor": None, "layer": KIND_ROOT})
                seen.add(key)
        return out
    folder = target.split(":", 1)[1]
    for key in sorted(sources_index(repo)):
        origin, owner = policy_mod.effective_ignore_origin(repo, key, defined)
        if key == target or (origin == "inherited" and owner == target):
            out.append({"source": key, "anchor": None, "layer": KIND_ROOT})
            seen.add(key)
    for key in walkthrough_sources(repo, folder, user_host=user_host_of(target),
                                   include_equal=False):
        if key not in seen:
            out.append({"source": key, "anchor": folder, "layer": KIND_POLICY})
    return out


def folder_scope(repo: str, folder: str, user_host: str | None = None) -> list[dict]:
    """Every backup whose walk reaches ``folder`` (root at or above it), anchored AT the folder -
    the scope of an ignore file's rules or any rule list anchored there."""
    return [{"source": key, "anchor": folder, "layer": KIND_FILE}
            for key in walkthrough_sources(repo, folder, user_host=user_host, include_equal=True)]


def rules_id(rules: list[str]) -> str:
    """Stable hash of a rule list, for pricing fingerprints."""
    return hashlib.md5(json.dumps(list(rules)).encode()).hexdigest()
