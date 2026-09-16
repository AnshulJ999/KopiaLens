"""Retention replica, simulation, and validation."""

from __future__ import annotations

import collections
import datetime
import hashlib
import json
import os
import re

from config import CACHEDIR

from .kopia import kopia
from .metrics import gb
from .sources import resolve_source, roots_of, sources_index, split_sources

RET_RE = re.compile(r"\b(?:latest|hourly|daily|weekly|monthly|annual)-\d+")


def _refcount_for_mode(repo: str, srcs: dict, mode: str) -> dict[str, int]:
    """The per-source content refcount over the sources a mode selects.

    ``mode="all"`` reads the generation's published `crefs` map. Any other mode counts the selected
    sources' tree reach - the same Counter the walk-based path built, now sourced from the tree
    instead of the walk cache. ````: the walk is gone, the numbers are not.
    """
    from ..index import layer2                      # lazy: core must not import index at load
    from ..index.physical import _refcount_map
    if mode == "all":
        return _refcount_map(repo)
    membership, _ = split_sources(srcs, mode)
    count: collections.Counter = collections.Counter()
    for key in membership:
        count.update(layer2.reach_content_ids(repo, roots_of(srcs[key])))
    return count


def retention_keep(snaps: list[dict], policy: dict[str, int]) -> tuple[set[str], dict[str, list[str]]]:
    ordered = sorted(snaps, key=lambda item: item.get("startTime", ""), reverse=True)
    keep: set[str] = set()
    reasons: dict[str, list[str]] = collections.defaultdict(list)
    for snap in ordered:
        if snap.get("pins"):
            keep.add(snap["id"])
            reasons[snap["id"]].append("pinned")

    cats = [
        ("hourly", "%Y-%m-%d-%H"),
        ("daily", "%Y-%m-%d"),
        ("weekly", "%G-%V"),
        ("monthly", "%Y-%m"),
        ("annual", "%Y"),
    ]
    seen = {cat: set() for cat, _ in cats}
    kept = {cat: 0 for cat, _ in cats}
    latest = 0
    for snap in ordered:
        timestamp = snap.get("startTime", "")
        try:
            dt = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except Exception:
            continue
        if latest < policy.get("latest", 0):
            keep.add(snap["id"])
            reasons[snap["id"]].append(f"latest-{latest + 1}")
            latest += 1
        for cat, fmt in cats:
            period = dt.strftime(fmt)
            if period in seen[cat] or kept[cat] >= policy.get(cat, 0):
                continue
            seen[cat].add(period)
            kept[cat] += 1
            keep.add(snap["id"])
            reasons[snap["id"]].append(f"{cat}-{kept[cat]}")
    return keep, reasons


def simulate(
    repo: str,
    source: str,
    policy: dict[str, int],
    mode: str = "all",
    refresh: bool = False,
) -> dict:
    key, srcs = resolve_source(repo, source=source)
    snaps = srcs[key]
    # lazy: core must not import index at module load - every neighbour here does the
    # same, and one eager import is all it takes to make the cycle real.
    from ..index import layer2
    from ..index.physical import _refcount_map, _size_map
    keep_ids, reasons = retention_keep(snaps, policy)
    keep_snaps = [snap for snap in snaps if snap["id"] in keep_ids]
    del_snaps = [snap for snap in snaps if snap["id"] not in keep_ids]
    smap = _size_map(repo, refresh=refresh)
    membership, _ = split_sources(srcs, mode)
    count_mode = mode if key in membership else "all"
    count = _refcount_for_mode(repo, srcs, count_mode)

    def contents(items: list[dict]) -> set[str]:
        roots = [snap["rootEntry"]["obj"] for snap in items if snap.get("rootEntry", {}).get("obj")]
        return layer2.reach_content_ids(repo, roots)

    kept_content = contents(keep_snaps) if keep_snaps else set()
    deleted_content = contents(del_snaps) if del_snaps else set()
    candidates = deleted_content - kept_content
    reclaim = sum(smap.get(cid, 0) for cid in candidates if count.get(cid, 0) == 1)
    pinned = sum(smap.get(cid, 0) for cid in candidates if count.get(cid, 0) != 1)
    return {
        "source": key,
        "policy": policy,
        "total_snapshots": len(snaps),
        "keep_snapshots": len(keep_snaps),
        "delete_snapshots": len(del_snaps),
        "reclaim": reclaim,
        "reclaim_gb": gb(reclaim),
        "pinned_by_other_sources": pinned,
        "pinned_by_other_sources_gb": gb(pinned),
        "delete_ids": [snap["id"] for snap in del_snaps],
        "reasons": dict(reasons),
    }


