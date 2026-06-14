"""Retention replica, simulation, and validation."""

from __future__ import annotations

import collections
import datetime
import json
import re

from .cache import cache_db
from .kopia import kopia
from .metrics import cached_refcount, gb
from .sizemap import load_sizemap
from .sources import resolve_source, split_sources
from .walk import walk

RET_RE = re.compile(r"\b(?:latest|hourly|daily|weekly|monthly|annual)-\d+")


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
    keep_ids, reasons = retention_keep(snaps, policy)
    keep_snaps = [snap for snap in snaps if snap["id"] in keep_ids]
    del_snaps = [snap for snap in snaps if snap["id"] not in keep_ids]
    db = cache_db(repo)
    smap = load_sizemap(repo, refresh=refresh)
    membership, _ = split_sources(srcs, mode)
    count_mode = mode if key in membership else "all"
    count, _ = cached_refcount(repo, db, smap, mode=count_mode, recent=None)

    def contents(items: list[dict]) -> set[str]:
        roots = [snap["rootEntry"]["obj"] for snap in items if snap.get("rootEntry", {}).get("obj")]
        cids, _ = walk(repo, roots, db)
        return cids

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
