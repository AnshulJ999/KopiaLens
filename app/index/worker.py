"""Standalone subprocess entry point for a repo's nightly heavy pass.

Run as ``python -m app.index.worker <repo> [--full]`` from the KopiaLens root - which is exactly
what ``app.main`` does once a day. Everything expensive lives here:

  1. index build (kopia walk + DuckDB writes)
  2. per-snapshot analysis, including the heavy reclaimable pass
  3. dashboard rebuild (the global content refcount)
  4. churn rollup for the default window
  5. per-source policy refresh

Why a subprocess: CPython does not return large freed heaps to Windows, and DuckDB's buffer pool
never shrinks, so doing this work in the long-lived app process left several GB resident until
Windows forced it out. A process that exits gives every byte back, guaranteed.

There is no exclusive lock to hand over any more: the index is immutable Parquet published under
``publish.WriterLock``, and the app keeps serving the previous generation while this runs.
Progress is written to ``data/index_progress_repo<N>.json`` so the app can show a live badge.

Prints a JSON summary to stdout and exits 0; nonzero on failure.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import sys
import threading
import time

# Support being run directly (not via -m) too, mirroring app/backblaze/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_PROGRESS_EVERY = 2.0


def progress_path(repo: str) -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, f"index_progress_repo{repo}.json")


def write_progress(repo: str, payload: dict) -> None:
    """Atomic write so the app never reads a half-written progress file."""
    path = progress_path(repo)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - progress reporting must never affect the real work
        pass


def clear_progress(repo: str) -> None:
    try:
        os.remove(progress_path(repo))
    except Exception:  # noqa: BLE001
        pass


class _Reporter:
    """Mirrors the builder's in-process job dict out to a file every couple of seconds, so the
    app (which cannot touch the DuckDB right now) can still render a live progress badge."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.stage = "starting"
        self.started = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def snapshot(self) -> dict:
        from .builder import _JOBS
        job = _JOBS.get(self.repo, {})
        return {
            "repo": self.repo, "stage": self.stage, "started": self.started,
            "updated": time.time(), "pid": os.getpid(),
            "indexed": job.get("indexed", 0), "total": job.get("total", 0),
            "targets": job.get("targets"), "current_source": job.get("current_source"),
            "error": job.get("error"),
        }

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        logging.info("stage: %s", stage)
        write_progress(self.repo, self.snapshot())

    def _loop(self) -> None:
        while not self._stop.wait(_PROGRESS_EVERY):
            write_progress(self.repo, self.snapshot())

    def start(self) -> None:
        write_progress(self.repo, self.snapshot())
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def index_stage(repo: str, full: bool = False) -> None:
    """Produce and publish this repo's index generation.

    The worker is the ONLY writer: artifacts are immutable and the manifest swap is the single
    atomic switch, so a second publisher would just race this one. `build_repo` decides for itself
    whether a source needs rebuilding by reading the existing manifest, which is why there is no
    separate incremental entry point.

    The job dict is registered in `builder._JOBS` because that is where `_Reporter` reads progress
    from - unchanged plumbing, new producer. Module level, not a closure inside `run`, so tests can
    replace it the same way they replace every other stage.
    """
    from . import artifacts, publish, store
    from .builder import _JOBS

    root = store.repo_dir(repo)
    manifest = store.read_manifest(repo)
    gen = int((manifest or {}).get("generation") or 0) + 1
    job: dict = {}
    _JOBS[repo] = job
    with publish.WriterLock(repo):
        body = artifacts.build_repo(repo, root, gen=gen, job=job, full=full)
        publish.publish(root, body)
    logging.info("published generation %s: %s", gen, body.get("stats"))