def _simall_cache(repo: str, policy: dict, mode: str) -> str:
    h = hashlib.md5(f"{sorted(policy.items())}|{mode}".encode())
    srcs = sources_index(repo)
    for key in sorted(srcs):
        for snap in srcs[key]:
            h.update(snap.get("id", "").encode())
    return f"simall_repo{repo}_{h.hexdigest()}"


def simulate_all(repo: str, policy: dict[str, int], mode: str = "all", refresh: bool = False) -> dict:
    """Simulate retention across EVERY source in one combined pass. Per-source reclaim is the
    content freed and attributable to that source alone (global refcount == 1); the combined
    total is the true union (content that leaves no kept snapshot anywhere) - NOT the per-source
    sum, which double-counts content shared between sources. Cached by policy+mode+snapshot-set."""
    # lazy: core must not import index at module load - every neighbour here does the
    # same, and one eager import is all it takes to make the cycle real.
    from ..index import layer2
    from ..index.physical import _refcount_map, _size_map
    from ..index import sidecache
    cache_name = _simall_cache(repo, policy, mode)
    if not refresh:
        data = sidecache.read(cache_name)
        if data is not None:
            data["cached"] = True
            return data

    srcs = sources_index(repo)
    smap = _size_map(repo)
    _, display = split_sources(srcs, mode)
    count = _refcount_for_mode(repo, srcs, mode)

    union_del: set[str] = set()
    global_kept: set[str] = set()
    rows = []
    for key in display:
        snaps = srcs[key]
        keep_ids, _ = retention_keep(snaps, policy)
        keep_snaps = [s for s in snaps if s["id"] in keep_ids]
        del_snaps = [s for s in snaps if s["id"] not in keep_ids]
        kept_c = layer2.reach_content_ids(repo, roots_of(keep_snaps)) if keep_snaps else set()
        del_c = layer2.reach_content_ids(repo, roots_of(del_snaps)) if del_snaps else set()
        cand = del_c - kept_c
        reclaim = sum(smap.get(cid, 0) for cid in cand if count.get(cid, 0) == 1)
        rows.append({
            "source": key,
            "total_snapshots": len(snaps),
            "keep_snapshots": len(keep_snaps),
            "delete_snapshots": len(del_snaps),
            "reclaim": reclaim,
            "reclaim_gb": gb(reclaim),
            "pins": sum(1 for s in snaps if s.get("pins")),
        })
        union_del |= del_c
        global_kept |= kept_c

    combined = sum(smap.get(cid, 0) for cid in (union_del - global_kept))
    sum_per = sum(r["reclaim"] for r in rows)
    data = {
        "rows": sorted(rows, key=lambda r: r["reclaim"], reverse=True),
        "combined_reclaim": combined,
        "combined_reclaim_gb": gb(combined),
        "sum_per_source": sum_per,
        "shared_between_sources": combined - sum_per,
        "shared_between_sources_gb": gb(combined - sum_per),
        "policy": policy,
        "mode": mode,
        "cached": False,
    }
    sidecache.write(cache_name, data)
    return data


