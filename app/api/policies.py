"""Policies tab API ( +).

Read half: overview (global + sources with inheritance state), detail, "keeping out" pricing
against the index. Write half: staged diff -> fail-closed pre-write backup -> `kopia policy
set/delete` -> cache invalidation; backups list/read/manual/restore. 
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import kopia_repos, repo_kind

from ..core import policybackup, policytargets
from ..core.kopia import (
    _quote_arg,
    policy_delete_command,
    policy_delete_execute,
    policy_import_command,
    policy_set_command,
    policy_set_execute,
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
_EXAMPLES_MAX = 200  # endpoint-level example cap (the pricing cache itself stores 500)


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


def _pricing_scope(repo: str, target: str) -> list[str]:
    """Sources whose effective ignore rules a write to ``target`` actually changes, and therefore
    what pricing must cover: the global policy prices over every source that inherits global
    rules; a source target prices over ITSELF plus every source inheriting FROM it ( - a
    rule at an ancestor policy re-anchors to each inheritor's walk root, so the per-target price
    must cover the target's full reach, not just its own source)."""
    defined = load_defined_policies(repo).get("policies") or {}
    if _is_global(target):
        return [key for key in _source_keys(repo)
                if effective_ignore_origin(repo, key, defined)[0] == "global"]
    out = [target]
    for key in _source_keys(repo):
        origin, owner = effective_ignore_origin(repo, key, defined)
        if origin == "inherited" and owner == target:
            out.append(key)
    return out


def _price_for_target(repo: str, target: str, patterns: list[str], limit: int = 10) -> dict:
    """Price patterns against a source, or - for a target whose rules other sources inherit -
    aggregated over that full reach (per-source breakdown included). The rollup is
    single-flighted: concurrent identical requests block on one lock and then hit the price
    cache the leader populated."""
    scope = _pricing_scope(repo, target)
    if len(scope) > 1:
        merged: dict[str, dict] = {}
        breakdown = []
        lock = policypricing.rollup_lock((repo, tuple(sorted(patterns))))
        with lock:
            per_source = {key: policypricing.price_patterns(repo, key, patterns,
                                                            limit=_EXAMPLES_MAX)
                          for key in scope}
        for key in scope:
            out = per_source[key]
            totals = {}
            for res in out["results"]:
                item = merged.setdefault(res["pattern"], {
                    "pattern": res["pattern"], "files": 0, "bytes": 0, "dirs": 0,
                    "examples": [], "error": res["error"], "negated": res["negated"]})
                if res["error"]:
                    continue
                item["files"] += res["files"]
                item["bytes"] += res["bytes"]
                item["dirs"] += res["dirs"]
                item["examples"].extend(res["examples"])
                totals[res["pattern"]] = {"files": res["files"], "bytes": res["bytes"]}
            breakdown.append({"source": key, "results": totals})
        results = []
        for item in merged.values():
            item["examples"].sort(key=lambda e: e["size"], reverse=True)
            item["total_examples"] = item["files"]
            results.append(item)
        results.sort(key=lambda r: patterns.index(r["pattern"]) if r["pattern"] in patterns else 0)
        return {"target": target, "scope": {"sources": scope, "inheriting_global": len(scope)},
                "results": results, "breakdown": breakdown}
    out = policypricing.price_patterns(repo, target, patterns, limit=_EXAMPLES_MAX)
    for res in out["results"]:
        res["total_examples"] = res["files"] if not res["error"] else 0
        res["capped"] = res["total_examples"] > limit
        res["examples"] = res["examples"][:limit]
    return {"target": target,
            "scope": {"sources": [target], "inheriting_global": 0},
            "results": out["results"]}


# ---- models ----------------------------------------------------------------------------

class PriceBody(BaseModel):
    repo: str = "1"
    target: str
    patterns: list[str] = Field(default_factory=list)
    limit: int = 10


class Changes(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    delete_policy: bool = False


class DiffBody(BaseModel):
    repo: str = "1"
    target: str
    changes: Changes


class SaveBody(DiffBody):
    confirmed: bool = False


class BackupBody(BaseModel):
    repo: str = "1"


class RestoreBody(BaseModel):
    repo: str = "1"
    name: str
    mode: str = "merge"
    confirmed: bool = False


class TargetsBody(BaseModel):
    repo: str = "1"
    path: str
    source: str | None = None     # the panel the dialog opened from: disambiguates user@host
    is_folder: bool = False
    shape: str = "exact"          # exact | name | extension | parent
    price: bool = False           # opt-in impact pricing; the dialog ranks by covers count


# ---- read half -------------------------------------------------------------------------

@router.get("/policies/overview")
def overview(repo: str = "1") -> dict:
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
    return {"repo": repo, "stale": not fresh, "global": global_entry, "sources": sources}


@router.get("/policies/detail")
def detail(repo: str = "1", target: str = "") -> dict:
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
        return {"target": body.target, "scope": {"sources": [], "inheriting_global": 0},
                "results": []}
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
def stored_prices(repo: str = "1", target: str = "") -> dict:
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


@router.post("/policies/exclusion-targets")
def exclusion_targets(body: TargetsBody) -> dict:
    """Owning policies that can receive an exclusion for ``path`` (Option A: a source whose
    rules are inherited is never offered - its owner is), deduped, ranked by reach (matched
    covers first). ``price: false`` (default) keeps this a pure resolution call - zero index
    scans; impact pricing is opt-in and small-scope."""
    _kopia_repo(body.repo)
    try:
        return policytargets.resolve_targets(
            body.repo, body.path, source=body.source,
            is_folder=body.is_folder, shape=body.shape, price=body.price)
    except TargetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- write half ------------------------------------------------------------------------

def _validate_changes(repo: str, target: str, changes: Changes) -> tuple[list[str], list[str], dict]:
    """Shared diff/save validation. Returns (current_rules, resulting_rules, defined_policies)."""
    defined, _fresh = refresh_defined_policies(repo)
    defined_policies = defined.get("policies") or {}
    current = _own_rules(defined_policies, _GLOBAL_TARGET if _is_global(target) else target)
    add = list(dict.fromkeys(changes.add))
    remove = list(dict.fromkeys(changes.remove))

    if changes.delete_policy:
        if add or remove:
            raise HTTPException(status_code=400,
                                detail="delete_policy cannot be combined with add/remove")
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


def _build_diff(repo: str, target: str, changes: Changes) -> dict:
    current, resulting, defined_policies = _validate_changes(repo, target, changes)
    add = list(dict.fromkeys(changes.add))
    remove = list(dict.fromkeys(changes.remove))
    priced = []
    if not changes.delete_policy and (add or remove):
        priced = _price_for_target(repo, target, add + remove, limit=_EXAMPLES_MAX)["results"]
    by_pattern = {r["pattern"]: r for r in priced}
    impact = {
        "added": [by_pattern.get(p) for p in add],
        "removed": [by_pattern.get(p) for p in remove],
    }
    target_key = _GLOBAL_TARGET if _is_global(target) else target
    will_create = (not changes.delete_policy and (add or remove)
                   and target_key not in defined_policies)
    if changes.delete_policy:
        command = policy_delete_command(repo, None if _is_global(target) else target,
                                        global_=_is_global(target))
    else:
        command = policy_set_command(repo, None if _is_global(target) else target,
                                     global_=_is_global(target),
                                     add_ignore=add or None, remove_ignore=remove or None)
    return {
        "repo": repo, "target": target,
        "current_rules": current, "resulting_rules": resulting,
        "added": add, "removed": remove,
        "impact": impact, "will_create_policy": will_create,
        "command": _cmd_text(command),
        "backup_dir": policybackup.backup_dir(repo),
    }


@router.post("/policies/diff")
def diff(body: DiffBody) -> dict:
    """Review data for staged edits - nothing is written."""
    _kopia_repo(body.repo)
    if not body.target:
        raise HTTPException(status_code=400, detail="target is required")
    return _build_diff(body.repo, body.target, body.changes)


@router.post("/policies/save")
def save(body: SaveBody) -> dict:
    """Preview -> confirm dialog -> HERE: fail-closed backup, then the write, then cache
    invalidation. A backup that cannot be produced and verified refuses the write."""
    repo = _kopia_repo(body.repo)
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="confirmed=false - the confirm dialog must run first")
    if not body.target:
        raise HTTPException(status_code=400, detail="target is required")
    diff_payload = _build_diff(repo, body.target, body.changes)

    try:
        backup = policybackup.create_backup(repo, "pre-write")
    except BackupError as exc:
        raise HTTPException(status_code=500,
                            detail=f"pre-write policy backup failed; write refused (fail-closed): {exc}") from exc

    if body.changes.delete_policy:
        res = policy_delete_execute(repo, None if _is_global(body.target) else body.target,
                                    global_=_is_global(body.target))
    else:
        res = policy_set_execute(repo, None if _is_global(body.target) else body.target,
                                 global_=_is_global(body.target),
                                 add_ignore=list(dict.fromkeys(body.changes.add)) or None,
                                 remove_ignore=list(dict.fromkeys(body.changes.remove)) or None)
    invalidate_after_write(repo, targets=[_GLOBAL_TARGET] if _is_global(body.target) else [body.target],
                           ignore_only=not body.changes.delete_policy)
    output = ((res.get("stdout") or "") + "\n" + (res.get("stderr") or "")).strip()
    return {
        "ok": res.get("returncode") == 0,
        "returncode": res.get("returncode"),
        "output_tail": output[-_BACKUP_OUTPUT_TAIL:],
        "command": _cmd_text(res.get("command") or []),
        "backup": backup,
        "diff": diff_payload,
    }


# ---- backups ---------------------------------------------------------------------------

@router.get("/policies/backups")
def backups(repo: str = "1") -> dict:
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
def backup_file(repo: str = "1", name: str = "") -> dict:
    _kopia_repo(repo)
    try:
        content = policybackup.read_backup(repo, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"repo": repo, "name": name, "content": content}


@router.get("/policies/restore-cmd")
def restore_cmd(repo: str = "1", name: str = "", mode: str = "merge") -> dict:
    """The always-available copy path: the exact kopia command a restore would run."""
    _kopia_repo(repo)
    if mode not in ("merge", "mirror"):
        raise HTTPException(status_code=400, detail="mode must be merge or mirror")
    try:
        path = policybackup._validated_path(repo, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"repo": repo, "name": name, "mode": mode,
            "command": _cmd_text(policy_import_command(repo, path, mirror=mode == "mirror"))}


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
