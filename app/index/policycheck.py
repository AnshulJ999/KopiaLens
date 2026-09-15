"""Nightly check: KopiaLens' inheritance copy against kopia's own `policy show` output.

Runs in the worker's policies stage. It re-checks only when the defined policies or the kopia
binary changed since the last clean run, and logs loudly on any mismatch (a kopia upgrade that
changed inheritance shows up here first). Never fails the stage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time

from config import CACHEDIR

log = logging.getLogger("kopialens.policycheck")


def _path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"policy_check_repo{repo}.json")


def load(repo: str) -> dict | None:
    try:
        with open(_path(repo), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 - never checked yet
        return None


def _fingerprint(defined: dict, targets: list[str]) -> str:
    h = hashlib.sha256(json.dumps(defined, sort_keys=True).encode("utf-8"))
    h.update("\n".join(targets).encode("utf-8"))
    exe = shutil.which("kopia")
    if exe:
        try:
            st = os.stat(exe)
            h.update(f"{exe}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8"))
        except OSError:
            pass
    return h.hexdigest()


def run(repo: str, force: bool = False) -> dict:
    from ..core import policyresolve
    from ..core.policy import load_defined_policies
    from ..core.sources import sources_index

    defined = load_defined_policies(repo).get("policies") or {}
    targets = sorted(set(defined) | set(sources_index(repo)))
    fp = _fingerprint(defined, targets)
    prev = load(repo) or {}
    if not force and prev.get("fingerprint") == fp and not prev.get("mismatches"):
        return prev
    t0 = time.time()
    out = policyresolve.check(repo, defined, targets)
    rec = {"repo": repo, "ts": time.time(), "fingerprint": fp, "elapsed": round(time.time() - t0, 1),
           "checked": out["checked"], "failed": out["failed"], "mismatches": out["mismatches"]}
    if out["mismatches"]:
        log.error("repo=%s policy inheritance check: %d of %d targets differ from kopia's own output "
                  "(kopia upgrade?): %s", repo, len(out["mismatches"]), out["checked"],
                  "; ".join(f"{m['target']}: {m['diff'][0]}" for m in out["mismatches"][:3]))
    else:
        log.info("repo=%s policy inheritance check: %d targets match kopia (%.1fs)",
                 repo, out["checked"], rec["elapsed"])
    try:
        os.makedirs(CACHEDIR, exist_ok=True)
        tmp = _path(repo) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, _path(repo))
    except OSError as exc:
        log.warning("repo=%s policy check result not saved: %s", repo, exc)
    return rec
