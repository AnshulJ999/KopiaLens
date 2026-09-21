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


def _path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"retention_check_repo{repo}.json")


def load(repo: str) -> dict | None:
    try:
        with open(_path(repo), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 - never checked yet
        return None


def run(repo: str) -> dict:
    from ..core import retention

    t0 = time.time()
    out = retention.check_retention(repo)
    rec = {"repo": repo, "ts": time.time(), "elapsed": round(time.time() - t0, 1),
           "checked": out["checked"], "mismatches": out["mismatches"], "failed": out["failed"]}
    if out["mismatches"]:
        log.error("repo=%s retention check: %d of %d sources differ from kopia's own retention "
                  "reasons: %s", repo, len(out["mismatches"]), out["checked"],
                  "; ".join(f"{m['source']} (we drop {m['we_drop']}, we keep {m['we_keep']})"
                            for m in out["mismatches"][:3]))
    else:
        log.info("repo=%s retention check: %d sources match kopia (%.1fs)",
                 repo, out["checked"], rec["elapsed"])
    try:
        os.makedirs(CACHEDIR, exist_ok=True)
        tmp = _path(repo) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, _path(repo))
    except OSError as exc:
        log.warning("repo=%s retention check result not saved: %s", repo, exc)
    return rec
