"""Exclusion-target resolution for the exclusion dialog (, Option A).

Given a path, answer: WHICH policies can receive this exclusion rule? The naive answer - "the
sources that contain the file" - is wrong twice over:

1. A source whose ignore rules are INHERITED is never a write target. Kopia does not stack
   exclusion lists: the moment a policy defines any rule of its own, the inherited list is
   replaced entirely (kopia ``policy_merge.go mergeStringList``). Writing to the inheriting
   source would silently switch off everything it inherits. The rule belongs at the policy that
   OWNS the list - resolved by ``policy.effective_ignore_origin``.

2. A write to an owning policy does not automatically "cover" every source that inherits from
   it. Inherited rules RE-ANCHOR to each source's walk root (kopia puts the effective policy at
   the root with no base dir), so an ANCHORED pattern written at ``G:\\GitHub`` matches the file
   in source ``G:\\GitHub`` but NOT in source ``G:\\GitHub\\Example-Repo`` (where the same text
   would mean ``...\\Example-Repo\\Example-Repo\\...``). Unanchored shapes (a bare name, an
   extension) match at any depth and cover every inheriting source. Covers are therefore computed
   by MATCHING the pattern against each covered source, never by inheritance alone.

Option A: non-owning sources are not offered as targets at all. A source
with NO rules anywhere (origin "none") is the one exception - creating its list shadows nothing,
so the source itself is offered with ``will_create_policy`` set.
"""

from __future__ import annotations

from . import paths as pathrules
from .applog import get_logger
from .ignorematch import compile_pattern
from .policy import _GLOBAL_TARGET, effective_ignore_origin
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


def _covering_sources(repo: str, path: str, host_filter: str | None) -> list[str]:
    """Sources whose root contains ``path`` (same user@host when given, per-source separator and
    case rules - never the host's)."""
    out = []
    for key in _source_roots(repo, host_filter):
        root = pathrules.source_root(key)
        sep = pathrules.sep(key)
        fold = not pathrules.case_sensitive(key)
        npath, nroot = _norm(path, sep, fold), _norm(root, sep, fold)
        if npath == nroot or npath.startswith(nroot + "/"):
            out.append(key)
    return out


def _rel(path: str, root: str, sep: str, fold: bool) -> str:
    """Path relative to ``root``, '/'-separated ('' when equal). Case is PRESERVED in the result
    - the pattern text must match kopia's case-sensitive matcher against real paths - while the
    prefix match honours the source's case rules. _norm keeps length, so positions align."""
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
        return base + ("/**" if is_folder else "")
    if shape == "extension":
        if is_folder:
            return None
        base = rel.rsplit("/", 1)[-1] if rel else path.rsplit("/", 1)[-1]
        if "." not in base.lstrip("."):
            return None  # no extension to key on
        return "*." + base.rsplit(".", 1)[-1]
    if shape == "parent":
        if not rel or "/" not in rel:
            return None  # already at the target root: there is no level up
        return "/" + rel.rsplit("/", 1)[0] + "/**"
    return None  # pragma: no cover - guarded above


def _matches(pattern: str, rel: str, is_folder: bool) -> bool:
    """Does the rule (anchored at the COVERED source's root) keep this selection out there?
    Folders are probed with a synthetic child: ``x/**`` never matches ``x`` itself but prunes
    everything under it."""
    pat = compile_pattern(pattern)
    probe = "/" + rel if not is_folder else "/" + (rel or "") + "/__kl_probe__"
    return pat.match(probe, is_dir=False)


