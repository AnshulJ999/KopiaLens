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

from functools import lru_cache

from . import paths as pathrules
from .applog import get_logger
from .ignorematch import compile_pattern
from .policy import _GLOBAL_TARGET, effective_ignore_origin, global_ignores
from .sources import sources_index

log = get_logger("policytargets")

_SHAPES = ("exact", "name", "extension", "parent")


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


def _pattern_for_shape(shape: str, rel: str, is_folder: bool, path: str) -> str | None:
    """The pattern text as it lives in the TARGET's policy (anchored = relative to the target's
    root). Returns None when the shape does not apply to this selection."""
    if shape not in _SHAPES:
        raise TargetError(f"unknown shape {shape!r} (expected one of {_SHAPES})")
    if shape == "exact":
        if not rel:
            return "/**" if is_folder else None  # the target root itself: only a folder rule exists
        return "/" + rel + ("/**" if is_folder else "")
    if shape == "name":
        base = rel.rsplit("/", 1)[-1] if rel else ""
        if not base:
            return None
        # A folder name rule is directory-only ("Name/"): it matches at any depth and prunes the
        # subtree. "Name/**" would be rooted and miss nested folders.
        return base + "/" if is_folder else base
    if shape == "extension":
        if is_folder:
            return None
        base = rel.rsplit("/", 1)[-1] if rel else path.rsplit("/", 1)[-1]
        if "." not in base.lstrip("."):
            return None
        return "*." + base.rsplit(".", 1)[-1]
    if shape == "parent":
        if not rel or "/" not in rel:
            return None  # already at the target root: there is no level up
        return "/" + rel.rsplit("/", 1)[0] + "/**"
    return None  # pragma: no cover - guarded above


def _matches(pattern: str, rel: str, is_folder: bool) -> bool:
    """Does one rule, anchored at a source root, keep this selection out? A directory-only rule
    matches the folder itself; other folder rules are probed through a synthetic child."""
    pat = compile_pattern(pattern)
    if pat.dir_only:
        return is_folder and pat.match("/" + (rel or ""), is_dir=True)
    probe = "/" + rel if not is_folder else "/" + (rel or "") + "/__kl_probe__"
    return pat.match(probe, is_dir=False)


@lru_cache(maxsize=20000)
def _compiled(rule: str, base: str):
    return compile_pattern(rule, base_dir=base)


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
    """Is ``rel`` (relative to the source root) kept out of this source's snapshots? The root
    list, plus the rules of every path policy on a folder the walk passes through, evaluated in
    kopia's order (parent context first, last match wins). A folder equal to the source root counts
    as left out when its root rules leave out everything inside it."""
    if not rel and not is_folder:
        return False
    root = pathrules.source_root(source_key)
    sep = pathrules.sep(source_key)
    fold = not pathrules.case_sensitive(source_key)
    user_host = source_key.split(":", 1)[0]
    _origin, _owner, root_list = _root_rules(repo, source_key, defined)

    segs = [s for s in rel.strip("/").split("/") if s]
    nested: list[tuple[str, list]] = []   # (base "/a/b" in the path's own casing, compiled rules)
    for key, pol in defined.items():
        if ":" not in key or key.split(":", 1)[0] != user_host:
            continue
        rules = list(((pol or {}).get("files") or {}).get("ignore") or [])
        if not rules:
            continue
        ppath = key.split(":", 1)[1]
        if not _under(ppath, root, sep, fold, strict=True):
            continue
        prel = _rel(ppath, root, sep, fold)
        depth = prel.count("/") + 1
        # only policies on folders this path actually passes through
        if _norm("/".join(segs[:depth]), "/", fold) != _norm(prel, "/", fold) or \
                depth > len(segs) - (0 if is_folder else 1):
            continue
        base = "/" + "/".join(segs[:depth])
        nested.append((base, [_compiled(r, base) for r in rules]))
    nested.sort(key=lambda n: n[0].count("/"))
    root_c = [_compiled(r, "") for r in root_list]

    from ..index.policypricing import _machine  # lazy: index layer

    def rules_in(dir_path: str) -> list:
        out = list(root_c)
        for base, comp in nested:
            if dir_path == base or dir_path.startswith(base + "/"):
                out.extend(comp)
        return out

    for i in range(1, len(segs)):  # proper ancestors: a pruned folder prunes everything under it
        parent = "/" + "/".join(segs[:i - 1]) if i > 1 else ""
        if _machine(rules_in(parent), "/" + "/".join(segs[:i]), True)[0]:
            return True
    if not segs:  # the source root itself (a folder selection)
        return _machine(rules_in(""), "/__kl_probe__", False)[0]
    parent = "/" + "/".join(segs[:-1]) if len(segs) > 1 else ""
    full = "/" + "/".join(segs)
    if is_folder:
        if _machine(rules_in(parent), full, True)[0]:
            return True
        return _machine(rules_in(full), full + "/__kl_probe__", False)[0]
    return _machine(rules_in(parent), full, False)[0]


