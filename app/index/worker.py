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

An encrypted install hands the data key over on stdin (one JSON line, flagged by
``KOPIALENS_KEY_STDIN``) with the OS keychain as the other carrier; with encryption on and no
key available the run stops with ``skipped`` rather than writing a plaintext generation. A
manual ``python -m app.index.worker`` run has the console on stdin, which is why the read only
happens when the parent flags it.

Prints a JSON summary to stdout and exits 0; nonzero on failure. The same summary is written to
``data/index_result_repo<N>.json`` first: stdout dies with the app that started the worker, and a
restarted app reads the file instead.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import sys
import threading
import time
from ..core import atomicfile

# Support being run directly (not via -m) too, mirroring app/backblaze/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_PROGRESS_EVERY = 2.0


def progress_path(repo: str) -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, f"index_progress_repo{repo}.json")


def detail_path(repo: str) -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, f"index_progress_detail_repo{repo}.json")


def result_path(repo: str) -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, f"index_result_repo{repo}.json")


# Progress fields that can name paths: written to the detail file, which follows the encryption
# level. The status file stays plain so the keyless tray and Stop can always read the pid.
DETAIL_KEYS = ("current_source", "targets", "error")


def write_result(repo: str, payload: dict) -> None:
    try:
        from ..core import filecrypto
        filecrypto.write_json(result_path(repo), payload)
    except Exception:  # noqa: BLE001 - the stdout summary is the primary channel
        pass


def write_progress(repo: str, payload: dict) -> None:
    """Atomic writes so the app never reads a half-written progress file."""
    try:
        status = {k: v for k, v in payload.items() if k not in DETAIL_KEYS}
        path = progress_path(repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status, fh)
        atomicfile.replace(tmp, path)
        from ..core import filecrypto
        filecrypto.write_json(detail_path(repo), {k: payload.get(k) for k in DETAIL_KEYS})
    except Exception:  # noqa: BLE001 - progress reporting must never affect the real work
        pass


def clear_progress(repo: str) -> None:
    for path in (progress_path(repo), detail_path(repo)):
        try:
            os.remove(path)
        except Exception:  # noqa: BLE001
            pass


def _pct(job: dict) -> int | None:
    """An honest 0-100 for the index stage, or None when there is no real denominator.

    Never a fabricated percentage: the walk-cache seed knows its folder count, and a source rebuild
    knows how many sources it has. The tree sync discovers its folders level by level, so it reports
    counters (`sync`) and no percentage - a bar that guessed would be a lie, and the UI is built to
    show the counters instead when `pct` is None.
    """
    seed = job.get("seed") or {}
    if seed.get("total"):
        return min(99, int(100 * int(seed.get("done") or 0) / int(seed["total"])))
    total = int(job.get("total") or 0)
    if total:
        return min(99, int(100 * int(job.get("indexed") or 0) / total))
    return None


def _estimate() -> dict | None:
    from . import perf
    run = perf.active()
    return run.estimate if run is not None else None


class _Reporter:
    """Mirrors the builder's in-process job dict out to a file every couple of seconds, so the
    app (which cannot touch the DuckDB right now) can still render a live progress badge."""

    def __init__(self, repo: str, full: bool = False) -> None:
        self.repo = repo
        self.full = full
        self.stage = "starting"
        self.stage_started = time.time()      # The panel's live stage fraction needs it
        self.started = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def snapshot(self) -> dict:
        from .builder import _JOBS
        job = _JOBS.get(self.repo, {})
        return {
            "repo": self.repo, "stage": self.stage, "started": self.started,
            "stage_started": self.stage_started,
            "updated": time.time(), "pid": os.getpid(),
            "trigger": trigger(), "full": self.full,
            "indexed": job.get("indexed", 0), "total": job.get("total", 0),
            "targets": job.get("targets"), "current_source": job.get("current_source"),
            "error": job.get("error"),
            # Index-stage detail: the phase name, the sync's live counters, the seed's folder
            # count, and a percentage only where a denominator genuinely exists.
            "phase": job.get("phase"), "sync": job.get("sync"), "seed": job.get("seed"),
            "pct": _pct(job),
            "estimate": _estimate(),
        }

    def set_stage(self, stage: str) -> None:
        self.stage_started = time.time()
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
        publish.publish(root, body, keep_version=True)
    from . import perf, resume
    resume.clear_tree(os.path.join(root, "_staging"))     # the saved read is published now
    perf.set_outputs(root, body.get("artifacts") or {})
    logging.info("published generation %s: %s", gen, body.get("stats"))
    # Loud, because it is the one condition that silently under-counts the index. The next build
    # reads the same flag off the manifest and rebuilds every source from the repaired tree.
    unread = ((body.get("_checks") or {}).get("tree") or {}).get("incomplete") or []
    if unread:
        logging.warning("repo=%s: kopia would not read %d object(s) - folders or large files the "
                        "index is INCOMPLETE without, and every source rebuilds next run. "
                        "e.g. %s", repo, len(unread), unread[:5])