def resolve_targets(repo: str, path: str, source: str | None = None,
                    is_folder: bool = False, shape: str = "exact",
                    price: bool = False) -> dict:
    """Owning policies for this path, deduped, with the sources each write covers, ranked
    biggest-reach first. See the module docstring for the two reasons this is not "the sources
    that contain the file".

    ``price=False`` (default) keeps this a pure resolution call - zero index scans; the dialog
    ranks by matched-covers count, which the inheritance/matcher walk yields for free. Impact
    pricing is opt-in (small scopes only - the global target prices one pattern per covering
    source)."""
    from ..index import policypricing  # local import: keeps the index layer out of import time
    from .policy import global_ignores, load_defined_policies

    if not path or not path.strip():
        raise TargetError("path is required")
    defined = load_defined_policies(repo).get("policies") or {}
    host = source.split(":", 1)[0] if source else None
    covering = _covering_sources(repo, path, host)
    if not covering:
        return {"repo": repo, "path": path, "shape": shape, "sources": [], "targets": [],
                "note": "no configured source contains this path", "ranked_by": "impact"}

    # Resolve every covering source to the policy that OWNS its ignore rules.
    owners: dict[str, list[str]] = {}   # owner target -> covering sources it speaks for
    none_sources: list[str] = []
    source_info: list[dict] = []
    for key in covering:
        origin, owner = effective_ignore_origin(repo, key, defined)
        source_info.append({"source": key, "root": pathrules.source_root(key),
                            "origin": origin, "owner": owner})
        if origin == "none":
            none_sources.append(key)      # nothing to shadow: the source itself is the target
        else:
            owners.setdefault(owner, []).append(key)

    # A "none" source owns nothing yet - its own (to-be-created) list is its target.
    for key in none_sources:
        owners.setdefault(key, []).append(key)

    targets: list[dict] = []
    for owner, keys in owners.items():
        is_global = owner == _GLOBAL_TARGET
        target_root = None if is_global else pathrules.source_root(owner)
        fold = False if is_global else not pathrules.case_sensitive(owner)
        sep = "\\" if is_global else pathrules.sep(owner)
        if is_global:
            # A global anchored rule re-anchors to EVERY covered source's root, so the pattern
            # text is framed relative to one covered source - the one the dialog opened from
            # when it is among the covers, else the first. It then matches wherever that same
            # relative path exists under any global-inheriting source.
            anchor = source if source in keys else sorted(keys)[0]
            rel_t = _rel(path, pathrules.source_root(anchor), pathrules.sep(anchor),
                         not pathrules.case_sensitive(anchor))
        else:
            rel_t = _rel(path, target_root, sep, fold)
        pattern = _pattern_for_shape(shape, rel_t, is_folder, path.replace(sep, "/"))
        if is_global:
            existing = len(global_ignores(repo, defined))
        else:
            dp = defined.get(owner) or {}
            existing = len((dp.get("files") or {}).get("ignore") or [])

        covers = []
        for key in sorted(keys):
            sroot = pathrules.source_root(key)
            ssep = pathrules.sep(key)
            sfold = not pathrules.case_sensitive(key)
            rel_s = _rel(path, sroot, ssep, sfold)
            matches = bool(pattern) and _matches(pattern, rel_s, is_folder)
            covers.append({"source": key, "root": sroot, "rel_path": rel_s.replace(ssep, "/"),
                           "matches": matches})

        impact = None
        if price and pattern:
            by_source = []
            files_total = bytes_total = 0
            for cover in covers:
                if not cover["matches"]:
                    continue
                out = policypricing.price_patterns(repo, cover["source"], [pattern], limit=0)
                res = out["results"][0]
                if res["error"]:
                    continue
                by_source.append({"source": cover["source"], "files": res["files"],
                                  "bytes": res["bytes"]})
                files_total += res["files"]
                bytes_total += res["bytes"]
            impact = {"files": files_total, "bytes": bytes_total, "by_source": by_source}

        note = None
        if owner in none_sources:
            note = ("this source has no ignore rules of its own and inherits none - saving here "
                    "creates its first rule list, which shadows nothing")
        elif is_global:
            note = (f"the global policy owns the rules of {len(keys)} covering source(s); "
                    "sources with their own lists are NOT affected by this write")

        targets.append({
            "target": "global" if is_global else owner,
            "is_global": is_global,
            "target_root": target_root,
            "existing_rule_count": existing,
            "will_create_policy": owner in none_sources,
            "pattern": pattern,
            "pattern_anchored": bool(pattern) and pattern.startswith("/"),
            "covers": covers,
            "impact": impact,
            "note": note,
        })

    # Ranked by REACH: matched covers first (a pattern that genuinely keeps the file out of more
    # sources), then total covers, then the target key. When impact pricing is on, bytes break
    # ties within the same reach.
    def _rank(t: dict) -> tuple:
        covers = t["covers"]
        matched = sum(1 for c in covers if c["matches"])
        byts = t["impact"]["bytes"] if t["impact"] else 0
        return (-matched, -len(covers), -byts, t["target"])

    targets.sort(key=_rank)
    return {"repo": repo, "path": path, "shape": shape, "sources": source_info,
            "targets": targets, "ranked_by": "sources-covered"}
