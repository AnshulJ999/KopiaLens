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
from .sources import resolve_source, roots_of, sources_index


_MAX_INT = 2 ** 62                # kopia's EffectiveKeepLatest returns MaxInt when all six are 0
_INCOMPLETE_YOUNG = datetime.timedelta(hours=4)     # retainIncompleteSnapshotsYoungerThan
_INCOMPLETE_MIN = 3                                  # retainIncompleteSnapshotMinimumCount

_BUCKETS = ("latest", "hourly", "daily", "weekly", "monthly", "annual")

# kopia evaluates its buckets in THIS order (the cases list in retention_policy.go) and shares one
# period-id namespace across them, so the order is part of the answer when a weekly id collides
# with a monthly one (an early-January snapshot: ISO week 1 is "2026-01", and so is the month).
_EVAL_ORDER = ("latest", "annual", "monthly", "weekly", "daily", "hourly")
# The reasons a user sees are canonically sorted after evaluation (SortRetentionTags); pins are
# the app's own tag and stay first.
_REASON_RANK = {"pinned": 0, "latest": 1, "hourly": 2, "daily": 3, "weekly": 4, "monthly": 5,
                "annual": 6}


def _parse_ts(value: str) -> datetime.datetime | None:
    """kopia's startTime is tz-aware ISO; >6 fractional digits are trimmed (Go emits 7-9)."""
    if not value:
        return None
    try:
        t = datetime.datetime.fromisoformat(re.sub(r"\.(\d{1,6})\d*", r".\1", str(value)))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()   # naive means local; keep every comparison comparable


def _incomplete(snap: dict) -> str:
    """kopia's manifest JSON names this ``incomplete`` (a reason string, e.g. "checkpoint") and
    only emits it when set. Reading ``incompleteReason`` instead never matched anything."""
    return snap.get("incomplete") or ""


def _go_add_date(t: datetime.datetime, years: int = 0, months: int = 0,
                 days: int = 0) -> datetime.datetime:
    """Go's Time.AddDate, which NORMALIZES day overflow: Mar 31 minus one month is Mar 2 (leap) or
    Mar 3, never Feb 28. Clamping instead moves the age cutoff and was the porting trap here."""
    y, m = t.year + years, t.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return t.replace(year=y, month=m, day=1) + datetime.timedelta(days=t.day - 1 + days)


def _retention_counts(pol: dict) -> dict[str, int]:
    """A ``policy show --json`` (or resolved effective policy) -> the six bucket counts."""
    ret = (pol or {}).get("retention") or {}
    return {b: int(ret.get("keep" + b.capitalize()) or 0) for b in _BUCKETS}


def _policy_counts(policy: dict) -> dict[str, int]:
    """The six bucket counts from either spelling: the app's own (``latest``/``hourly``/...) or
    kopia's (``keepLatest``/...), which a raw policy dict carries.

    A kopia-spelled dict read as all-zero - and all-zero now means keep EVERYTHING, so a caller
    holding a policy straight from kopia would silently get the opposite of what its policy says."""
    out: dict[str, int] = {}
    for b in _BUCKETS:
        val = policy.get(b)
        if val is None:
            val = policy.get("keep" + b.capitalize())
        out[b] = int(val or 0)
    return out


def keep_all_note(policy: dict) -> str | None:
    """The sentence to show when a policy carries all six zeros, or None.

    kopia treats that as "keep everything" (EffectiveKeepLatest = MaxInt) - the exact opposite of
    "delete everything", which is what a naive reading of six zeros suggests."""
    if any(_policy_counts(policy).values()):
        return None
    return "All retention counts are 0. kopia keeps every snapshot, so nothing gets deleted."