def trigger() -> str:
    """What started this run ("nightly" / "manual"), passed by the app for its run history."""
    return os.environ.get("KOPIALENS_INDEX_TRIGGER") or "manual"


def run(repo: str, full: bool = False, skip_index: bool = False) -> dict:
    """The nightly pass for one repo.

    STAGE ORDER MATTERS. The dashboard stage is what rebuilds the global content refcount, and the
    reclaimable analysis READS that refcount to decide what is safe to delete. Running analysis
    first would price tonight's reclaimable numbers against yesterday's refcount and then leave
    them that way - the "always refresh the dashboard before analyzing" rule, in code.

    Stages after the index are otherwise independent: a failed churn rollup should not cost us the
    index. But if the INDEX itself fails, everything downstream would be computed over a
    half-updated index, so those are skipped rather than cached wrong.

    ``skip_index`` runs every stage after the index over the generation already in place (a demo
    copy is written whole, then given its caches this way)."""
    rep = _Reporter(repo, full)
    rep.start()
    from config import DATA_DIR
    from . import perf, store as _store
    perf.start(repo, [os.path.join(_store.repo_dir(repo), "_staging", "tmp"),
                      os.path.join(DATA_DIR, f"duck_tmp_repo{repo}")])
    summary: dict = {"repo": repo, "ok": True, "full": full, "stages": {},
                     "errors": {}, "skipped": []}
    started = time.time()
    from ..core.applog import RunWarnings
    warned = RunWarnings()
    warned_on = warned.attach()

    def stage(name: str, fn) -> bool:
        rep.set_stage(name)
        t0 = time.time()
        try:
            with perf.phase(name):
                fn()
            summary["stages"][name] = round(time.time() - t0, 1)
            return True
        except Exception as exc:  # noqa: BLE001 - one stage failing must not abort the rest
            from .treesync import GaveUpWaiting, StopRequested
            summary["ok"] = False
            if isinstance(exc, GaveUpWaiting):
                summary["errors"][name] = f"{exc} - progress is saved; the next run continues from here"
                logging.warning("stage %s gave up waiting for the repository; progress saved", name)
                return False
            if isinstance(exc, StopRequested):
                summary.update(stopped=True, reason="stopped - the next run continues from here")
                logging.info("stage %s stopped on request; progress saved", name)
                return False
            summary["errors"][name] = str(exc)[:500]
            logging.exception("stage %s failed", name)
            return False

    try:
        def _dashboard() -> None:
            from ..core.metrics import table_rows
            table_rows(repo, refresh=True)   # rebuilds content_refs

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
            from config import is_demo
            if is_demo(repo):
                return   # no real policies to back up; the checks compare against kopia itself
            # Fail-closed is not the nightly's job: a failed export here is logged loudly and the
            # next nightly retries, while a pre-WRITE backup (the API path) refuses the write.
            try:
                policybackup.nightly_backup(repo)
            except Exception as exc:  # noqa: BLE001 - a backup hiccup must not fail the stage
                logging.error("policy backup repo=%s failed: %s", repo, exc)
            try:
                from . import policycheck
                policycheck.run(repo)
            except Exception as exc:  # noqa: BLE001 - the check is a diagnostic
                logging.error("policy inheritance check repo=%s failed: %s", repo, exc)
            try:
                # The policy cache was refreshed moments ago, so the retention replica can be
                # compared against kopia's own retentionReason with no subprocess at all.
                from . import retentioncheck
                retentioncheck.run(repo)
            except Exception as exc:  # noqa: BLE001 - the check is a diagnostic
                logging.error("retention check repo=%s failed: %s", repo, exc)

        def _policy_prices() -> None:
            # Whole-policy "keeping out" pricing as a STORED stage - the tab reads the
            # numbers instantly instead of fanning out per-source scans per request. Same
            # buffer-pool rationale as churn: everything before this filled the pool.
            from .store import close_all
            close_all()
            gc.collect()
            from ..index import policypricing
            policypricing.precompute(repo)

        def _rank_paths() -> None:
            # The exact top lists Search sorts the all-versions columns by.
            from .store import close_all
            close_all()
            gc.collect()
            from . import rankpaths
            rankpaths.precompute(repo)

        def _ignore_files() -> None:
            # Dot-ignore files - discover, read the changed ones, price their rules over
            # every walk that reaches their folder. Zero files on most installs.
            from .store import close_all
            close_all()
            gc.collect()
            from ..index import policypricing
            policypricing.precompute_ignore_files(repo)

        def _next_snaps() -> None:
            from ..core.schedule import compute_next_snapshots
            compute_next_snapshots(repo)

        if skip_index or stage("index", lambda: index_stage(repo, full)):
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
                                             ("ignore_files", _ignore_files, False),
                                             ("rank_paths", _rank_paths, False),
                                             ("next_snapshots", _next_snaps, False)):
                if needs_refcount and not fresh_refcount:
                    summary["skipped"].append(name)
                    logging.error("skipping %s - the dashboard stage failed, so the content "
                                  "refcount is stale and its numbers would be wrong", name)
                    continue
                stage(name, fn)
            from . import versions
            if not skip_index and versions.backup_dir():
                # After the last stage, so the copy holds the whole run's results.
                copied = versions.sync_backup(repo)
                if not copied.get("ok"):
                    summary["backup_copy"] = copied.get("error")
        else:
            summary["skipped"] = ["dashboard", "analysis", "churn", "folders", "policies",
                                  "policy_prices", "ignore_files", "rank_paths",
                                  "next_snapshots"]
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
        try:  # unreadable folders belong in the run record, not only in the log
            from .store import read_manifest
            unread = list((read_manifest(repo) or {}).get("tree_incomplete") or [])
            if unread:
                summary["unreadable_dirs"] = {"count": len(unread), "sample": unread[:5]}
        except Exception:  # noqa: BLE001 - a diagnostic must never affect the run
            pass
        for lg in warned_on:
            lg.removeHandler(warned)
        if warned.count:
            summary["warnings"] = {"count": warned.count, "first": warned.messages}
        from .store import close_all
        try:
            close_all()  # release the DuckDB file before we exit so the app can reopen it promptly
        except Exception:  # noqa: BLE001
            pass
        clear_progress(repo)

    summary["elapsed"] = round(time.time() - started, 1)
    perf.finish({**summary, "trigger": trigger()})
    return summary


