"""Exclusion targets for the exclusion dialog: which policies can receive a rule for a path, what
each write would change, and which ones the Smart default picks.

Kopia applies ignore rules in two layers (kopia ``GetPolicyHierarchy`` + ``ignorefs``):

1. A source's root list comes from ONE policy: its own, else the nearest parent path policy, else
   ``user@host``, else ``@host``, else Global. Lists replace, never merge.
2. While a snapshot walks into a subfolder that has its own policy, that policy's rules are added
   for that subtree, anchored at that folder.

Every target is evaluated by simulation: for each source holding the path, is the path excluded
with today's policies, and with the write applied? Smart picks the policies the sources already
get their rules from; child sources are offered too, with the inherited rules copied in.
"""

from __future__ import annotations

import re

from . import paths as pathrules
from . import rulelayers
from .applog import get_logger
from .ignorematch import compile_pattern, escape_name
from .policy import _GLOBAL_TARGET, effective_ignore_origin, global_ignores
from .sources import sources_index

log = get_logger("policytargets")

_SHAPES = ("exact", "name", "extension", "similar", "parent")
_WHERES = ("any", "here", "under")


class TargetError(ValueError):
    """Bad request shape/path for target resolution."""


def _norm(p: str, sep: str, fold: bool) -> str:
    p = p.replace(sep, "/").rstrip("/")
    return p.lower() if fold else p


def _source_roots(repo: str, host_filter: str | None) -> list[str]:
    keys = sorted(sources_index(repo))
    if host_filter:
        uh = host_filter.split(":", 1)[0]
        keys = [k for k in keys if k.split(":", 1)[0] == uh]
    return keys


def _under(path: str, root: str, sep: str, fold: bool, strict: bool = False) -> bool:
    npath, nroot = _norm(path, sep, fold), _norm(root, sep, fold)
    if npath == nroot:
        return not strict
    return npath.startswith(nroot + "/") if nroot else True


def _covering_sources(repo: str, path: str, host_filter: str | None) -> list[str]:
    """Sources whose root contains ``path`` (per-source separator and case rules)."""
    out = []
    for key in _source_roots(repo, host_filter):
        if _under(path, pathrules.source_root(key), pathrules.sep(key),
                  not pathrules.case_sensitive(key)):
            out.append(key)
    return out


def _rel(path: str, root: str, sep: str, fold: bool) -> str:
    """Path relative to ``root``, '/'-separated ('' when equal). Case is PRESERVED in the result
    - kopia's matcher is case-sensitive against real paths - while the prefix match honours the
    source's case rules. _norm keeps length, so positions align."""
    npath, nroot = _norm(path, sep, fold), _norm(root, sep, fold)
    if npath == nroot:
        return ""
    if not npath.startswith(nroot + "/"):
        raise TargetError(f"path {path!r} is not under {root!r}")
    return path.replace(sep, "/").rstrip("/")[len(nroot) + 1:]


_STAR = object()


def similar_leaf(name: str, is_folder: bool = False) -> str:
    """A name pattern for names like this one: digit runs become '*' and stars joined only by
    separators collapse ("ws-2026-09-21.log" -> "ws-*.log"). No digits: '*' before the extension.
    The literal parts are escaped, so a name holding '[' or '*' stays literal there."""
    stem, ext = name, ""
    if not is_folder and "." in name.lstrip("."):
        stem, ext = name.rsplit(".", 1)
        ext = "." + ext
    parts = re.split(r"\d+", stem)
    if len(parts) == 1:
        return escape_name(stem, end=False) + "*" + escape_name(ext, start=False)
    toks: list = [parts[0]]
    for lit in parts[1:]:
        if len(toks) >= 2 and toks[-2] is _STAR and re.fullmatch(r"[-_.: ]*", toks[-1]):
            toks.pop()                      # only separators between two stars: one star
        else:
            toks.append(_STAR)
        toks.append(lit)
    last = len(toks) - 1
    body = "".join("*" if t is _STAR else escape_name(t, start=i == 0, end=i == last and not ext)
                   for i, t in enumerate(toks))
    return body + escape_name(ext, start=False)