def set_reclaim(repo: str, delete_ids: list[str], refresh: bool = False) -> dict:
    """Accurate reclaim for an ARBITRARY set of snapshots (what the delete panel needs): the true
    ``union(deleted content) - union(kept content)``, NOT the per-snapshot sum (which under-counts
    content shared among the deleted set but exclusive to none of them individually).

    Kept = every OTHER snapshot of each affected source. ``refcount<=#affected-sources`` excludes
    content also held by an unaffected source. EXACT for a single-source selection (the normal
    case); for a cross-source selection it is approximate (cross-source shared churn not fully
    deducted) and flagged ``exact=false``."""
    # lazy: core must not import index at module load - every neighbour here does the
    # same, and one eager import is all it takes to make the cycle real.
    from ..index import layer2
    from ..index.physical import _refcount_map, _size_map
    delete_set = set(delete_ids)
    srcs = sources_index(repo)
    affected = sorted({k for k, lst in srcs.items() if any(s["id"] in delete_set for s in lst)})
    if not affected:
        return {"reclaim": 0, "reclaim_gb": 0.0, "delete_snapshots": 0, "sources": [], "exact": True}
    smap = _size_map(repo, refresh=refresh)
    count = _refcount_map(repo)
    del_snaps, kept_snaps = [], []
    for key in affected:
        for snap in srcs[key]:
            (del_snaps if snap["id"] in delete_set else kept_snaps).append(snap)
    del_c = layer2.reach_content_ids(repo, roots_of(del_snaps)) if del_snaps else set()
    kept_c = layer2.reach_content_ids(repo, roots_of(kept_snaps)) if kept_snaps else set()
    candidates = del_c - kept_c
    na = len(affected)
    reclaim = sum(smap.get(cid, 0) for cid in candidates if count.get(cid, 0) <= na)
    return {
        "reclaim": reclaim,
        "reclaim_gb": gb(reclaim),
        "delete_snapshots": len(del_snaps),
        "kept_snapshots": len(kept_snaps),
        "sources": affected,
        "exact": na == 1,
    }


def parse_policy_retention(repo: str, target: str) -> tuple[dict[str, int], str]:
    out = kopia(repo, ["policy", "show", target, "--json"]).stdout
    policy = {"latest": 0, "hourly": 0, "daily": 0, "weekly": 0, "monthly": 0, "annual": 0}
    fmap = {
        "latest": "keepLatest",
        "hourly": "keepHourly",
        "daily": "keepDaily",
        "weekly": "keepWeekly",
        "monthly": "keepMonthly",
        "annual": "keepAnnual",
    }
    try:
        retention = json.loads(out).get("retention", {})
        for key, json_key in fmap.items():
            policy[key] = retention.get(json_key, 0) or 0
    except Exception:
        pass
    return policy, out


def validate_retention(repo: str, source: str) -> dict:
    key, srcs = resolve_source(repo, source=source)
    snaps = srcs[key]
    policy, raw = parse_policy_retention(repo, key)
    if not any(policy.values()):
        return {"source": key, "ok": False, "error": "could not parse policy retention", "raw": raw[:1200]}

    mine, _ = retention_keep(snaps, policy)
    all_ids = {snap["id"] for snap in snaps}
    text = kopia(repo, ["snapshot", "list", key, "--manifest-id"]).stdout
    kopia_keep: set[str] = set()
    for line in text.splitlines():
        if not (RET_RE.search(line) or "pins:" in line):
            continue
        for token in re.findall(r"[0-9a-f]{16,40}", line):
            if token in all_ids:
                kopia_keep.add(token)

    only_mine = sorted(mine - kopia_keep)
    only_kopia = sorted(kopia_keep - mine)
    return {
        "source": key,
        "ok": not only_mine and not only_kopia and bool(kopia_keep),
        "policy": policy,
        "snapshots": len(snaps),
        "replica_keeps": len(mine),
        "kopia_keeps": len(kopia_keep),
        "agree": len(mine & kopia_keep),
        "replica_only": only_mine,
        "kopia_only": only_kopia,
    }