def main(argv: list) -> int:
    from ..core.applog import setup_worker_logging
    setup_worker_logging(os.environ.get("KOPIALENS_WORKER_LOG"))
    from .store import mark_worker
    mark_worker()
    from ..core.sources import pin_for_run
    pin_for_run()  # one snapshot listing per run; the stages are minutes apart, the TTL is 45s
    # Run-scoped memo for the refcount join: the analysis and folder stages each ask for it, and
    # the ~1.8s join was paid per call. This process exits after the run, so holding one map costs
    # nothing here; the long-lived app keeps loading per call so its RAM never pins it.
    from . import physical as _physical
    _physical.REFCOUNT_MEMO_ENABLED = True
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(json.dumps({"ok": False, "reason": "usage: worker <repo> [--full]"}))
        return 2
    repo = args[0]
    full = "--full" in argv
    started = time.time()
    from . import crypto
    if not crypto.worker_key(crypto.read_key_payload(os.environ.get("KOPIALENS_KEY_STDIN") == "1")):
        # Nothing indexes in plaintext: with encryption on and no key anywhere, stop before any
        # store connection exists. The app's own pre-flight check means this is the manual run.
        logging.error("the index is encrypted and locked - no data key available, skipping")
        summary = {"ok": False, "skipped": True, "repo": repo,
                   "reason": "the index is encrypted and locked - unlock KopiaLens to run the index"}
        summary.update({"pid": os.getpid(), "finished": time.time(), "trigger": trigger(),
                        "full": full, "elapsed": round(time.time() - started, 1)})
        write_result(repo, summary)
        try:
            print(json.dumps(summary))
        except OSError:
            pass
        return 0
    try:
        summary = run(repo, full=full)
    except Exception as exc:  # noqa: BLE001 - always emit parseable output, never crash silently
        summary = {"ok": False, "repo": repo, "reason": str(exc)[:500],
                   "elapsed": round(time.time() - started, 1)}
    summary.update({"pid": os.getpid(), "finished": time.time(), "trigger": trigger(), "full": full})
    write_result(repo, summary)
    try:
        print(json.dumps(summary))
    except OSError:
        pass  # the app that started us is gone; it reads the result file instead
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