def _pattern_for_shape(shape: str, rel: str, is_folder: bool, level: int = 0,
                       where: str = "any", leaf: str | None = None) -> str | None:
    """The pattern text as it lives in the TARGET's policy (anchored = relative to the target's
    root). ``level`` counts folders up from the path; ``where`` limits a name-type rule to the
    containing folder ("here"), to anything under the folder ``level`` up ("under"), or not at all.
    Returns None when the shape does not apply here (e.g. that folder is above the target)."""
    if shape not in _SHAPES:
        raise TargetError(f"unknown shape {shape!r} (expected one of {_SHAPES})")
    if where not in _WHERES:
        raise TargetError(f"unknown scope {where!r} (expected one of {_WHERES})")
    segs = [s for s in rel.split("/") if s]
    lit = [escape_name(s) for s in segs]
    if shape == "parent":
        if len(segs) < 2:
            return None  # already at the target root: there is no level up
        shape, level = "exact", 1
    if shape == "exact":
        if level <= 0:
            if not segs:
                return "/**" if is_folder else None  # the target root itself: only a folder rule exists
            return "/" + "/".join(lit) + ("/**" if is_folder else "")
        if level > len(segs):
            return None
        anc = lit[:len(segs) - level]
        return "/" + "/".join(anc) + "/**" if anc else "/**"

    base = segs[-1] if segs else ""
    if not base:
        return None
    if shape == "name":
        # A folder name rule is directory-only ("Name/"): it matches at any depth and prunes the
        # subtree. "Name/**" would be rooted and miss nested folders.
        tail = escape_name(base)
    elif shape == "extension":
        if is_folder or "." not in base.lstrip("."):
            return None
        tail = "*." + escape_name(base.rsplit(".", 1)[-1])
    else:
        tail = (leaf or "").strip().strip("/") or similar_leaf(base, is_folder)
        if "/" in tail:
            raise TargetError("a name pattern cannot contain /")
    if is_folder:
        tail += "/"
    if where == "any":
        return tail
    if where == "here":
        anc = lit[:-1]
        return ("/" + "/".join(anc) if anc else "") + "/" + tail
    k = max(level, 1)
    if k > len(segs):
        return None
    anc = lit[:len(segs) - k]
    return ("/" + "/".join(anc) if anc else "") + "/**/" + tail


def _matches(pattern: str, rel: str, is_folder: bool) -> bool:
    """Does one rule, anchored at a source root, keep this selection out? A directory-only rule
    matches the folder itself; other folder rules are probed through a synthetic child."""
    pat = compile_pattern(pattern)
    if pat.dir_only:
        return is_folder and pat.match("/" + (rel or ""), is_dir=True)
    probe = "/" + rel if not is_folder else "/" + (rel or "") + "/__kl_probe__"
    return pat.match(probe, is_dir=False)


def _own_rules(defined: dict, key: str) -> list[str]:
    return list(((defined.get(key) or {}).get("files") or {}).get("ignore") or [])


def _root_rules(repo: str, source_key: str, defined: dict) -> tuple[str, str | None, list[str]]:
    origin, owner = effective_ignore_origin(repo, source_key, defined)
    if origin == "global":
        return origin, owner, global_ignores(repo, defined)
    if owner is None:
        return origin, owner, []
    return origin, owner, _own_rules(defined, owner)


def _excluded_in(repo: str, source_key: str, rel: str, is_folder: bool, defined: dict) -> bool:
    """Is ``rel`` (relative to the source root) kept out of this source's snapshots?

    Delegates to the shared rule layer (/): the root list, the rules of every path
    policy on a folder the walk passes through, and every dot-ignore file in effect, evaluated in
    kopia's order (parent context first, a deeper level can negate it). A folder equal to the
    source root counts as left out when its rules leave out everything inside it."""
    return rulelayers.excluded_in(repo, source_key, rel, is_folder, defined=defined)


def _target_label_kind(key: str) -> str:
    if key == _GLOBAL_TARGET:
        return "global"
    if ":" not in key:
        return "userhost"   # user@host or @host
    return "path"


def _uses(i: dict) -> str | None:
    """The policy a source's root rule list comes from."""
    return _GLOBAL_TARGET if i["origin"] == "global" else i["owner"]


