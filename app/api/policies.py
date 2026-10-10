"""Policies tab API ( +).

Read half: overview (global + sources with inheritance state), detail, "keeping out" pricing
against the index. Write half: staged diff -> fail-closed pre-write backup -> `kopia policy
set/delete` -> cache invalidation; backups list/read/manual/restore. 
"""

from __future__ import annotations

import copy
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import is_demo, kopia_repos, repo_kind

from ..core import policybackup, policyresolve, policytargets, rulelayers
from ..core.kopia import (
    _quote_arg,
    policy_delete_command,
    policy_delete_execute,
    policy_import_command,
    policy_set_command,
    policy_write_rules_execute,
)
from ..core.policy import (
    _GLOBAL_TARGET,
    effective_ignore_origin,
    global_ignores,
    invalidate_after_write,
    load_defined_policies,
    load_policies,
    refresh_defined_policies,
    show_policy,
    summarize,
)
from ..core.policybackup import BackupError
from ..core.policytargets import TargetError
from ..index import policypricing

router = APIRouter(tags=["policies"])

_BACKUP_OUTPUT_TAIL = 2000
_EXAMPLES_MAX = 5000  # endpoint-level example cap (the pricing cache itself keeps fewer)


# ---- helpers ---------------------------------------------------------------------------

def _kopia_repo(repo: str) -> str:
    if repo not in kopia_repos():
        if repo_kind(repo) == "backblaze":
            raise HTTPException(status_code=400, detail="the Backblaze repo has no kopia policies")
        raise HTTPException(status_code=400, detail=f"unknown repo: {repo}")
    return repo


def _is_global(target: str) -> bool:
    return target == "global"


def _cmd_text(cmd: list[str]) -> str:
    return " ".join(_quote_arg(a) for a in cmd)


def _source_keys(repo: str) -> list[str]:
    pols = load_policies(repo).get("policies") or {}
    defined = load_defined_policies(repo).get("policies") or {}
    keys = set(pols) | {t for t in defined if t != _GLOBAL_TARGET}
    return sorted(keys)


def _own_rules(defined_policies: dict, target: str) -> list[str]:
    pol = defined_policies.get(target) or {}
    return list((pol.get("files") or {}).get("ignore") or [])


def _pricing_scope(repo: str, target: str) -> list[dict]:
    """Every backup a write to ``target`` can affect, each with the anchor its rules price at.

    Root layer: the target itself plus sources whose root list comes from it ( - a rule at
    an ancestor policy re-anchors to each inheritor's walk root). Folder layer (): every
    source whose walk passes through the target's folder applies its rules there anchored at that
    folder - pricing that ignores this undercounts what the policy keeps out."""
    # The API says "global"; the policy layer keys it as kopia does, "(global)".
    return rulelayers.scope_for_target(repo, _GLOBAL_TARGET if _is_global(target) else target)


def _price_for_target(repo: str, target: str, patterns: list[str], limit: int = 10) -> dict:
    """Price patterns over every backup the target's rules reach, each at its own anchor. The
    rollup is single-flighted: concurrent identical requests block on one lock and then hit the
    price cache the leader populated."""
    scope = _pricing_scope(repo, target)
    if len(scope) > 1:
        with policypricing.rollup_lock((repo, tuple(sorted(patterns)))):
            out = policypricing.price_scope(repo, scope, patterns, limit=limit)
    else:
        out = policypricing.price_scope(repo, scope, patterns, limit=limit)
    out["target"] = target
    out.setdefault("scope", {})["inheriting_global"] = (
        len(scope) if target == _GLOBAL_TARGET else 0)
    return out


# ---- models ----------------------------------------------------------------------------

class PriceBody(BaseModel):
    repo: str
    target: str
    patterns: list[str] = Field(default_factory=list)
    limit: int = 10


