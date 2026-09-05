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


def _read_pkl(path: str) -> dict[str, int] | None:
    try:
        with open(path, "rb") as handle:
            return pickle.load(handle)
    except Exception:  # noqa: BLE001 - missing or unreadable is not an error here
        return None


def load_sizemap(repo: str, refresh: bool = False) -> dict[str, int]:
    pf = os.path.join(CACHEDIR, f"sizemap_repo{repo}.pkl")
    if not refresh and os.path.exists(pf) and time.time() - os.path.getmtime(pf) < SIZEMAP_TTL:
        cached = _read_pkl(pf)
        if cached is not None:
            return cached

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
            log.error("repo=%s sizemap rebuild failed (%s); serving the previous map (%d contents)",
                      repo, exc, len(stale))
            return stale
        raise

    os.makedirs(CACHEDIR, exist_ok=True)
    try:
        with open(pf, "wb") as handle:
            pickle.dump(sizes, handle, protocol=4)
    except Exception:
        pass
    return sizes