def _smart_rank(t: dict) -> tuple:
    """Deepest path policy first, then host-level, then Global."""
    key = "global" if t["is_global"] else t["target"]
    if key == "global":
        return (0, 0)
    if ":" not in key:
        return (1, 0)
    root = pathrules.source_root(key)
    return (2, len([s for s in root.split(pathrules.path_sep(root)) if s]))


def _evaluate(repo: str, path: str, covering: list[str], source: str | None, is_folder: bool,
              spec: dict, defined: dict) -> dict:
    """Every target for one rule spec ({shape, level, where, leaf}), with before/after coverage
    per covering source."""
    info = []
    for key in covering:
        origin, owner, rules = _root_rules(repo, key, defined)
        root = pathrules.source_root(key)
        rel = _rel(path, root, pathrules.sep(key), not pathrules.case_sensitive(key))
        info.append({"source": key, "root": root, "rel": rel, "origin": origin, "owner": owner,
                     "rules": rules, "before": _excluded_in(repo, key, rel, is_folder, defined)})
    anchor = next((i for i in info if i["source"] == source), info[0])

    # candidates: owners (Smart), each covering source itself, and Global
    cands: dict[str, dict] = {}
    for i in info:
        if i["origin"] == "none":
            cands.setdefault(i["source"], {"role": "start"})
        else:
            owner = _GLOBAL_TARGET if i["origin"] == "global" else i["owner"]
            cands.setdefault(owner, {"role": "owner"})
    for i in info:
        if i["source"] not in cands:
            cands[i["source"]] = {"role": "child", "inherits_from": (
                "global" if i["origin"] == "global" else i["owner"]), "inherited_rules": i["rules"]}
    # Folder policies on this path that already hold rules: every backup walking through the
    # folder applies them (layer 2), and adding one changes no backup's own rule list.
    hosts = {i["source"].split(":", 1)[0] for i in info}
    for key in defined:
        if key in cands or ":" not in key or key.split(":", 1)[0] not in hosts \
                or not _own_rules(defined, key):
            continue
        kroot = pathrules.source_root(key)
        ksep, kfold = pathrules.sep(key), not pathrules.case_sensitive(key)
        if _under(path, kroot, ksep, kfold) and \
                any(_under(kroot, i["root"], ksep, kfold, strict=True) for i in info):
            cands[key] = {"role": "folder"}
    cands.setdefault(_GLOBAL_TARGET, {"role": "global"})

    def pattern(rel: str) -> str | None:
        return _pattern_for_shape(spec["shape"], rel, is_folder, spec.get("level", 0),
                                  spec.get("where", "any"), spec.get("leaf"))

    targets = []
    for key, meta in cands.items():
        kind = _target_label_kind(key)
        pats: list[str] = []
        roots: dict[str, list[str]] = {}
        if kind == "path":
            troot = pathrules.source_root(key)
            p = pattern(_rel(path, troot, pathrules.sep(key), not pathrules.case_sensitive(key)))
            pats = [p] if p else []
        else:
            # Global and host-level rules anchor at each using source's root: an anchored rule
            # needs its own text per distinct root; an unanchored one collapses to one rule.
            users = [i for i in info if _uses(i) == key] or [anchor]
            for i in users:
                p = pattern(i["rel"])
                if not p:
                    continue
                if p not in roots:
                    pats.append(p)
                    roots[p] = []
                roots[p].append(i["root"])
        own = global_ignores(repo, defined) if key == _GLOBAL_TARGET else _own_rules(defined, key)
        copied = list(meta.get("inherited_rules") or [])
        covers = []
        for i in info:
            after = i["before"]
            if pats and not i["before"]:
                new_list = (copied if meta["role"] == "child" else own) + pats
                after_defined = dict(defined)
                after_defined[key] = {**(defined.get(key) or {}),
                                      "files": {**((defined.get(key) or {}).get("files") or {}),
                                                "ignore": new_list}}
                after = _excluded_in(repo, i["source"], i["rel"], is_folder, after_defined)
            covers.append({"source": i["source"], "root": i["root"], "rel_path": i["rel"],
                           "matches": bool(pats) and after, "already_excluded": i["before"]})
        protects = sum(1 for c in covers if c["matches"] and not c["already_excluded"])
        targets.append({
            "target": "global" if key == _GLOBAL_TARGET else key,
            "role": meta["role"],
            "is_global": key == _GLOBAL_TARGET,
            "existing_rule_count": len(own),
            "will_create_policy": meta["role"] in ("start", "child") or (kind == "path" and key not in defined),
            "inherits_from": meta.get("inherits_from"),
            "inherited_rules": copied,
            "uses_count": sum(1 for i in info if _uses(i) == key),
            "pattern": pats[0] if pats else None,
            "patterns": pats,
            "pattern_roots": [{"pattern": p, "roots": roots[p]} for p in pats] if len(pats) > 1 else [],
            "pattern_anchored": any(p.startswith("/") for p in pats),
            "covers": covers,
            "protects": protects,
            "smart": meta["role"] in ("owner", "start") and protects > 0,
            "impact": None,
        })

    # One write is enough when a single policy keeps the path out of every backup still holding
    # it: Smart picks only that policy, the deepest one on a tie.
    pending = [idx for idx, i in enumerate(info) if not i["before"]]
    whole = [t for t in targets if pending and t["patterns"]
             and t["role"] in ("owner", "folder", "start")
             and all(t["covers"][idx]["matches"] for idx in pending)]
    if whole:
        best = max(whole, key=_smart_rank)
        for t in targets:
            t["smart"] = t is best
    return {"sources": info, "targets": targets}