def _target_label_kind(key: str) -> str:
    if key == _GLOBAL_TARGET:
        return "global"
    if ":" not in key:
        return "userhost"   # user@host or @host
    return "path"


def _evaluate(repo: str, path: str, covering: list[str], source: str | None, is_folder: bool,
              shape: str, defined: dict) -> dict:
    """Every target for one shape, with before/after coverage per covering source."""
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
    cands.setdefault(_GLOBAL_TARGET, {"role": "global"})

    targets = []
    for key, meta in cands.items():
        kind = _target_label_kind(key)
        if kind == "path":
            troot = pathrules.source_root(key)
            rel_t = _rel(path, troot, pathrules.sep(key), not pathrules.case_sensitive(key))
            text_path = path.replace(pathrules.sep(key), "/")
        else:   # global and host-level rules anchor at each inheriting source's root
            rel_t = anchor["rel"]
            text_path = path.replace(pathrules.sep(anchor["source"]), "/")
        pattern = _pattern_for_shape(shape, rel_t, is_folder, text_path)
        own = global_ignores(repo, defined) if key == _GLOBAL_TARGET else _own_rules(defined, key)
        copied = list(meta.get("inherited_rules") or [])
        covers = []
        for i in info:
            after = i["before"]
            if pattern and not i["before"]:
                new_list = (copied if meta["role"] == "child" else own) + [pattern]
                after_defined = dict(defined)
                after_defined[key] = {**(defined.get(key) or {}),
                                      "files": {**((defined.get(key) or {}).get("files") or {}),
                                                "ignore": new_list}}
                after = _excluded_in(repo, i["source"], i["rel"], is_folder, after_defined)
            covers.append({"source": i["source"], "root": i["root"], "rel_path": i["rel"],
                           "matches": bool(pattern) and after, "already_excluded": i["before"]})
        protects = sum(1 for c in covers if c["matches"] and not c["already_excluded"])
        targets.append({
            "target": "global" if key == _GLOBAL_TARGET else key,
            "role": meta["role"],
            "is_global": key == _GLOBAL_TARGET,
            "existing_rule_count": len(own),
            "will_create_policy": meta["role"] in ("start", "child") or (kind == "path" and key not in defined),
            "inherits_from": meta.get("inherits_from"),
            "inherited_rules": copied,
            "uses_count": sum(1 for i in info if (_GLOBAL_TARGET if i["origin"] == "global" else i["owner"]) == key),
            "pattern": pattern,
            "pattern_anchored": bool(pattern) and pattern.startswith("/"),
            "covers": covers,
            "protects": protects,
            "smart": meta["role"] in ("owner", "start") and protects > 0,
            "impact": None,
        })
    return {"sources": info, "targets": targets}


def _coverage(ev: dict) -> tuple[int, int]:
    """(sources left out with the Smart picks applied, sources holding the path)."""
    total = len(ev["sources"])
    covered = 0
    for idx, i in enumerate(ev["sources"]):
        if i["before"] or any(t["smart"] and t["covers"][idx]["matches"] for t in ev["targets"]):
            covered += 1
    return covered, total


_ROLE_ORDER = {"owner": 0, "start": 0, "global": 1, "child": 2}


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


def resolve_targets(repo: str, path: str, source: str | None = None,
                    is_folder: bool = False, shape: str = "exact",
                    price: bool = False, suggest: bool = False) -> dict:
    """Targets for this path, Smart picks first. ``suggest`` also returns ``smart_shape``: the
    shape whose Smart picks keep the path out of the most sources (exact on a tie). ``price`` adds
    per-target impact for the matching covers (small scopes only)."""
    from ..index import policypricing  # local import: keeps the index layer out of import time
    from .policy import load_defined_policies

    if not path or not path.strip():
        raise TargetError("path is required")
    if shape not in _SHAPES:
        raise TargetError(f"unknown shape {shape!r} (expected one of {_SHAPES})")
    defined = load_defined_policies(repo).get("policies") or {}
    host = source.split(":", 1)[0] if source else None
    covering = _covering_sources(repo, path, host)
    if not covering:
        return {"repo": repo, "path": path, "shape": shape, "sources": [], "targets": [],
                "note": "no configured source contains this path", "smart_shape": None}

    ev = _evaluate(repo, path, covering, source, is_folder, shape, defined)

    smart_shape = None
    if suggest:
        best = (_coverage(ev)[0], shape)
        for alt in ("exact", "name"):
            if alt == shape:
                continue
            alt_ev = _evaluate(repo, path, covering, source, is_folder, alt, defined)
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
    return {"repo": repo, "path": path, "shape": shape, "sources": sources,
            "targets": ev["targets"], "smart_shape": smart_shape}