def retention_keep(snaps: list[dict], policy: dict[str, int]) -> tuple[set[str], dict[str, list[str]]]:
    """Port of kopia's ComputeRetentionReasons (snapshot/policy/retention_policy.go).

    A snapshot is kept when a bucket still has room AND the snapshot is not older than that
    bucket's cutoff, where every cutoff is measured from the newest COMPLETE snapshot - `keepHourly
    48` means "within the last 48 hours", not "the 48 latest hours". Pins are always kept (kopia's
    expire step keeps ``len(reasons) > 0 or len(pins) > 0``). An all-zero policy keeps everything:
    kopia's EffectiveKeepLatest becomes MaxInt. Incomplete snapshots only carry the reasons kopia
    gives them - a leading run younger than 4 h or among the first three."""
    ordered = sorted(snaps, key=lambda s: (s.get("startTime", ""), s.get("endTime", "")), reverse=True)
    keep: set[str] = set()
    reasons: dict[str, list[str]] = collections.defaultdict(list)
    for snap in ordered:
        if snap.get("pins"):
            keep.add(snap["id"])
            reasons[snap["id"]].append("pinned")

    counts = _policy_counts(policy)
    keep_latest = _MAX_INT if not any(counts.values()) else counts["latest"]

    times = [_parse_ts(s.get("startTime", "")) for s in ordered]
    complete = [t for s, t in zip(ordered, times)
                if t is not None and not _incomplete(s)]
    max_complete = max(complete) if complete else None
    max_any = max((t for t in times if t is not None), default=None)

    def _cut(shift, n: int) -> datetime.datetime | None:
        # kopia measures every cutoff from the newest complete snapshot; with none it falls back to
        # Go's zero time, i.e. "no cutoff" for all practical purposes.
        return shift(max_complete, n) if max_complete is not None else None

    cutoffs = {
        "latest": None,
        "annual": _cut(lambda t, n: _go_add_date(t, years=-n), counts["annual"]),
        "monthly": _cut(lambda t, n: _go_add_date(t, months=-n), counts["monthly"]),
        "weekly": _cut(lambda t, n: _go_add_date(t, days=-n * 7), counts["weekly"]),
        "daily": _cut(lambda t, n: _go_add_date(t, days=-n), counts["daily"]),
        "hourly": _cut(lambda t, n: t - datetime.timedelta(hours=n), counts["hourly"]),
    }
    limits = {**counts, "latest": keep_latest}

    def _period(kind: str, t: datetime.datetime, i: int) -> str:
        if kind == "latest":
            return str(i)
        if kind == "annual":
            return f"{t.year:04d}"
        if kind == "monthly":
            return f"{t.year:04d}-{t.month:02d}"
        if kind == "weekly":
            # kopia's weekly bucket is the ONLY one measured in the machine's local zone: the code
            # calls StartTime.ToTime().ISOWeek(), and Go's ToTime() renders in the local location.
            # Every other bucket goes through UTCTimestamp.Format, which is UTC. So a snapshot
            # between 00:00 and 05:29 IST belongs to a different ISO week here than in UTC - match
            # kopia's local render or the weekly period ids drift apart.
            iso = t.astimezone().isocalendar()
            return f"{iso[0]:04d}-{iso[1]:02d}"
        if kind == "daily":
            return t.strftime("%Y-%m-%d")
        return t.strftime("%Y-%m-%d %H")

    seen: set[str] = set()
    kept: collections.Counter = collections.Counter()
    for i, (snap, t) in enumerate(zip(ordered, times)):
        if t is None or _incomplete(snap):
            continue                      # incomplete snapshots get their own tag, below
        for kind in _EVAL_ORDER:
            if limits[kind] <= 0:
                continue                  # 0 = defined as zero: this bucket keeps nothing
            cut = cutoffs[kind]
            if cut is not None and t < cut:
                continue
            pid = _period(kind, t, i)
            if pid in seen or kept[kind] >= limits[kind]:
                continue
            seen.add(pid)
            kept[kind] += 1
            keep.add(snap["id"])
            reasons[snap["id"]].append(f"{kind}-{kept[kind]}")

    if max_any is not None:               # kopia's leading-incomplete pass, in list order
        for i, (snap, t) in enumerate(zip(ordered, times)):
            if t is None or not _incomplete(snap):
                break
            if max_any - t < _INCOMPLETE_YOUNG or i < _INCOMPLETE_MIN:
                keep.add(snap["id"])
                reasons[snap["id"]].append("incomplete")
            else:
                break
    for tags in reasons.values():
        tags.sort(key=lambda r: _REASON_RANK.get(r.split("-", 1)[0], 99))
    return keep, reasons


def simulate(
    repo: str,
    source: str,
    policy: dict[str, int],
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
    count = _refcount_map(repo)

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
        "note": keep_all_note(policy),
    }


_SIMALL_CACHE_VERSION = 2   # bump on any retention_keep rule change: old payloads assume old rules


def _simall_cache(repo: str, policy: dict) -> str:
    h = hashlib.md5(f"v{_SIMALL_CACHE_VERSION}|{sorted(policy.items())}".encode())
    srcs = sources_index(repo)
    for key in sorted(srcs):
        for snap in srcs[key]:
            h.update(snap.get("id", "").encode())
    return f"simall_repo{repo}_{h.hexdigest()}"