def _coverage(ev: dict) -> tuple[int, int]:
    """(sources left out with the Smart picks applied, sources holding the path)."""
    total = len(ev["sources"])
    covered = 0
    for idx, i in enumerate(ev["sources"]):
        if i["before"] or any(t["smart"] and t["covers"][idx]["matches"] for t in ev["targets"]):
            covered += 1
    return covered, total


_ROLE_ORDER = {"owner": 0, "start": 0, "folder": 0, "global": 1, "child": 2}


def path_status(repo: str, source: str, path: str, is_folder: bool = False) -> dict:
    """Which rule list a source uses for ``path`` and whether any rule already leaves it out."""
    from .policy import load_defined_policies

    defined = load_defined_policies(repo).get("policies") or {}
    origin, owner, rules = _root_rules(repo, source, defined)
    root = pathrules.source_root(source)
    sep, fold = pathrules.sep(source), not pathrules.case_sensitive(source)
    if not _under(path, root, sep, fold):
        raise TargetError(f"path {path!r} is not under source {source!r}")
    rel = _rel(path, root, sep, fold)
    return {"source": source, "origin": origin,
            "owner": "global" if origin == "global" else owner,
            "rule_count": len(rules), "excluded": _excluded_in(repo, source, rel, is_folder, defined)}


def _levels(path: str, source_key: str, is_folder: bool) -> list[dict]:
    """The path and each folder above it up to the source root: level 0 is the path itself."""
    root = pathrules.source_root(source_key)
    sep = pathrules.sep(source_key)
    segs = [s for s in _rel(path, root, sep, not pathrules.case_sensitive(source_key)).split("/") if s]
    root_name = [s for s in root.split(sep) if s][-1:] or [root]
    out = []
    for k in range(len(segs) + 1):
        name = segs[len(segs) - 1 - k] if k < len(segs) else root_name[0].rstrip(sep) or root
        out.append({"level": k, "name": name, "folder": k > 0 or is_folder, "root": k == len(segs)})
    return out


