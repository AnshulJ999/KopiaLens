"""Content ID to packed-byte map, ported from ``kopiatoolkit.py``."""

from __future__ import annotations

import os
import pickle
import time

from config import CACHEDIR

from .kopia import kopia

SIZEMAP_TTL = 6 * 3600


def load_sizemap(repo: str, refresh: bool = False) -> dict[str, int]:
    pf = os.path.join(CACHEDIR, f"sizemap_repo{repo}.pkl")
    if not refresh and os.path.exists(pf) and time.time() - os.path.getmtime(pf) < SIZEMAP_TTL:
        try:
            with open(pf, "rb") as handle:
                return pickle.load(handle)
        except Exception:
            pass

    sizes: dict[str, int] = {}
    for line in kopia(repo, ["content", "list", "-l"]).stdout.splitlines():
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

    os.makedirs(CACHEDIR, exist_ok=True)
    try:
        with open(pf, "wb") as handle:
            pickle.dump(sizes, handle, protocol=4)
    except Exception:
        pass
    return sizes