def simulate_all(repo: str, policy: dict[str, int], refresh: bool = False, cached_only: bool = False) -> dict | None:
    """Simulate retention across EVERY source in one combined pass. Per-source reclaim is the
    content freed and attributable to that source alone (global refcount == 1); the combined
    total is the true union (content that leaves no kept snapshot anywhere) - NOT the per-source
    sum, which double-counts content shared between sources. Cached by policy+snapshot-set."""
    # lazy: core must not import index at module load - every neighbour here does the
    # same, and one eager import is all it takes to make the cycle real.
    from ..index import layer2
    from ..index.physical import _refcount_map, _size_map
    from ..index import sidecache
    cache_name = _simall_cache(repo, policy)
    if not refresh:
        data = sidecache.read(cache_name)
        if data is not None:
            data["cached"] = True
            return data
    if cached_only:        # opening the tab shows a saved result only; it never starts a run
        return None

    srcs = sources_index(repo)
    smap = _size_map(repo)
    display = sorted(srcs)
    count = _refcount_map(repo)

    union_del: set[str] = set()
    global_kept: set[str] = set()
    rows = []
    # ONE reach for every source's kept and deleted sets. Two walks per source measured
    # ~2.0 s each on repo 1 (~170 s across 85 sources) for the same question asked 170 times; the
    # groups all come back from a single recursive pass.
    groups: dict[str, list[str]] = {}
    planned: list[tuple[str, list[dict]]] = []
    for key in display:
        snaps = srcs[key]
        keep_ids, _ = retention_keep(snaps, policy)
        keep_snaps = [s for s in snaps if s["id"] in keep_ids]
        del_snaps = [s for s in snaps if s["id"] not in keep_ids]
        groups[f"{key}|keep"] = roots_of(keep_snaps)
        groups[f"{key}|del"] = roots_of(del_snaps)
        planned.append((key, keep_snaps, del_snaps))
    reached = layer2.reach_by_group(repo, groups)
    for key, keep_snaps, del_snaps in planned:
        snaps = srcs[key]
        kept_c = reached.get(f"{key}|keep") or set()
        del_c = reached.get(f"{key}|del") or set()
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
        "cached": False,
        "note": keep_all_note(policy),
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
    try:
        policy = _retention_counts(json.loads(out))
    except Exception:
        policy = {b: 0 for b in _BUCKETS}
    return policy, out


def kopia_kept_ids(snaps: list[dict]) -> set[str]:
    """What KOPIA keeps, straight from the listing `sources_index` already holds.

    kopia computes a retention reason per snapshot while it lists (``retentionReason`` in the JSON;
    the subprocess backend normalizes it to ``retention``), and its expire step keeps a snapshot on
    ``retentionReason`` OR a pin. The old validator parsed the TEXT listing instead - which
    collapses identical consecutive snapshots unless ``--show-identical`` is passed, so it reported
    false disagreements on 55 of 85 sources."""
    kept = set()
    for snap in snaps:
        if (snap.get("retention") or snap.get("retentionReason") or snap.get("pins")):
            kept.add(snap["id"])
    return kept


def validate_retention(repo: str, source: str) -> dict:
    """One source: the replica against kopia's own reasons. No extra kopia call - the listing is
    the source of truth, and it includes every snapshot kopia actually keeps."""
    key, srcs = resolve_source(repo, source=source)
    snaps = srcs[key]
    policy, raw = parse_policy_retention(repo, key)
    try:
        json.loads(raw)        # all-zero is a KEEP-EVERYTHING policy now, not a parse failure ()
    except Exception:          # noqa: BLE001 - unreadable output is the failure this guard is for
        return {"source": key, "ok": False, "error": "could not parse policy retention", "raw": raw[:1200]}

    mine, _ = retention_keep(snaps, policy)
    kopia_keep = kopia_kept_ids(snaps)
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


def check_retention(repo: str, srcs: dict | None = None, policies: dict | None = None) -> dict:
    """Every source at once: the replica against kopia's own reasons, entirely in-process.

    Called by the nightly right after the policy stage refreshed the effective-policy cache, so
    neither the listing nor a policy costs a subprocess here. A source whose policy the cache does
    not hold is reported as failed, never silently skipped."""
    if srcs is None:
        srcs = sources_index(repo)
    if policies is None:
        from .policy import load_policies              # lazy: core must not import index at load
        policies = load_policies(repo).get("policies") or {}
    mismatches: list[dict] = []
    failed: list[str] = []
    checked = 0
    for key in sorted(srcs):
        snaps = srcs[key]
        if not snaps:
            continue
        pol = policies.get(key)
        if not isinstance(pol, dict) or not pol:
            failed.append(key)
            continue
        mine, _ = retention_keep(snaps, _retention_counts(pol))
        kopia_keep = kopia_kept_ids(snaps)
        checked += 1
        if mine != kopia_keep:
            we_drop = sorted(kopia_keep - mine)
            we_keep = sorted(mine - kopia_keep)
            mismatches.append({"source": key, "snapshots": len(snaps),
                               "ours_keeps": len(mine), "kopia_keeps": len(kopia_keep),
                               "we_drop": len(we_drop), "we_keep": len(we_keep),
                               "sample": (we_drop + we_keep)[:3]})
    return {"checked": checked, "mismatches": mismatches, "failed": failed}