def resolve_targets(repo: str, path: str, source: str | None = None,
                    is_folder: bool = False, shape: str = "exact",
                    price: bool = False, suggest: bool = False, level: int = 0,
                    where: str = "any", leaf: str | None = None) -> dict:
    """Targets for this path, Smart picks first. ``suggest`` also returns ``smart_shape``: the
    shape whose Smart picks keep the path out of the most sources (exact on a tie). ``price`` adds
    per-target impact for the matching covers (small scopes only). ``levels`` names the folders
    above the path for the level picker; ``similar_default`` is the suggested similar-names leaf."""
    from ..index import policypricing  # local import: keeps the index layer out of import time
    from .policy import load_defined_policies

    if not path or not path.strip():
        raise TargetError("path is required")
    if shape not in _SHAPES:
        raise TargetError(f"unknown shape {shape!r} (expected one of {_SHAPES})")
    if where not in _WHERES:
        raise TargetError(f"unknown scope {where!r} (expected one of {_WHERES})")
    spec = {"shape": shape, "level": max(0, min(int(level or 0), 64)), "where": where, "leaf": leaf}
    defined = load_defined_policies(repo).get("policies") or {}
    host = source.split(":", 1)[0] if source else None
    covering = _covering_sources(repo, path, host)
    if not covering:
        return {"repo": repo, "path": path, "shape": shape, "sources": [], "targets": [],
                "note": "no configured source contains this path", "smart_shape": None,
                "levels": [], "similar_default": None, "sep": None}
    anchor_key = source if source in covering else covering[0]

    ev = _evaluate(repo, path, covering, source, is_folder, spec, defined)

    smart_shape = None
    if suggest:
        best = (_coverage(ev)[0], shape)
        for alt in ("exact", "name"):
            if alt == shape:
                continue
            alt_ev = _evaluate(repo, path, covering, source, is_folder,
                               {"shape": alt, "level": 0, "where": "any", "leaf": None}, defined)
            got = _coverage(alt_ev)[0]
            if got > best[0] or (got == best[0] and alt == "exact" and best[1] != "exact"):
                best = (got, alt)
        smart_shape = best[1]

    if price:
        for t in ev["targets"]:
            if not t["pattern"]:
                continue
            by_source, files_total, bytes_total = [], 0, 0
            for cover in t["covers"]:
                if not cover["matches"] or cover["already_excluded"]:
                    continue
                res = policypricing.price_patterns(repo, cover["source"], [t["pattern"]], limit=0)["results"][0]
                if res["error"]:
                    continue
                by_source.append({"source": cover["source"], "files": res["files"], "bytes": res["bytes"]})
                files_total += res["files"]
                bytes_total += res["bytes"]
            t["impact"] = {"files": files_total, "bytes": bytes_total, "by_source": by_source}

    ev["targets"].sort(key=lambda t: (not t["smart"], _ROLE_ORDER[t["role"]], -t["protects"],
                                      -len(t["covers"]), t["target"]))
    sources = [{"source": i["source"], "root": i["root"], "origin": i["origin"], "owner": i["owner"],
                "already_excluded": i["before"]} for i in ev["sources"]]
    base = [pathrules.basename(path) or path]
    return {"repo": repo, "path": path, "shape": shape, "sources": sources,
            "targets": ev["targets"], "smart_shape": smart_shape,
            "levels": _levels(path, anchor_key, is_folder),
            "similar_default": similar_leaf(base[0], is_folder),
            "sep": pathrules.sep(anchor_key)}


_VARIANTS = {
    False: (("This file", {"shape": "exact"}),
            ("Same name anywhere", {"shape": "name"}),
            ("Same extension anywhere", {"shape": "extension"}),
            ("Same extension in this folder", {"shape": "extension", "where": "here"}),
            ("Similar names anywhere", {"shape": "similar"}),
            ("Its folder", {"shape": "exact", "level": 1})),
    True: (("This folder", {"shape": "exact"}),
           ("Same folder name anywhere", {"shape": "name"}),
           ("Similar folder names anywhere", {"shape": "similar"}),
           ("Its parent folder", {"shape": "exact", "level": 1})),
}


def variants(repo: str, path: str, source: str | None = None, is_folder: bool = False) -> dict:
    """Ready-made rules for the file/folder panel: each variant's text as it would be written into
    the policy Smart picks for it."""
    rows = []
    for label, spec in _VARIANTS[bool(is_folder)]:
        full = {"shape": spec["shape"], "level": spec.get("level", 0),
                "where": spec.get("where", "any"), "leaf": None}
        out = resolve_targets(repo, path, source=source, is_folder=is_folder, **full)
        picks = [t for t in out["targets"] if t["smart"] and t["patterns"]] \
            or [t for t in out["targets"] if t["patterns"]][:1]
        if not picks or any(r["patterns"] == picks[0]["patterns"] for r in rows):
            continue   # not applicable, or the same rule as an earlier row
        rows.append({"label": label, "spec": full, "target": picks[0]["target"],
                     "patterns": picks[0]["patterns"], "more_targets": len(picks) - 1,
                     "already_excluded": all(s["already_excluded"] for s in out["sources"])})
    return {"repo": repo, "path": path, "variants": rows}
