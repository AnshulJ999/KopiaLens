"""First-index estimates: index size and run time, always as ranges.

Fitted on measured first indexes of real repositories on local storage. The tree a first index
reads grows with how much each source changed between snapshots,
which the snapshot listing cannot tell, hence a range rather than one figure.
"""

from __future__ import annotations

import math

ENTRY_FACTOR = (6.0, 12.0)      # folder entries read per entry of a source's newest snapshot
FOLDER_FACTOR = (1.8, 3.2)      # folder objects read per folder of a source's newest snapshot
BYTES_PER_ENTRY = (28, 40)      # index bytes per folder entry
FIXED_BYTES = 1_000_000
FIXED_SECONDS = 8.0             # worker defaults, local storage
SECONDS_PER_M_ENTRIES = 14.4
SECONDS_PER_M_FOLDERS = 566.0   # each folder is a kopia read
SPEED_SPREAD = (0.8, 1.25)

# Share of a first index's time per stage (repos 1 and 2, worker defaults).
STAGE_SHARE = {"index": 0.657, "dashboard": 0.009, "analysis": 0.076, "churn": 0.047,
               "folders": 0.097, "policies": 0.020, "policy_prices": 0.065,
               "ignore_files": 0.010, "rank_paths": 0.020, "next_snapshots": 0.0}


def _summ(snap: dict) -> tuple[int, int]:
    summ = (snap.get("rootEntry") or {}).get("summ") or {}
    files, dirs = int(summ.get("files") or 0), int(summ.get("dirs") or 0)
    return files + dirs, dirs


def tree_range(srcs: dict) -> dict:
    """Folder entries and folders a first index reads: per source, the newest snapshot times a
    factor, never more than every snapshot summed (a source with few snapshots)."""
    out = {"entries": [0, 0], "folders": [0, 0]}
    for snaps in srcs.values():
        if not snaps:
            continue
        newest = _summ(max(snaps, key=lambda s: str(s.get("startTime") or "")))
        every = [sum(x) for x in zip(*(_summ(s) for s in snaps))]
        for i, (key, factor) in enumerate((("entries", ENTRY_FACTOR), ("folders", FOLDER_FACTOR))):
            out[key][0] += min(every[i], int(newest[i] * factor[0]))
            out[key][1] += min(every[i], int(newest[i] * factor[1]))
    return out


def _seconds(entries: int, folders: int) -> float:
    return FIXED_SECONDS + (SECONDS_PER_M_ENTRIES * entries + SECONDS_PER_M_FOLDERS * folders) / 1e6


def from_tree(entries: list[int], folders: list[int], exact: bool = False) -> dict:
    """Size and time ranges for a known (``exact``) or estimated tree."""
    secs = (_seconds(entries[0], folders[0]) * SPEED_SPREAD[0],
            _seconds(entries[1], folders[1]) * SPEED_SPREAD[1])
    mid = math.sqrt(secs[0] * secs[1])
    return {"entries": list(entries), "folders": list(folders), "exact": exact,
            "bytes": [FIXED_BYTES + entries[0] * BYTES_PER_ENTRY[0],
                      FIXED_BYTES + entries[1] * BYTES_PER_ENTRY[1]],
            "seconds": [round(secs[0]), round(secs[1])],
            "stage_seconds": {k: round(mid * v, 1) for k, v in STAGE_SHARE.items()}}


def from_listing(srcs: dict) -> dict:
    """``srcs``: {source key: [snapshot, ...]} as kopia lists them."""
    t = tree_range(srcs)
    return from_tree(t["entries"], t["folders"])
