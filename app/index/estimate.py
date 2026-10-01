"""First-index estimates: index size and run time, always as ranges.

Fitted on measured first indexes of real repositories on local storage; see the PERF5 measurements
report. The tree a first index reads grows with how much each source changed between snapshots,
which the snapshot listing cannot tell, hence a range rather than one figure.
"""

from __future__ import annotations

import math

ENTRY_FACTOR = (6.0, 12.0)      # folder entries read per entry of a source's newest snapshot
BYTES_PER_ENTRY = (28, 40)      # index bytes per folder entry
FIXED_BYTES = 1_000_000
SECONDS_PER_M_ENTRIES = 26.0    # worker defaults, local storage
FIXED_SECONDS = 8.0
SPEED_SPREAD = (0.8, 1.25)

# Share of a first index's time per stage (repo 2, worker defaults).
STAGE_SHARE = {"index": 0.658, "dashboard": 0.008, "analysis": 0.088, "churn": 0.036,
               "folders": 0.100, "policies": 0.022, "policy_prices": 0.058,
               "ignore_files": 0.010, "rank_paths": 0.020, "next_snapshots": 0.0}


def _entries(snap: dict) -> int:
    summ = (snap.get("rootEntry") or {}).get("summ") or {}
    return int(summ.get("files") or 0) + int(summ.get("dirs") or 0)


def entries_range(srcs: dict) -> tuple[int, int]:
    """Folder entries a first index reads: per source, the newest snapshot times ENTRY_FACTOR,
    never more than every snapshot's entries summed (a source with few snapshots)."""
    lo = hi = 0
    for snaps in srcs.values():
        if not snaps:
            continue
        newest = _entries(max(snaps, key=lambda s: str(s.get("startTime") or "")))
        every = sum(_entries(s) for s in snaps)
        lo += min(every, int(newest * ENTRY_FACTOR[0]))
        hi += min(every, int(newest * ENTRY_FACTOR[1]))
    return lo, hi


def _seconds(entries: int) -> float:
    return FIXED_SECONDS + SECONDS_PER_M_ENTRIES * entries / 1e6


def from_entries(lo: int, hi: int, exact: bool = False) -> dict:
    """Size and time ranges for a known (``exact``) or estimated range of folder entries."""
    secs = (_seconds(lo) * SPEED_SPREAD[0], _seconds(hi) * SPEED_SPREAD[1])
    mid = math.sqrt(secs[0] * secs[1])
    return {"entries": [lo, hi], "exact": exact,
            "bytes": [FIXED_BYTES + lo * BYTES_PER_ENTRY[0], FIXED_BYTES + hi * BYTES_PER_ENTRY[1]],
            "seconds": [round(secs[0]), round(secs[1])],
            "stage_seconds": {k: round(mid * v, 1) for k, v in STAGE_SHARE.items()}}


def from_listing(srcs: dict) -> dict:
    """``srcs``: {source key: [snapshot, ...]} as kopia lists them."""
    return from_entries(*entries_range(srcs))