class Changes(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    delete_policy: bool = False
    fields: dict = Field(default_factory=dict)   # field JSON path -> value (None = unset)


class DiffBody(BaseModel):
    repo: str
    target: str
    changes: Changes
    price: bool = True            # false: rule lists only (the exclusion panels' diff view)


class SaveBody(DiffBody):
    confirmed: bool = False


class BackupBody(BaseModel):
    repo: str


class RestoreBody(BaseModel):
    repo: str
    name: str
    mode: str = "merge"
    confirmed: bool = False


class TargetsBody(BaseModel):
    repo: str
    path: str
    source: str | None = None     # the panel the dialog opened from: disambiguates user@host
    is_folder: bool = False
    shape: str = "exact"          # exact | name | extension | similar | parent
    level: int = 0                # folders up from the path (exact) or the "under" folder
    where: str = "any"            # any | here | under - scope of a name-type rule
    leaf: str | None = None       # similar-names pattern; None = the suggested one
    price: bool = False           # opt-in impact pricing; the dialog ranks by covers count
    refresh: bool = False         # re-export policies first (the dialog's first load)
    suggest: bool = False         # also return smart_shape


class VariantsBody(BaseModel):
    repo: str
    path: str
    source: str | None = None
    is_folder: bool = False


# ---- read half -------------------------------------------------------------------------

@router.get("/policies/overview")
def overview(repo: str) -> dict:
    """Left-rail data: the global policy + every source with its inheritance state."""
    _kopia_repo(repo)
    defined, fresh = refresh_defined_policies(repo)
    defined_policies = defined.get("policies") or {}
    effective = load_policies(repo).get("policies") or {}

    gpol = defined_policies.get(_GLOBAL_TARGET) or {}
    gsum = summarize(gpol) if gpol else {"schedule": "inherited", "retention": "inherited",
                                         "ignore": [], "compression": "none"}
    global_entry = {
        "target": "global",
        "defined": bool(gpol),
        "rule_count": len(gsum["ignore"]),
        "no_parent_ignore": bool((gpol.get("files") or {}).get("noParentIgnore")),
        "summarized": gsum,
    }
    sources = []
    for key in _source_keys(repo):
        dp = defined_policies.get(key) or {}
        ep = effective.get(key) or {}
        own = _own_rules(defined_policies, key)
        origin, owner = effective_ignore_origin(repo, key, defined_policies)
        eff_pol = ep or dp
        sources.append({
            "source": key,
            "root": key.split(":", 1)[1] if ":" in key else key,
            "defined": bool(dp),
            "has_own_rules": bool(own),
            "rule_count": len(own),
            "effective_origin": origin,
            "effective_origin_target": owner,
            "effective_rule_count": len((eff_pol.get("files") or {}).get("ignore") or []),
            "global_shadowed": origin == "own",
            "no_parent_ignore": bool((dp.get("files") or {}).get("noParentIgnore")
                                     or (ep.get("files") or {}).get("noParentIgnore")),
            "no_parent_dot_files": bool((dp.get("files") or {}).get("noParentDotFiles")
                                        or (ep.get("files") or {}).get("noParentDotFiles")),
            "summarized": summarize(eff_pol) if eff_pol else None,
        })
    source_keys = {s["source"] for s in sources}
    others = []
    for key in sorted(defined_policies):
        if key == _GLOBAL_TARGET or key in source_keys:
            continue
        _user, _host, path = policyresolve.parse_key(key)
        others.append({"target": key, "kind": "folder" if path else "computer", "root": path or key,
                       "rule_count": len(_own_rules(defined_policies, key))})
    return {"repo": repo, "stale": not fresh, "global": global_entry, "sources": sources,
            "others": others}


# ---- editor () ---------------------------------------------------------------------------

_CLIENT_CACHE: dict[str, tuple[float, dict]] = {}


def client_options(repo: str) -> dict:
    """This computer's kopia client identity for the repo (user, host, actions), cached 10 min."""
    import json as _json
    import time as _time
    hit = _CLIENT_CACHE.get(repo)
    if hit and _time.time() - hit[0] < 600:
        return hit[1]
    from ..core.kopia import kopia
    opts: dict = {}
    try:
        res = kopia(repo, ["repository", "status", "--json"])
        opts = (_json.loads(res.stdout or "{}").get("clientOptions") or {}) if res.returncode == 0 else {}
    except Exception:  # noqa: BLE001 - an unreachable repo just has no client facts
        opts = {}
    out = {"user": opts.get("username") or "", "host": opts.get("hostname") or "",
           "enable_actions": bool(opts.get("enableActions"))}
    _CLIENT_CACHE[repo] = (_time.time(), out)
    return out


def _target_key(target: str) -> str:
    return _GLOBAL_TARGET if _is_global(target) else target


def _defined(repo: str, force: bool = False) -> dict:
    return refresh_defined_policies(repo, force=force)[0].get("policies") or {}


def _last_snapshot(repo: str, key: str):
    import datetime as _dt
    from ..core.sources import sources_index
    snaps = sources_index(repo).get(key) or []
    if not snaps:
        return None
    try:
        return _dt.datetime.fromisoformat(snaps[-1]["startTime"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def _retention_counts(eff: dict) -> dict[str, int] | None:
    ret = eff.get("retention") or {}
    vals = {k: int(ret.get(f) or 0) for k, f in (("latest", "keepLatest"), ("hourly", "keepHourly"),
            ("daily", "keepDaily"), ("weekly", "keepWeekly"), ("monthly", "keepMonthly"), ("annual", "keepAnnual"))}
    return None if not any(vals.values()) else vals   # all zero: kopia keeps everything


def _retention_impact(repo: str, defined_before: dict, defined_after: dict, key: str) -> list[dict]:
    """Snapshots kept per affected source, before and after (KopiaLens' retention replica)."""
    from ..core.retention import retention_keep
    from ..core.sources import sources_index
    out = []
    for src, snaps in sorted(sources_index(repo).items()):
        if key not in {k for k, _ in policyresolve.hierarchy(defined_after, src)} and \
                key not in {k for k, _ in policyresolve.hierarchy(defined_before, src)}:
            continue
        before = _retention_counts(policyresolve.resolve(defined_before, src)[0])
        after = _retention_counts(policyresolve.resolve(defined_after, src)[0])
        if before == after:
            continue
        total = len(snaps)
        kb = total if before is None else len(retention_keep(snaps, before)[0])
        ka = total if after is None else len(retention_keep(snaps, after)[0])
        if kb == ka:
            continue
        out.append({"source": src, "total": total, "kept_before": kb, "kept_after": ka})
        if len(out) >= 200:
            break
    return out


def _draft_policy(defined: dict, key: str, changes: Changes) -> dict:
    pol = policyresolve.apply_fields(defined.get(key) or {}, changes.fields)
    current = list(((defined.get(key) or {}).get("files") or {}).get("ignore") or [])
    rules = [p for p in current if p not in set(changes.remove)]
    for p in changes.add:
        if p not in rules:
            rules.append(p)
    files = dict(pol.get("files") or {})
    if rules:
        files["ignore"] = rules
    else:
        files.pop("ignore", None)
    pol["files"] = files
    return pol


class ResolveBody(BaseModel):
    repo: str
    target: str
    changes: Changes = Field(default_factory=Changes)
    impact: bool = False


@router.get("/policies/meta")
def meta(repo: str) -> dict:
    """Field catalogue, option lists, and this computer's kopia client facts."""
    _kopia_repo(repo)
    return {"fields": policyresolve.FIELDS, "compressors": policyresolve.COMPRESSORS,
            "splitters": policyresolve.SPLITTERS, "client": client_options(repo)}


@router.post("/policies/resolve")
def resolve_draft(body: ResolveBody) -> dict:
    """Effective values and where each comes from, before and after the staged changes."""
    _kopia_repo(body.repo)
    key = _target_key(body.target)
    defined = _defined(body.repo)
    try:
        draft = _draft_policy(defined, key, body.changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    after_defined = dict(defined)
    after_defined[key] = draft
    eff_b, def_b = policyresolve.resolve(defined, key)
    eff_a, def_a = policyresolve.resolve(after_defined, key)
    last = _last_snapshot(body.repo, key)
    chain = [k for k, _ in policyresolve.hierarchy(defined, key)]
    from ..core import schedule as _sched
    import datetime as _dt
    now = _dt.datetime.now()
    cron = []
    for line in (draft.get("scheduling") or {}).get("cron") or []:
        errs = [e for e in policyresolve.validate(key, {"scheduling": {"cron": [line]}}) if "Cron" in e]
        nxt = []
        if not errs:
            t = now
            for _ in range(3):
                t = _sched.cron_next(line, t)
                if t is None:
                    break
                nxt.append(t.isoformat())
        cron.append({"line": line, "error": errs[0] if errs else "", "meaning": _sched.describe_cron(line),
                     "next": nxt})
    out = {
        "target": key, "exists": key in defined, "chain": chain,
        "global_defined": defined.get(_GLOBAL_TARGET) or {}, "cron": cron,
        "defined": defined.get(key) or {}, "draft": draft,
        "effective": eff_a, "definition": def_a,
        "effective_before": eff_b, "definition_before": def_b,
        "errors": policyresolve.validate(key, draft),
        "next_snapshots": policyresolve.next_snapshots(eff_a, last, count=5),
        "next_snapshots_before": policyresolve.next_snapshots(eff_b, last, count=5),
        "inheritors": [s for s in _source_keys(body.repo)
                       if s != key and key in {k for k, _ in policyresolve.hierarchy(defined, s)}],
    }
    if body.impact and any(p.startswith("retention.") for p in body.changes.fields):
        out["retention_impact"] = _retention_impact(body.repo, defined, after_defined, key)
    return out


class MultiBody(BaseModel):
    repo: str
    targets: list[str]
    fields: dict = Field(default_factory=dict)


@router.post("/policies/multi-plan")
def multi_plan(body: MultiBody) -> dict:
    """Per field and target: today's value, whether it is inherited, and the shared parent."""
    _kopia_repo(body.repo)
    defined = _defined(body.repo)
    plans = []
    resolved = {t: policyresolve.resolve(defined, _target_key(t)) for t in body.targets}
    for path in body.fields:
        if path not in policyresolve.FIELD_PATHS or path.startswith("files.ignore"):
            raise HTTPException(status_code=400, detail=f"field cannot be edited here: {path}")
        spec = next(f for f in policyresolve.FIELDS if f["path"] == path)
        rows = []
        for t in body.targets:
            key = _target_key(t)
            eff, df = resolved[t]
            own = policyresolve.get(defined.get(key) or {}, path)
            src = df.get(path, _GLOBAL_TARGET)
            inherits = own is None or (spec["kind"] == "plain" and not own)
            rows.append({"target": key, "own": own, "effective": policyresolve.get(eff, path),
                         "from": src, "inherits": inherits, "creates_policy": key not in defined,
                         "no_effect": spec["kind"] == "plain" and body.fields[path] in (None, False, 0, "")
                                      and inherits and bool(policyresolve.get(eff, path))})
        parents = {r["from"] for r in rows if r["inherits"]}
        common = next(iter(parents)) if len(parents) == 1 and all(r["inherits"] for r in rows) else None
        plans.append({"field": path, "value": body.fields[path], "rows": rows, "common_parent": common})
    return {"plans": plans}


class PreviewBody(BaseModel):
    repo: str
    target: str
    pattern: str
    source: str | None = None
    mode: str = "index"            # index = what the index would drop | disk = what is on disk now
    rules: list[str] | None = None   # the rule list the pattern sits in (disk mode ordering)
    limit: int = 500


@router.post("/policies/preview")
def preview(body: PreviewBody) -> dict:
    """What one rule keeps out, listed: from the index (a new rule) or from disk (an active rule,
    whose matches were never indexed)."""
    from ..core import disklookup
    _kopia_repo(body.repo)
    from ..core.sources import sources_index
    live = set(sources_index(body.repo))
    entries = [s for s in _pricing_scope(body.repo, body.target) if s["source"] in live]
    scope = [s["source"] for s in entries]
    source = body.source if body.source in scope else (scope[0] if scope else None)
    # A backup that only walks through the policy's folder applies its rules anchored there.
    anchor = next((s.get("anchor") for s in entries if s["source"] == source), None)
    out = {"target": body.target, "pattern": body.pattern, "mode": body.mode, "scope": scope,
           "source": source, "items": [], "total": 0, "bytes": 0, "capped": False, "note": ""}
    if not source:
        out["note"] = "No backup uses this policy's rules."
        return out
    limit = min(max(body.limit, 1), 2000)
    if body.mode == "index":
        res = policypricing.price_patterns(body.repo, source, [body.pattern], limit=min(limit, 500),
                                           anchor=anchor)
        r = res["results"][0]
        out.update(items=[{"path": e["path"], "size": e["size"], "is_dir": False} for e in r["examples"]],
                   total=r.get("files", 0), bytes=r.get("bytes", 0), dirs=r.get("dirs", 0),
                   capped=r.get("capped", False), error=r.get("error"))
        return out
    if anchor is not None:
        out["note"] = "Disk preview runs from the backups that use this policy's rules directly."
        return out
    if is_demo(body.repo):
        out["note"] = "Sample data isn't on this computer's disk."
        return out
    user, host, _path = policyresolve.parse_key(source)
    client = client_options(body.repo)
    if client["host"] and (host.lower() != client["host"].lower() or user.lower() != client["user"].lower()):
        out["note"] = f"This backup belongs to {user}@{host}. Its disk is not on this computer."
        return out
    rules = body.rules
    if rules is None:
        defined = _defined(body.repo)
        rules = policytargets._root_rules(body.repo, source, defined)[2]
    if body.pattern not in rules:
        rules = list(rules) + [body.pattern]
    found = disklookup.excluded_by_rule(source, rules, rules.index(body.pattern), limit)
    out.update(found)
    return out


@router.get("/policies/check")
def check_status(repo: str) -> dict:
    """The last comparison of KopiaLens' inheritance copy with kopia's own output."""
    _kopia_repo(repo)
    from ..index import policycheck
    return policycheck.load(repo) or {"repo": repo, "ts": None}


@router.get("/policies/detail")
def detail(repo: str, target: str = "") -> dict:
    """Full policy for ONE target (defined + effective + the rule block the tab renders)."""
    _kopia_repo(repo)
    if not target:
        raise HTTPException(status_code=400, detail="target is required ('global' or a source key)")
    defined, fresh = refresh_defined_policies(repo)
    defined_policies = defined.get("policies") or {}

    if _is_global(target):
        dp = defined_policies.get(_GLOBAL_TARGET) or {}
        ep = dp  # the global policy has no parent to inherit from
        own = list((dp.get("files") or {}).get("ignore") or [])
        rules = {"own": own, "inherited": [], "inherited_origin": None, "inherited_active": False}
        target_label = "global"
        is_global = True
    else:
        dp = defined_policies.get(target) or {}
        ep = (load_policies(repo).get("policies") or {}).get(target) or {}
        if not ep and not dp:
            try:
                ep = show_policy(repo, target)  # one subprocess; the nightly cache stays authoritative
            except Exception:  # noqa: BLE001 - an unreachable repo still renders the defined view
                ep = {}
        own = _own_rules(defined_policies, target)
        origin, owner = effective_ignore_origin(repo, target, defined_policies)
        if origin == "own":
            rules = {"own": own, "inherited": [], "inherited_origin": None, "inherited_active": False}
        elif origin == "global":
            rules = {"own": own, "inherited": global_ignores(repo, defined_policies),
                     "inherited_origin": "global", "inherited_active": True}
        elif origin == "inherited":
            rules = {"own": own, "inherited": _own_rules(defined_policies, owner or ""),
                     "inherited_origin": owner, "inherited_active": True}
        else:
            rules = {"own": own, "inherited": [], "inherited_origin": None, "inherited_active": False}
        target_label = target
        is_global = False

    eff_pol = ep or dp
    return {
        "repo": repo, "target": target_label, "is_global": is_global,
        "defined": dp, "effective": ep, "rules": rules,
        "summarized": summarize(eff_pol) if eff_pol else None,
        "stale": not fresh,
    }


@router.post("/policies/price")
def price(body: PriceBody) -> dict:
    """"Keeping out" pricing for each pattern - LIVE, for small rule sets only (the dialog's
    rule-about-to-be-added case). Large rule sets read the nightly-stored numbers instead:
    GET /policies/stored-prices."""
    _kopia_repo(body.repo)
    if not body.target:
        raise HTTPException(status_code=400, detail="target is required")
    if not body.patterns:
        return {"target": body.target,
                "scope": {"sources": [], "layers": {"root": [], "folder": []},
                          "inheriting_global": 0},
                "results": [], "breakdown": []}
    if len(body.patterns) > policypricing._LIVE_PRICE_CAP:
        raise HTTPException(
            status_code=400,
            detail=f"live pricing is capped at {policypricing._LIVE_PRICE_CAP} patterns per "
                   f"request ({len(body.patterns)} given) - read GET /api/policies/stored-prices"
                   "?repo=" + body.repo + "&target=... for whole-policy numbers")
    try:
        return _price_for_target(body.repo, body.target, body.patterns,
                                 limit=min(max(body.limit, 0), _EXAMPLES_MAX))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/policies/stored-prices")
def stored_prices(repo: str, target: str = "") -> dict:
    """Nightly-stored "keeping out" numbers for one target (whole policies - the Global
    rollup's 250 rules cost nothing to read here). Shape matches POST /policies/price's
    results so the UI renders both with one path; `stored: false` = not priced yet (first
    nightly has not run)."""
    _kopia_repo(repo)
    if not target:
        raise HTTPException(status_code=400, detail="target is required ('global' or a source key)")
    out = policypricing.stored_for_target(repo, "global" if target == "global" else target)
    if out is None:
        return {"repo": repo, "target": target, "stored": False, "ts": None, "results": [],
                "detail": "no nightly pricing stored yet - available after the next index run"}
    out["repo"] = repo
    return out


@router.get("/policies/ignore-files")
def ignore_files(repo: str, target: str = "") -> dict:
    """Dot-ignore files found under a target (whole repo when omitted) with the rules each holds
    and what they keep out (). Read from the nightly artifact - no kopia calls."""
    _kopia_repo(repo)
    out = policypricing.ignore_files_for(repo, target or None)
    out["repo"] = repo
    return out


@router.get("/policies/path-status")
def path_status(repo: str, source: str = "", path: str = "", is_folder: bool = False) -> dict:
    """The rule list a source uses for one path, and whether a rule already leaves it out."""
    _kopia_repo(repo)
    if not source or not path:
        raise HTTPException(status_code=400, detail="source and path are required")
    try:
        return policytargets.path_status(repo, source, path, is_folder=is_folder)
    except TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/policies/exclusion-targets")
def exclusion_targets(body: TargetsBody) -> dict:
    """Policies that can receive an exclusion for ``path``, Smart picks first, each with its
    before/after coverage per source. ``price: false`` (default) keeps this a pure resolution
    call - zero index scans; impact pricing is opt-in and small-scope."""
    _kopia_repo(body.repo)
    if body.refresh:
        # Policies changed outside KopiaLens are invisible to a cached export.
        refresh_defined_policies(body.repo, force=True)
    try:
        return policytargets.resolve_targets(
            body.repo, body.path, source=body.source,
            is_folder=body.is_folder, shape=body.shape, price=body.price, suggest=body.suggest,
            level=body.level, where=body.where, leaf=body.leaf)
    except TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/policies/exclusion-variants")
def exclusion_variants(body: VariantsBody) -> dict:
    """Ready-made exclusion rules for the file/folder panel, each written for the policy Smart
    picks for it. Reads the cached policy export; no kopia call."""
    _kopia_repo(body.repo)
    try:
        return policytargets.variants(body.repo, body.path, source=body.source,
                                      is_folder=body.is_folder)
    except TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- write half ------------------------------------------------------------------------

def _validate_changes(repo: str, target: str, changes: Changes,
                      force: bool = False) -> tuple[list[str], list[str], dict]:
    """Shared diff/save validation. Returns (current_rules, resulting_rules, defined_policies).
    Resulting order: current rules with removals taken out in place, additions appended."""
    defined, _fresh = refresh_defined_policies(repo, force=force)
    defined_policies = defined.get("policies") or {}
    current = _own_rules(defined_policies, _GLOBAL_TARGET if _is_global(target) else target)
    add = list(dict.fromkeys(changes.add))
    remove = list(dict.fromkeys(changes.remove))

    if changes.delete_policy:
        if add or remove or changes.fields:
            raise HTTPException(status_code=400,
                                detail="delete_policy cannot be combined with other changes")
        if not current and not defined_policies.get(_GLOBAL_TARGET if _is_global(target) else target):
            raise HTTPException(status_code=400, detail="target has no defined policy to delete")
        return current, [], defined_policies

    overlap = sorted(set(add) & set(remove))
    if overlap:
        raise HTTPException(status_code=400, detail=f"patterns both added and removed: {overlap}")
    missing = [p for p in remove if p not in current]
    if missing:
        if _is_global(target):
            raise HTTPException(status_code=400,
                                detail=f"patterns not defined by the global policy: {missing}")
        raise HTTPException(
            status_code=400,
            detail=f"patterns not defined by this target's own policy: {missing}. "
                   "Rules inherited from the global policy are removed there (or by defining "
                   "own rules first), not on the source.")
    resulting = [p for p in current if p not in set(remove)]
    for p in add:
        if p not in resulting:
            resulting.append(p)
    return current, resulting, defined_policies


def _build_diff(repo: str, target: str, changes: Changes, force: bool = False,
                price: bool = True) -> dict:
    current, resulting, defined_policies = _validate_changes(repo, target, changes, force=force)
    add = list(dict.fromkeys(changes.add))
    remove = list(dict.fromkeys(changes.remove))
    priced = []
    if price and not changes.delete_policy and (add or remove):
        priced = _price_for_target(repo, target, add + remove, limit=_EXAMPLES_MAX)["results"]
    by_pattern = {r["pattern"]: r for r in priced}
    impact = {
        "added": [by_pattern.get(p) for p in add],
        "removed": [by_pattern.get(p) for p in remove],
    }
    target_key = _GLOBAL_TARGET if _is_global(target) else target
    will_create = (not changes.delete_policy and (add or remove or changes.fields)
                   and target_key not in defined_policies)
    field_changes, errors, retention = [], [], []
    if changes.fields and not changes.delete_policy:
        try:
            draft = _draft_policy(defined_policies, target_key, changes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        after = dict(defined_policies)
        after[target_key] = draft
        eff_b, def_b = policyresolve.resolve(defined_policies, target_key)
        eff_a, def_a = policyresolve.resolve(after, target_key)
        before_pol = defined_policies.get(target_key) or {}
        for path in changes.fields:
            field_changes.append({
                "field": path,
                "before": policyresolve.get(before_pol, path), "after": policyresolve.get(draft, path),
                "effective_before": policyresolve.get(eff_b, path), "from_before": def_b.get(path, _GLOBAL_TARGET),
                "effective_after": policyresolve.get(eff_a, path), "from_after": def_a.get(path, _GLOBAL_TARGET),
            })
        errors = policyresolve.validate(target_key, draft)
        if price and any(p.startswith("retention.") for p in changes.fields):
            retention = _retention_impact(repo, defined_policies, after, target_key)
    if changes.delete_policy:
        command = policy_delete_command(repo, None if _is_global(target) else target,
                                        global_=_is_global(target))
    elif changes.fields:
        command = None  # a whole-policy import from a temporary file; nothing pasteable
    else:
        command = policy_set_command(repo, None if _is_global(target) else target,
                                     global_=_is_global(target),
                                     add_ignore=add or None, remove_ignore=remove or None)
    return {
        "repo": repo, "target": target,
        "current_rules": current, "resulting_rules": resulting,
        "added": add, "removed": remove,
        "field_changes": field_changes, "errors": errors, "retention_impact": retention,
        "impact": impact, "will_create_policy": will_create,
        "command": _cmd_text(command) if command else "",
        "backup_dir": policybackup.backup_dir(repo),
    }


@router.post("/policies/diff")
def diff(body: DiffBody) -> dict:
    """Review data for staged edits - nothing is written."""
    _kopia_repo(body.repo)
    if not body.target:
        raise HTTPException(status_code=400, detail="target is required")
    return _build_diff(body.repo, body.target, body.changes, price=body.price)


@router.post("/policies/save")
def save(body: SaveBody) -> dict:
    """Preview -> confirm dialog -> HERE: fail-closed backup, then the write, then cache
    invalidation. A backup that cannot be produced and verified refuses the write."""
    repo = _kopia_repo(body.repo)
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="confirmed=false - the confirm dialog must run first")
    if not body.target:
        raise HTTPException(status_code=400, detail="target is required")
    return write_rules(repo, body.target, body.changes)


_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def write_rules(repo: str, target: str, changes: Changes, log=None) -> dict:
    """Backup, write, re-read. ``log`` receives one line per step (the job runner's output).
    One write per repo at a time: each write replaces the whole policy from a fresh export, so two
    overlapping writes to one policy would drop the first one's rules."""
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.setdefault(repo, threading.Lock())
    with lock:
        return _write_rules(repo, target, changes, log)


def _write_rules(repo: str, target: str, changes: Changes, log=None) -> dict:
    say = log or (lambda _line: None)
    is_global = _is_global(target)
    target_key = _GLOBAL_TARGET if is_global else target
    # The written policy is built from a fresh export, so fields and rules changed elsewhere survive.
    diff_payload = _build_diff(repo, target, changes, force=True, price=False)
    if diff_payload["errors"]:
        raise HTTPException(status_code=400, detail="Kopia would refuse this policy: " + " ".join(diff_payload["errors"]))
    defined_policies = load_defined_policies(repo).get("policies") or {}

    say("Backing up policies")
    try:
        backup = policybackup.create_backup(repo, "pre-write")
    except BackupError as exc:
        raise HTTPException(status_code=500,
                            detail=f"pre-write policy backup failed; write refused (fail-closed): {exc}") from exc
    say(f"Backup {backup.get('name', '')}".rstrip())

    label = "Global" if is_global else target
    if changes.delete_policy:
        say(f"Deleting the policy for {label}")
        res = policy_delete_execute(repo, None if is_global else target, global_=is_global)
    else:
        if changes.fields:
            n = len(changes.fields)
            say(f"Writing {n} setting{'' if n == 1 else 's'} to {label}")
        if changes.add or changes.remove:
            say(f"Writing {len(diff_payload['resulting_rules'])} rules to {label}"
                f" (+{len(diff_payload['added'])} -{len(diff_payload['removed'])})")
        policy = _draft_policy(defined_policies, target_key,
                               Changes(add=diff_payload["added"], remove=diff_payload["removed"],
                                       fields=changes.fields))
        res = policy_write_rules_execute(repo, None if is_global else target, is_global, policy,
                                         add=len(diff_payload["added"]),
                                         remove=len(diff_payload["removed"]))
    invalidate_after_write(repo, targets=[target_key],
                           ignore_only=not changes.delete_policy and not changes.fields)
    say("Re-reading from kopia")
    ok = res.get("returncode") == 0
    stored_pol = (load_defined_policies(repo).get("policies") or {}).get(target_key) or {}
    stored = list((stored_pol.get("files") or {}).get("ignore") or [])
    fields_match = all(policyresolve.get(stored_pol, p) == policyresolve.get(policy, p)
                       for p in changes.fields) if not changes.delete_policy else True
    output = ((res.get("stdout") or "") + "\n" + (res.get("stderr") or "")).strip()
    return {
        "ok": ok,
        "returncode": res.get("returncode"),
        "output_tail": output[-_BACKUP_OUTPUT_TAIL:],
        "command": _cmd_text(res.get("command") or []),
        "backup": backup,
        "diff": diff_payload,
        # what kopia stored, re-read after the write
        "stored_rules": stored,
        "stored_matches": ok and stored == diff_payload["resulting_rules"] and fields_match,
    }


# ---- backups ---------------------------------------------------------------------------

@router.get("/policies/backups")
def backups(repo: str) -> dict:
    _kopia_repo(repo)
    return {"repo": repo, "dir": policybackup.backup_dir(repo),
            "backups": [{k: v for k, v in e.items() if k != "path"}
                        for e in policybackup.list_backups(repo)]}


@router.post("/policies/backup")
def manual_backup(body: BackupBody) -> dict:
    _kopia_repo(body.repo)
    try:
        info = policybackup.create_backup(body.repo, "manual")
    except BackupError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return info


@router.get("/policies/backup-file")
def backup_file(repo: str, name: str = "") -> dict:
    _kopia_repo(repo)
    try:
        content = policybackup.read_backup(repo, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BackupError as exc:            # encrypted with a key this install does not have
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"repo": repo, "name": name, "content": content}


@router.get("/policies/restore-cmd")
def restore_cmd(repo: str, name: str = "", mode: str = "merge") -> dict:
    """The always-available copy path: the exact kopia command a restore would run."""
    _kopia_repo(repo)
    if mode not in ("merge", "mirror"):
        raise HTTPException(status_code=400, detail="mode must be merge or mirror")
    try:
        path = policybackup._validated_path(repo, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    from ..core import filecrypto
    encrypted = filecrypto.file_state(path) == "encrypted"
    return {"repo": repo, "name": name, "mode": mode,
            "command": _cmd_text(policy_import_command(repo, path, mirror=mode == "mirror")),
            # kopia reads the file itself, and cannot read an encrypted one
            "encrypted": encrypted,
            "note": ("This backup is encrypted on disk, so kopia cannot read it directly. "
                     "Use Restore in KopiaLens instead.") if encrypted else ""}


@router.post("/policies/restore")
def restore(body: RestoreBody) -> dict:
    repo = _kopia_repo(body.repo)
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="confirmed=false - the confirm dialog must run first")
    if body.mode not in ("merge", "mirror"):
        raise HTTPException(status_code=400, detail="mode must be merge or mirror")
    try:
        res = policybackup.restore_backup(repo, body.name, mirror=body.mode == "mirror")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BackupError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    output = ((res.get("stdout") or "") + "\n" + (res.get("stderr") or "")).strip()
    return {"ok": res.get("returncode") == 0, "returncode": res.get("returncode"),
            "output_tail": output[-_BACKUP_OUTPUT_TAIL:],
            "command": _cmd_text(res.get("command") or []),
            "pre_restore_backup": res.get("pre_restore_backup")}