def run(repo: str, full: bool = False) -> dict:
    """The nightly pass for one repo.

    STAGE ORDER MATTERS. The dashboard stage is what rebuilds the global content refcount, and the
    reclaimable analysis READS that refcount to decide what is safe to delete. Running analysis
    first would price tonight's reclaimable numbers against yesterday's refcount and then leave
    them that way - the "always refresh the dashboard before analyzing" rule, in code.

    Stages after the index are otherwise independent: a failed churn rollup should not cost us the
    index. But if the INDEX itself fails, everything downstream would be computed over a
    half-updated index, so those are skipped rather than cached wrong."""
    rep = _Reporter(repo)
    rep.start()
    summary: dict = {"repo": repo, "ok": True, "full": full, "stages": {},
                     "errors": {}, "skipped": []}
    started = time.time()

    def stage(name: str, fn) -> bool:
        rep.set_stage(name)
        t0 = time.time()
        try:
            fn()
            summary["stages"][name] = round(time.time() - t0, 1)
            return True
        except Exception as exc:  # noqa: BLE001 - one stage failing must not abort the rest
            summary["ok"] = False
            summary["errors"][name] = str(exc)[:500]
            logging.exception("stage %s failed", name)
            return False

    try:
        def _dashboard() -> None:
            from ..core.metrics import table_rows
            table_rows(repo, refresh=True, mode="all")   # rebuilds content_refs

        def _analysis() -> None:
            from .snapanalysis import prewarm
            out = prewarm(repo, with_reclaim=True)       # reads content_refs
            if out and out.get("failed_sources"):
                # the stage still SUCCEEDS - partial results are published; the failures ride
                # the summary so a nightly that silently lost sources becomes visible
                summary["analysis_failed_sources"] = out["failed_sources"]
                logging.warning("repo=%s analysis failed for %d source(s): %s", repo,
                                len(out["failed_sources"]), ", ".join(out["failed_sources"][:10]))

        def _churn() -> None:
            # The churn windows are BOUNDED (365d completes in ~12s on a clean pool), but by now
            # index/dashboard/analysis have filled DuckDB's buffer pool, so whichever window runs
            # next OOMs nondeterministically. A cursor off the pooled connection shares that pool -
            # only closing the connection yields an empty one. Safe here: this process owns the
            # repo for the whole run and nothing is in flight between stages.
            from .store import close_all
            close_all()
            gc.collect()
            from .churnview import prewarm as churn_prewarm
            churn_prewarm(repo)

        def _folders() -> None:
            from .foldercache import prewarm as folder_prewarm
            folder_prewarm(repo)

        def _policies() -> None:
            from ..core import policybackup
            from ..core.policy import refresh_policies
            refresh_policies(repo, force=True)
            # Fail-closed is not the nightly's job: a failed export here is logged loudly and the
            # next nightly retries, while a pre-WRITE backup (the API path) refuses the write.
            try:
                policybackup.nightly_backup(repo)
            except Exception as exc:  # noqa: BLE001 - a backup hiccup must not fail the stage
                logging.error("policy backup repo=%s failed: %s", repo, exc)

        def _policy_prices() -> None:
            #: whole-policy "keeping out" pricing as a STORED stage - the tab reads the
            # numbers instantly instead of fanning out per-source scans per request. Same
            # buffer-pool rationale as churn: everything before this filled the pool.
            from .store import close_all
            close_all()
            gc.collect()
            from ..index import policypricing
            policypricing.precompute(repo)

        def _next_snaps() -> None:
            from ..core.schedule import compute_next_snapshots
            compute_next_snapshots(repo)

        if stage("index", lambda: index_stage(repo, full)):
            # Ordering alone is not the dependency - the dashboard stage has to SUCCEED. It rebuilds
            # the global content refcount, and the stages flagged below price delete-safety numbers
            # against it. Letting them run after a failed rebuild computes tonight's freeable and
            # reclaimable figures from yesterday's refcount and then persists them as if current,
            # which is the exact failure the stage order exists to prevent.
            fresh_refcount = stage("dashboard", _dashboard)
            for name, fn, needs_refcount in (("analysis", _analysis, True),
                                             ("churn", _churn, False),
                                             ("folders", _folders, True),
                                             ("policies", _policies, False),
                                             ("policy_prices", _policy_prices, False),
                                             ("next_snapshots", _next_snaps, False)):
                if needs_refcount and not fresh_refcount:
                    summary["skipped"].append(name)
                    logging.error("skipping %s - the dashboard stage failed, so the content "
                                  "refcount is stale and its numbers would be wrong", name)
                    continue
                stage(name, fn)
        else:
            summary["skipped"] = ["dashboard", "analysis", "churn", "folders", "policies",
                                  "policy_prices", "next_snapshots"]
            logging.error("index stage failed - skipping every stage that reads the index")
    finally:
        rep.stop()
        # The refcount-miss counters are process-local, and MOST freeable lookups happen right here
        # in the worker (the folder prewarm), not in the app. Without this line /debug/memory would
        # report near-zero misses while the measurement's real population went to the grave with
        # this process. Logged rather than persisted - it is a diagnostic, not state.
        try:
            from .physical import refcount_miss_stats
            stats = refcount_miss_stats().get(repo)
            if stats:
                summary["refcount_misses"] = stats
                logging.info("refcount misses repo=%s: %s", repo, stats)
        except Exception:  # noqa: BLE001 - a diagnostic must never affect the run
            pass
        try:
            from ..core.cache import read_failure_stats
            fails = read_failure_stats(repo)
            if fails:
                summary["unreadable_dirs"] = fails
                logging.warning("repo=%s: %d unreadable director(y/ies) skipped (NOT cached, "
                                "retried next build): e.g. %s", repo, fails["count"], fails["sample"])
        except Exception:  # noqa: BLE001 - a diagnostic must never affect the run
            pass
        from .store import close_all
        try:
            close_all()  # release the DuckDB file before we exit so the app can reopen it promptly
        except Exception:  # noqa: BLE001
            pass
        clear_progress(repo)

    summary["elapsed"] = round(time.time() - started, 1)
    return summary


def main(argv: list) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    from ..core.sources import pin_for_run
    pin_for_run()  # one snapshot listing per run; the stages are minutes apart, the TTL is 45s
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(json.dumps({"ok": False, "reason": "usage: worker <repo> [--full]"}))
        return 2
    repo = args[0]
    try:
        summary = run(repo, full="--full" in argv)
    except Exception as exc:  # noqa: BLE001 - always emit parseable output, never crash silently
        print(json.dumps({"ok": False, "repo": repo, "reason": str(exc)[:500]}))
        return 1
    print(json.dumps(summary))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
