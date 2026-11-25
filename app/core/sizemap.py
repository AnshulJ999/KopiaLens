"""Content ID to packed-byte map, ported from ``kopiatoolkit.py``."""

from __future__ import annotations

import os
import pickle
import time

from config import CACHEDIR

from .applog import get_logger
from .kopia import kopia_lines

log = get_logger("sizemap")

SIZEMAP_TTL = 6 * 3600

# Run-scoped in-process memo (the nightly worker enables it in worker.main): the 3.5M-entry
# unpickle cost ~1s per call and was paid by every analysis source and every folder detail.
# Scoped to the worker process on purpose - the long-lived app must not pin the map in RAM.
# refresh=True ALWAYS bypasses (and re-sets) the memo: it is the mid-run recovery path
# (metrics.py reloads once when content ids are missing from the map), and the memo returning
# first would silently serve the old map and skip the reload entirely.
MEMO_ENABLED = False
_MEMO: dict[str, dict[str, int]] = {}


def _memo_put(repo: str, sizes: dict[str, int]) -> dict[str, int]:
    if MEMO_ENABLED:
        _MEMO[repo] = sizes
    return sizes


def _read_pkl(path: str) -> dict[str, int] | None:
    try:
        with open(path, "rb") as handle:
            return pickle.load(handle)
    except Exception:  # noqa: BLE001 - missing or unreadable is not an error here
        return None


def load_sizemap(repo: str, refresh: bool = False) -> dict[str, int]:
    if not refresh and MEMO_ENABLED and repo in _MEMO:
        return _MEMO[repo]
    pf = os.path.join(CACHEDIR, f"sizemap_repo{repo}.pkl")
    if not refresh and os.path.exists(pf) and time.time() - os.path.getmtime(pf) < SIZEMAP_TTL:
        cached = _read_pkl(pf)
        if cached is not None:
            return _memo_put(repo, cached)

    # STREAMED, not captured. `content list -l` is the single biggest output kopia produces
    # (one line per distinct chunk - hundreds of MB on a large repo), and buffering it whole
    # meant the stdout string, the splitlines() list and this dict were all alive at once. Only
    # the dict is unavoidable: it is the result.
    sizes: dict[str, int] = {}
    try:
        for line in kopia_lines(repo, ["content", "list", "-l"]):
            fields = line.split()
            if len(fields) < 2:
                continue
            cid = fields[0]
            for token in fields:
                if "+" not in token:
                    continue
                left, _, right = token.partition("+")
                if left.isdigit() and right.isdigit():
                    sizes[cid] = int(right)
                    break
    except Exception as exc:  # noqa: BLE001 - kopia failed part-way through
        # A partial map is worse than an old one: every physical/freeable figure derived from it
        # would silently read low. Prefer the last good map (however stale) and never overwrite
        # it with the partial result.
        stale = _read_pkl(pf)
        if stale is not None:
            log.warning("repo=%s sizemap rebuild failed (%s); serving the previous map (%d contents)",
                        repo, exc, len(stale))
            return _memo_put(repo, stale)
        raise

    os.makedirs(CACHEDIR, exist_ok=True)
    try:
        with open(pf, "wb") as handle:
            pickle.dump(sizes, handle, protocol=4)
    except Exception:
        pass
    return _memo_put(repo, sizes)
