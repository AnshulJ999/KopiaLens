"""Standalone subprocess entry point for a Backblaze index refresh.

Run as `python -m app.backblaze.worker [--force]` from the KopiaLens root (this is exactly what
`indexer.refresh_via_worker()` does). Keeps the ~4 min reindex out of the app's own process so its
own request-handling threads stay responsive throughout. Prints the refresh() summary dict as JSON
to stdout and exits 0; nonzero on error. Read-only on the Backblaze source data, same operation the
app performs on its own background cycle.
"""

from __future__ import annotations

import json
import logging
import os
import sys

# Support being run directly (not via -m) too, mirroring bz_verify.py's pattern.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def main(argv: list) -> int:
    from app.core.applog import setup_worker_logging
    setup_worker_logging(os.environ.get("KOPIALENS_WORKER_LOG"))
    from app.backblaze import indexer
    from app.index import crypto
    if not crypto.worker_key(crypto.read_key_payload(os.environ.get("KOPIALENS_KEY_STDIN") == "1")):
        print(json.dumps({"ok": False, "skipped": True,
                          "reason": "the Backblaze store is encrypted and locked - "
                                    "unlock KopiaLens"}))
        return 0
    force = "--force" in argv
    try:
        summary = indexer.refresh(force=force)
    except Exception as exc:  # noqa: BLE001 - always report a parseable result, never crash silently
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return 1
    print(json.dumps(summary))
    return 0 if summary.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
