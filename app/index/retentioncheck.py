"""Nightly check: the retention replica against kopia's own retentionReason.

Runs in the worker's policies stage, right after the per-source effective-policy cache is
refreshed - both inputs (the listing's retentionReason and each source's counts) are already in
hand, so the whole check costs no kopia subprocess. A mismatch is logged loudly and rides the
record the Stats tab reads; it never fails the stage.
"""

from __future__ import annotations

import json
import logging
import os
import time

from config import CACHEDIR

log = logging.getLogger("kopialens.retentioncheck")


def _cache_name(repo: str) -> str:
    return f"retention_check_repo{repo}"


def load(repo: str) -> dict | None:
    from . import sidecache
    data = sidecache.read(_cache_name(repo))
    return data if isinstance(data, dict) else None


def run(repo: str) -> dict:
    from ..core import retention

    t0 = time.time()
    out = retention.check_retention(repo)
    rec = {"repo": repo, "ts": time.time(), "elapsed": round(time.time() - t0, 1),
           "checked": out["checked"], "mismatches": out["mismatches"], "failed": out["failed"]}
    if out["mismatches"]:
        log.error("repo=%s retention check: %d of %d sources differ from kopia's own retention "
                  "reasons", repo, len(out["mismatches"]), out["checked"])
        log.debug("retention mismatches: %s",
                  "; ".join(f"{m['source']} (we drop {m['we_drop']}, we keep {m['we_keep']})"
                            for m in out["mismatches"][:3]))
    else:
        log.info("repo=%s retention check: %d sources match kopia (%.1fs)",
                 repo, out["checked"], rec["elapsed"])
    from . import sidecache
    if not sidecache.write(_cache_name(repo), rec):
        log.warning("repo=%s retention check result not saved", repo)
    return rec
