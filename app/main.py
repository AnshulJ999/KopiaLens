"""KopiaLens FastAPI application."""

from __future__ import annotations

import asyncio
import datetime as dt
import gc
import ipaddress
import json
import os
import re
import threading
import time
import traceback
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
from fastapi import Depends, FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from config import repo_record as config_repo_record
from config import API_VERSION, APP_VERSION, DATA_DIR, DEMO_BACKBLAZE, DEMO_INSTANCE, REPOS, is_demo, repo_kind

from .api import (access, account, actions, appconfig, audit, backblaze, cli, dashboard, deleteplan, encryption, engine, excluded,
                  help, insights, jobs, licence, logs, newsnapshot, notify, onboarding, policies, prune, restore, search, setup, shell, simulate,
                  snapshots, stats, tree, writemode)
from .api import tokens as tokens_api
from .core import auth, basepath, checkin, devaddons, devnotes, netaccess, pause, tokens
from .core import engine as backup_engine
from .core import jobs as jobrunner
from .index.crypto import StoreLocked
from .index import crypto as crypto_mod
from .core import kopia as kopia_mod
from .core import nightly
from .core import repos as repos_store
from .core.applog import RequestStats, apply_logging_settings, debug_on, get_logger, setup_logging
from .core.instrument import log_memory, memory_snapshot
from .core.metrics import table_rows
from .core.schedule import compute_next_snapshots
from .core.server import SERVERS
from .core.sources import ListingFailed
from .index import lanes, store, upgrade as index_upgrade
from .index.schema import RepoOffline, offline_status
from .index.schema import close_all as close_duckdb
from .index.upgrade import IndexVersionMismatch
from .settings import load_settings

log = get_logger("app")

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

_bg_stop = threading.Event()
_TICK = 60.0                       # scheduler heartbeat - the clock check must be finer than the work interval
_DEFAULT_REINDEX_INTERVAL = 1800   # fallback seconds between background passes (overridable in settings)
_DEFAULT_LIGHT_INTERVAL = 1800     # cheap warm pass: next-snapshot times, stats, search landing
_DEFAULT_INDEX_INTERVAL = 86400    # used only when index_mode == "interval"
_DEFAULT_INDEX_TIME = "23:00"      # daily mode: local wall-clock time for the heavy pass
_DEFAULT_DASHBOARD_INTERVAL = 14400
_DEFAULT_RECLAIM_INTERVAL = 10800  # fallback seconds between reclaimable pre-warm passes (heavy)
_DEFAULT_STARTUP_DELAY = 300       # fallback quiet period after every process start, before the first bg pass
_last_reclaim: dict[str, float] = {}
_last_light = 0.0
_last_dashboard: dict[str, float] = {}
_LAST_CYCLE_FILE = os.path.join(DATA_DIR, "bg_worker_state.json")


def _kl() -> dict:
    try:
        return load_settings().get("kopialens", {}) or {}
    except Exception:
        return {}


def _reindex_interval() -> float:
    """Background-pass interval, read live from settings so a change takes effect without restart."""
    try:
        val = float(_kl().get("reindex_interval", _DEFAULT_REINDEX_INTERVAL))
        return max(60.0, val)  # never tighter than 1 minute
    except Exception:
        return _DEFAULT_REINDEX_INTERVAL


def _index_mode() -> str:
    """``daily`` (default: one heavy pass at a fixed wall-clock time), ``interval`` (every N
    seconds) or ``off`` (only when the user starts it)."""
    mode = str(_kl().get("index_mode", "daily")).lower()
    return mode if mode in ("interval", "off") else "daily"


def _index_time() -> tuple[int, int]:
    raw = str(_kl().get("index_time", _DEFAULT_INDEX_TIME))
    try:
        hh, _, mm = raw.partition(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm or 0)))
    except Exception:
        return 23, 0


def _index_interval() -> float:
    try:
        return max(300.0, float(_kl().get("index_interval", _DEFAULT_INDEX_INTERVAL)))
    except Exception:
        return _DEFAULT_INDEX_INTERVAL


def _light_interval() -> float:
    try:
        return max(60.0, float(_kl().get("light_interval", _DEFAULT_LIGHT_INTERVAL)))
    except Exception:
        return _DEFAULT_LIGHT_INTERVAL


def _dashboard_nightly_only() -> bool:
    """Default: the global-refcount dashboard rebuild is an ~80s job, so it rides with the nightly
    pass. Set ``dashboard_mode`` to ``interval`` to get the old refresh-during-the-day behaviour."""
    return str(_kl().get("dashboard_mode", "nightly")).lower() != "interval"


def _dashboard_interval() -> float:
    try:
        return max(300.0, float(_kl().get("dashboard_interval", _DEFAULT_DASHBOARD_INTERVAL)))
    except Exception:
        return _DEFAULT_DASHBOARD_INTERVAL


def _startup_delay() -> float:
    """Flat minimum quiet period after every process start (boot or manual restart) before the
    first background pass - so a boot never triggers the kopia-subprocess + DuckDB burst while
    Windows/other startup apps are still settling. Read live from settings."""
    try:
        val = float(load_settings().get("kopialens", {}).get("startup_delay", _DEFAULT_STARTUP_DELAY))
        return max(0.0, val)
    except Exception:
        return _DEFAULT_STARTUP_DELAY


def _read_state() -> dict:
    """``{"ts": float, "nightly": {repo: float}}``. Tolerates the older ts-only file shape."""
    try:
        from .core import filecrypto
        data = filecrypto.read_json(_LAST_CYCLE_FILE)
        if not isinstance(data, dict):
            return {}
        data.setdefault("nightly", {})
        return data
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    from .core import filecrypto
    if not filecrypto.write_json(_LAST_CYCLE_FILE, data):
        log.warning("bg worker state write failed")


def _read_last_cycle_ts() -> float | None:
    try:
        return float(_read_state()["ts"])
    except Exception:
        return None


def _write_last_cycle_ts() -> None:
    data = _read_state()
    data["ts"] = time.time()
    _write_state(data)


def nightly_ts(repo: str) -> float:
    """When this repo's heavy pass last STARTED. Used only to decide whether a slot has been
    attempted - NOT for display. NO FALLBACK, deliberately (mirrors nightly_success_ts() below): a
    repo with no attempt recorded reads as 0 ("never"), which is already the correct "due" answer.

    A shared ``ts`` fallback used to live here to avoid an "upgrade looks like never indexed" catch-up
    storm. But `_mark_nightly()` bumps that same ``ts`` field on EVERY repo's attempt, not just once -
    so the moment repo1 started each night, repo2/bz (which have no ``nightly`` entry of their own)
    read that bumped ``ts`` as "already attempted this slot" and were silently starved forever, since
    REPOS is iterated in a fixed order and repo1 always claims the slot first. Confirmed live 2026-08-21:
    repo1 ran; repo2/bz never got a `nightly`/`attempts`/`success` entry at all, that night or any
    future night, until this fix."""
    data = _read_state()
    val = (data.get("nightly") or {}).get(repo)
    try:
        return float(val or 0)
    except Exception:
        return 0.0


def _repo_created(repo: str) -> float:
    import config
    rec = next((r for r in config.RECORDS.get("repos", []) if str(r.get("id")) == str(repo)), {})
    try:
        return float(rec.get("created") or 0)
    except (TypeError, ValueError):
        return 0.0


def nightly_success_ts(repo: str) -> float:
    """When this repo was last indexed SUCCESSFULLY, or 0 for "never".

    NO FALLBACK, deliberately. This used to fall through to the attempt time and then to the global
    cycle timestamp, which made the freshness strip read "8m ago" for three repos whose last real
    index was 18 hours earlier - all three showing the same time, because they were all showing the
    background cycle. `ts` is rewritten on every app start, so the displayed age got YOUNGER on each
    restart while the index got older: a repo that had been failing for a week would still read as
    minutes old. That is the one thing a staleness indicator must never do.

    `nightly_ts()` also has no fallback now (2026-08-22 fix): a repo with no attempt on record just
    reads as due and takes its turn in the queue. That is not the catch-up "storm" the old seeding
    was guarding against - passes are already serialized one at a time (`_reserve`) - so there is
    nothing left to seed. The old shared-`ts` fallback there was actively harmful, not just
    cosmetic: it let repo1 starting first each night falsely mark repo2/bz as "already attempted",
    so they never ran, every night, permanently. See `nightly_ts()` for the full story.
    """
    data = _read_state()
    val = (data.get("success") or {}).get(repo)
    if val is None:
        return 0.0   # the UI renders 0 as "never" and treats it as stale; both are correct here
    try:
        return float(val)
    except Exception:
        return 0.0


def _attempt_record(repo: str) -> dict:
    return (_read_state().get("attempts") or {}).get(repo) or {}


def _mark_nightly(repo: str, slot: float) -> None:
    """Stamp the attempt at START, not on success, so a failing worker is not relaunched every 60
    seconds for the rest of the night. ``slot`` identifies which scheduled run this is for, so a
    retry can be told apart from the next day's run."""
    now = time.time()
    data = _read_state()
    prev = (data.get("attempts") or {}).get(repo) or {}
    retries = int(prev.get("retries", 0)) + 1 if prev.get("slot") == slot else 0
    data.setdefault("attempts", {})[repo] = {"slot": slot, "at": now, "retries": retries, "ok": None}
    data.setdefault("nightly", {})[repo] = now
    data["ts"] = now
    _write_state(data)


def mark_index_stopped(repo: str) -> None:
    """ Record a DELIBERATE stop for the current slot so the retry never brings it back.

    A stopped worker exits non-zero, which is indistinguishable from a crash - and the retry arm
    exists exactly for crashes. The error text is replaced too: "stopped by you" is not a failure
    to investigate, and the freshness strip reads this reason."""
    data = _read_state()
    rec = (data.get("attempts") or {}).get(repo) or {}
    rec["stopped"] = True
    data.setdefault("attempts", {})[repo] = rec
    data.setdefault("errors", {})[repo] = {"at": time.time(),
                                           "reason": "Stopped by you. It will not restart on its own."}
    data["ts"] = time.time()
    _write_state(data)


def mark_nightly_result(repo: str, ok: bool, reason: str | None = None) -> None:
    """Record the outcome once the pass finishes. Success advances the freshness timestamp;
    failure leaves it alone and lets the retry window open.

    The failure REASON is persisted, not just held in memory: the in-process ``nightly.state()``
    copy dies with a restart, and a run that failed days ago is exactly when the reason still
    needs to be on screen."""
    data = _read_state()
    rec = (data.get("attempts") or {}).get(repo) or {}
    rec["ok"] = bool(ok)
    rec["finished"] = time.time()
    data.setdefault("attempts", {})[repo] = rec
    errors = data.setdefault("errors", {})
    if ok:
        data.setdefault("success", {})[repo] = time.time()
        errors.pop(repo, None)
    else:
        errors[repo] = {"at": time.time(), "reason": (reason or "index pass failed")[:500]}
    _write_state(data)
    try:            # a recovery inside the worker is alerted from here
        from .index import versions
        versions.alert_pending()
    except Exception as exc:  # noqa: BLE001 - an alert must never affect the run record
        log.warning("index notice alert failed: %s", exc)


def _close_stale_attempts(adopted: list[str]) -> None:
    """Startup: an attempt still marked in progress has no pass behind it unless its worker was
    just taken over. Record it as interrupted so the retry window opens."""
    for repo, rec in list((_read_state().get("attempts") or {}).items()):
        if rec.get("ok") is None and repo not in adopted:
            log.warning("repo=%s last index attempt never finished; recorded as interrupted", repo)
            mark_nightly_result(repo, False, nightly.INTERRUPTED)


def nightly_error(repo: str) -> dict:
    """The last recorded failure for this repo ({} once a pass succeeds)."""
    return (_read_state().get("errors") or {}).get(repo) or {}


def _retry_delay() -> float:
    try:
        return max(60.0, float(_kl().get("index_retry_delay", 900)))
    except Exception:
        return 900.0


def _max_retries() -> int:
    try:
        return max(0, int(_kl().get("index_max_retries", 1)))
    except Exception:
        return 1


def _startup_wait() -> float:
    """Seconds to wait before the FIRST bg pass after this process started: at least
    startup_delay (flat boot/restart quiet period), and never less than whatever remains of
    reindex_interval since the last completed cycle - so a restart soon after a real cycle
    doesn't re-trigger one early just because the process happened to restart."""
    if _index_mode() == "daily":
        return _startup_delay()  # the clock decides when the heavy pass runs; nothing to defer to
    last = _read_last_cycle_ts()
    if last is None:
        return _startup_delay()
    remaining = _reindex_interval() - (time.time() - last)
    return max(_startup_delay(), remaining)


def _reclaim_interval() -> float:
    """How often to pre-warm per-snapshot reclaimable (heavy op), read live from settings."""
    try:
        val = float(load_settings().get("kopialens", {}).get("reclaim_interval", _DEFAULT_RECLAIM_INTERVAL))
        return max(300.0, val)  # never tighter than 5 minutes (it is expensive)
    except Exception:
        return _DEFAULT_RECLAIM_INTERVAL


# _maybe_warm_reclaim / _maybe_compact used to live here. The reclaimable pre-warm now runs inside
# the index worker subprocess (app/index/worker.py) and compaction inside core/nightly.py, right
# after the worker hands the DuckDB back - both are heavy, and heavy work no longer runs in this
# process at all. _last_reclaim / _reclaim_interval are kept for the settings UI only.


def _refresh_dashboard(repo: str) -> None:
    """Refresh the dashboard cache so the open UI auto-updates. Uses refresh=False (Lever 2): the
    table-cache hash already invalidates when the snapshot set changes, so this recomputes (and
    rewrites content_refs) ONLY when something changed - not every cycle. That removes the biggest
    DuckDB churn source (the content_refs rewrite was happening every 30 min regardless)."""
    try:
        data = table_rows(repo, refresh=False)
        log.info("dashboard refreshed repo=%s in %.1fs", repo, float(data.get("elapsed") or 0))
    except Exception as exc:  # noqa: BLE001
        log.error("dashboard refresh repo=%s failed: %s", repo, exc)


def _warm(repo: str) -> None:
    """LIGHT warm pass, in-process, on the light interval. Everything here is cheap - a couple of
    already-cached kopia calls and one small query. The heavy work (index build, snapshot analysis,
    reclaimable, churn rollup, the global-refcount dashboard rebuild) moved to the nightly worker
    SUBPROCESS, which is what keeps this long-lived process's memory flat."""
    try:
        compute_next_snapshots(repo)
    except Exception as exc:  # noqa: BLE001
        log.error("warm next_snapshots repo=%s failed: %s", repo, exc)
    try:
        from .core import coexist                 # the Backups panel reads it on open
        coexist.maintenance_info(repo)
    except Exception as exc:  # noqa: BLE001
        log.debug("warm maintenance info repo=%s failed: %s", repo, exc)
    try:
        from .api.stats import refresh_stats
        refresh_stats(repo)
    except Exception as exc:  # noqa: BLE001
        log.error("warm stats repo=%s failed: %s", repo, exc)
    try:
        from .index.queries import warm_landing
        kl = load_settings().get("kopialens", {}) or {}
        size = str(kl.get("page_size", 500))   # saved as the picker's text; "Infinite" is not cached
        sort, direction = kl.get("search_sort") or "size", kl.get("search_dir") or "desc"
        warm_landing(repo, limit=int(size) if size.isdigit() else 500, sort=sort, direction=direction)
        from .index.queries import group_by_folder      # the Files + Folders view's folder list
        group_by_folder(repo, depth=int(kl.get("group_depth", 3)), limit=500, direction=direction,
                        sort="logical" if sort == "size" else "physical")
    except Exception as exc:  # noqa: BLE001
        log.error("warm search-landing repo=%s failed: %s", repo, exc)
    if not _dashboard_nightly_only() and not pause.active():  # opt-in daytime rebuild
        now = time.time()
        if now - _last_dashboard.get(repo, 0.0) >= _dashboard_interval():
            _last_dashboard[repo] = now
            _refresh_dashboard(repo)


def _light_pass() -> None:
    """One light warm pass over every kopia repo that is currently available."""
    try:
        from .core.applog import cleanup_jobs
        cleanup_jobs()          # log_cleanup_days; OFF (0) does nothing
    except Exception as exc:  # noqa: BLE001 - housekeeping must never stop the warm pass
        log.warning("log cleanup failed: %s", exc)
    from .core import configbackup
    configbackup.maybe_weekly()   # never raises
    from .index import crypto
    locked = crypto.enabled() and not crypto.unlocked()
    if locked:
        # Every read raises while the store is locked, and the nightly records its own skip.
        # One line here instead of one error per repo every interval.
        log.info("light pass skipped: the index is encrypted and locked")
    for repo in list(REPOS):
        if _bg_stop.is_set():
            return
        if repo_kind(repo) == "backblaze":
            continue  # BZ has no cheap warm step - its detect has its own pass below
        if repos_store.via_server(repo):
            continue  # browse-only: no index to warm
        if locked or nightly.is_running(repo) or offline_status(repo):
            continue  # locked, or the worker owns the DuckDB right now
        try:
            _warm(repo)
        except RepoOffline:
            continue  # raced with the start of a nightly pass; nothing to do
        except Exception as exc:  # noqa: BLE001
            log.error("light pass repo=%s failed: %s", repo, exc)


_DEFAULT_BZ_REINDEX_GAP = 21600   #: min seconds between real BZ reindexes (detect runs far more often)
_BZ_LAST_ATTEMPT = 0.0            # in-memory on purpose: after a restart an immediate retry is correct


def _bz_min_gap() -> float:
    try:
        return max(300.0, float(load_settings().get("backblaze", {}).get("bz_reindex_min_gap",
                                                                         _DEFAULT_BZ_REINDEX_GAP)))
    except Exception:
        return float(_DEFAULT_BZ_REINDEX_GAP)


def _bz_kv_float(key: str) -> float:
    try:
        from .backblaze import bbdb as bz_bbdb
        return float(bz_bbdb.kv_get(bz_bbdb.connect(), key) or 0.0)
    except Exception:  # noqa: BLE001 - a missing/unbuilt BZ DB simply reads as "never"
        return 0.0


def _bz_present() -> bool:
    """A Backblaze record exists and its automatic updates are on. Without one (Linux, Docker, a
    PC without Backblaze) or with updates off, no BZ pass runs; a refresh started by hand still does."""
    if load_settings().get("backblaze", {}).get("updates") == "off":
        return False
    return any(repo_kind(r) == "backblaze" for r in list(REPOS))


def _bz_reindex_due(now_ts: float) -> bool:
    """BZ's own due check () - the daily slot never applies to it. Due when the 30-min detect
    has flagged a change no successful refresh has absorbed yet, the min-gap since the last real
    reindex has opened, and a FAILED attempt has had its retry window (a failing worker must not
    be relaunched every tick)."""
    if DEMO_INSTANCE or not _bz_present() or _bz_kv_float("reindex_pending") != 1.0:
        return False
    try:
        from .backblaze import indexer as bz_indexer
        if bz_indexer.state().get("building"):
            return False   # a refresh (possibly left by the previous app run) is still going
    except Exception:  # noqa: BLE001
        pass
    if now_ts - _bz_kv_float("last_reindex_ts") < _bz_min_gap():
        return False
    return now_ts - _BZ_LAST_ATTEMPT >= _retry_delay()


def _bz_detect_pass() -> None:
    """Cheap BZ freshness detect on the light interval (): stat + body-hash in this process
    (chunked I/O, GIL released) - never the ~4 min reindex, which stays a worker subprocess behind
    the global heavy-pass slot. Skipped while a BZ refresh runs: the worker owns the same SQLite
    file right now and its success re-derives everything the detect would have learned."""
    if DEMO_INSTANCE or not _bz_present():
        return   # a demo never reads this machine's Backblaze data
    try:
        from .backblaze import indexer as bz_indexer
        if bz_indexer.state().get("building"):
            return
        out = bz_indexer.detect_changes()
        if out.get("changed"):
            log.info("BZ detect: data changed (%.1fs) - reindex wanted, gated by bz_reindex_min_gap",
                     float(out.get("elapsed") or 0.0))
    except Exception as exc:  # noqa: BLE001 - the detect must never kill the tick
        log.error("BZ detect failed: %s", exc)


def _bz_transmit_pass() -> None:
    """ Transmit-side refresh on the light interval: the upload-queue table + the
    remaining/totalbackup kv - the Backblaze files that move every ~10 min independent of the
    filelists, so the pill/Queue/dashboard cards cannot go stale for a whole reindex gap.
    refresh_transmit_side() carries its own building guard and never raises."""
    if DEMO_INSTANCE or not _bz_present():
        return
    try:
        from .backblaze import indexer as bz_indexer
        out = bz_indexer.refresh_transmit_side()
        if not out.get("ok") and out.get("reason") != "refresh in flight":
            log.warning("BZ transmit refresh failed: %s", out.get("reason"))
    except Exception as exc:  # noqa: BLE001 - must never kill the tick
        log.error("BZ transmit refresh failed: %s", exc)


def _current_slot(now_dt: dt.datetime) -> float:
    """Daily mode: the scheduled run this moment belongs to, the most recent occurrence of the
    configured time - which is also what gives catch-up for free: if the PC was off at 23:00, that
    occurrence is still in the past on the next boot, so the pass runs then."""
    hh, mm = _index_time()
    today = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    due = today if today <= now_dt else today - dt.timedelta(days=1)
    return due.timestamp()


def _retry_open(rec: dict, now_ts: float) -> bool:
    """A FAILED attempt whose retry window has opened. Without the retry arm, a single crashed
    worker costs a whole slot: exactly what happened on the first night, when the worker could
    not start at all."""
    if rec.get("ok") is not False or rec.get("stopped"):
        return False       # still running (ok is None), succeeded, or a deliberate stop
    if int(rec.get("retries", 0)) >= _max_retries():
        return False
    return now_ts - float(rec.get("finished") or rec.get("at") or 0) >= _retry_delay()


def _due_slot(repo: str, now_dt: dt.datetime, now_ts: float) -> float | None:
    """The slot a heavy pass is due for now, or None. A retry keeps the failed attempt's slot, so
    retries are counted per slot. Adding a repo never starts an index the user did not ask for."""
    mode = _index_mode()
    if mode == "off":
        return None
    rec = _attempt_record(repo)
    last = max(nightly_ts(repo), _repo_created(repo))
    if mode == "interval":
        # Every N hours, counted from when the last pass started (or the repo was added).
        if now_ts - last >= _index_interval():
            return now_ts
        slot = rec.get("slot")
        return slot if slot is not None and _retry_open(rec, now_ts) else None
    slot = _current_slot(now_dt)
    if last < slot:
        return slot
    return slot if rec.get("slot") == slot and _retry_open(rec, now_ts) else None


def _index_due(repo: str, now_dt: dt.datetime, now_ts: float) -> bool:
    return _due_slot(repo, now_dt, now_ts) is not None


def _tick() -> None:
    """One scheduler heartbeat: start at most ONE heavy pass, then do the light work when due."""
    global _last_light, _BZ_LAST_ATTEMPT
    now_ts = time.time()
    now_dt = dt.datetime.now()
    # User actions outrank indexing: while a user-started maintenance runs (hours, exclusive
    # kopia lock), the nightly waits for a later tick instead of contending with it. A pause
    # holds every scheduled run; a slot that came due meanwhile runs once it ends.
    if not nightly.any_running() and not jobrunner.maintenance_running() and not pause.active():
        for repo in list(REPOS):
            if _bg_stop.is_set():
                return
            if repo_kind(repo) == "backblaze":
                continue  # BZ left the daily slot () - its detect-driven check runs below
            if repos_store.via_server(repo):
                continue  # browse-only: kopia refuses the reads an index needs
            if is_demo(repo):
                continue  # a demo copy has no kopia to index from
            slot = _due_slot(repo, now_dt, now_ts)
            if slot is not None:
                log.info("heavy pass due for repo=%s (mode=%s, slot=%s)", repo, _index_mode(), int(slot))
                # Launch FIRST, record second. Recording before reserving meant a manual Index
                # winning the race left the slot marked attempted but never completed, so that
                # night's run was silently lost.
                started = nightly.start_index_pass_async(repo, on_done=mark_nightly_result,
                                                         trigger="nightly")
                if started.get("started"):
                    _mark_nightly(repo, slot)
                else:
                    log.info("heavy pass for repo=%s deferred: %s", repo, started.get("reason"))
                break  # strictly sequential - the next repo gets its turn on a later tick
        if _bz_reindex_due(now_ts):
            # Same single global slot as every heavy pass: a kopia run defers this to a later
            # tick, and a failed refresh retries after index_retry_delay, not every minute.
            started = nightly.start_index_pass_async("bz", on_done=mark_nightly_result)
            if started.get("started"):
                _BZ_LAST_ATTEMPT = now_ts
                log.info("BZ reindex started (detect flagged a change, min-gap opened)")
            else:
                log.info("BZ reindex deferred: %s", started.get("reason"))
    index_upgrade.drain_queue()   # an index a user cannot read must not wait for its next slot
    if now_ts - _last_light >= _light_interval():
        _last_light = now_ts
        _light_pass()
        _bz_detect_pass()
        _bz_transmit_pass()
        gc.collect()  # return the pass's transient arenas (pymalloc will not on its own)
        log_memory("light pass done", debug=True)


def _bg_worker() -> None:
    """Scheduler thread. Ticks once a minute so the wall-clock check is accurate, but only does
    work when something is actually due: at most one heavy pass at a time (delegated to a worker
    subprocess), plus a cheap warm pass on its own interval."""
    global _last_light
    wait = _startup_wait()
    log.debug("bg worker: waiting %.0fs before the first pass", wait)
    if _bg_stop.wait(wait):
        return  # shutdown requested during the startup delay
    _last_light = time.time()
    _light_pass()
    _bz_detect_pass()  # BZ catch-up: after the PC was off, the first detect flags any missed change
    _bz_transmit_pass()  #: the pill/queue/dashboard cards are fresh from the first tick
    gc.collect()
    _write_last_cycle_ts()
    log_memory("startup light pass done")
    while not _bg_stop.wait(_TICK):
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything a pass throws
            log.error("bg tick failed: %s", exc)


def _mount_cleanup() -> None:
    """Forget mounts that died while the app was down and remove the drive letters they left."""
    from .core import mounts
    mounts.startup_cleanup()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    try:
        apply_logging_settings(load_settings())
    except Exception as exc:  # noqa: BLE001 - a settings problem must not block startup
        log.warning("debug log setting could not be applied: %s", exc)
    log.info("KopiaLens starting up")
    _missing = missing_partials()
    if _missing:
        log.error("INCOMPLETE INSTALL - page files missing: %s. Reinstall KopiaLens.", ", ".join(_missing))
    import config as _config
    from .core import host as _host
    if not _host.data_persistent():
        log.warning("The data folder %s is inside the container, so the index and settings are lost "
                    "when the container is recreated. Mount a volume there.", _config.DATA_DIR)
    if _config.LEGACY_LAYOUT:
        log.warning("Old folder layout: data and settings sit next to the app. Move them into "
                    "one data folder and set KOPIALENS_DATA_DIR.")
        log.debug("old layout data dir: %s", _config.DATA_DIR)
    try:  # config backups: before a new app version's first run, and the weekly copy
        from .core import configbackup
        configbackup.on_startup()
    except Exception as exc:  # noqa: BLE001 - a backup problem must not block startup
        log.warning("config backup at startup failed: %s", exc)
    try:  # the folder's own version stamp + any registered migration step (background)
        from .core import dataupgrade
        dataupgrade.start_background()
    except Exception as exc:  # noqa: BLE001 - an upgrade problem must not block startup
        log.error("data folder version check failed: %s", exc)
    try:  # seed the first account from the environment ONCE - how a provisioned image gets auth
        from .core import users
        seeded = os.environ.get("KOPIALENS_PASSWORD") or ""
        if seeded and not users.has_accounts():
            name = (os.environ.get("KOPIALENS_USERNAME") or "admin").strip()
            users.create(name, auth.hash_password(seeded))
            log.warning("account %r seeded from KOPIALENS_PASSWORD (first boot only - an account "
                        "that already exists is never overwritten)", name)
        auth.mark_started()
        auth.announce_setup()
    except Exception as exc:  # noqa: BLE001 - seeding must never block startup
        log.error("account seeding failed: %s", exc)
    try:  # encrypted install: unlock from the OS keychain if the user chose to remember it
        from .index import crypto
        if crypto.enabled() and not crypto.unlocked() and crypto.unlock_from_keychain():
            log.info("index unlocked from the keychain")
        elif crypto.enabled() and not crypto.unlocked() and crypto.unlock_from_file():
            pass
        elif crypto.enabled() and not crypto.unlocked():
            log.warning("the index is encrypted and locked - reads and the nightly wait for an unlock")
        gone = crypto.sweep_all()   # session databases a crashed process left behind
        if gone:
            log.info("removed %d leftover session database file(s)", gone)
    except Exception as exc:  # noqa: BLE001 - never block startup on the keystore
        log.error("keystore check failed: %s", exc)
    try:
        # Before the scheduler starts: a worker from the previous app run is taken over, not restarted.
        # After the keychain unlock: its result file is encrypted at the Full level.
        resumed = nightly.resume_workers(on_done=mark_nightly_result)
        _close_stale_attempts(resumed["adopted"])
    except Exception as exc:  # noqa: BLE001 - cleanup must never block startup
        log.error("resuming index workers failed: %s", exc)
    try:  # index updates queued after deletes before the last exit
        from .core import reindexq
        reindexq.load()
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("index update queue could not be resumed: %s", exc)
    threading.Thread(target=_mount_cleanup, name="mount-cleanup", daemon=True).start()
    try:  # point the read-only Backblaze backend at the configured bzdata root
        from .backblaze import paths as bz_paths
        bz_paths.set_bzdata_root(load_settings().get("backblaze", {}).get("bzdata_path")
                                 or bz_paths.default_bzdata() or bz_paths.bzdata_root())
    except Exception as exc:  # noqa: BLE001
        log.error("BZ bzdata path setup failed: %s", exc)
    if load_settings().get("listing_backend") == "server":
        SERVERS.warmup()  # only when explicitly opted into the persistent-server backend

    def _capture_ids() -> None:  # a couple of kopia calls - off the startup path
        try:
            from .core import kopiabin
            kopiabin.version()      # one spawn, once: /api/repos must never do it on a poll
        except Exception as exc:  # noqa: BLE001 - a version read is cosmetic
            log.warning("kopia version read failed: %s", exc)
        try:
            from .core.repos import ensure_unique_ids
            ensure_unique_ids()
        except Exception as exc:  # noqa: BLE001 - identity capture never blocks startup
            log.warning("repository unique-id capture failed: %s", exc)

    def _upgrade_indexes() -> None:
        """ Migrate any generation still in an older format, ONCE, off the startup path.

        Before this, a format bump silently rebuilt every repo on its next pass (10-40 min each,
        and the sticky first-seen dates were lost). A generation from a NEWER app is refused here
        and by store.open_generation, so an older KopiaLens can never write into it."""
        try:
            report = index_upgrade.migrate_all_repos(list(REPOS))
            if any(report.values()):
                log.warning("index format pass: %s", report)
        except Exception as exc:  # noqa: BLE001 - never block startup
            log.error("index format migration failed: %s", exc, exc_info=True)

    store.start_idle_reaper()    # nothing stays open: close every generation after the idle window
    threading.Thread(target=_capture_ids, daemon=True).start()
    # After the keychain unlock above: a migration step that rewrites Parquet needs the key.
    threading.Thread(target=_upgrade_indexes, name="index-upgrade", daemon=True).start()
    threading.Thread(target=_bg_worker, daemon=True).start()
    backup_engine.start()        # KopiaLens's kopia servers for the repositories it runs backups for
    try:  # subscription and Patreon licences renew quietly before they end
        from .licensing import licence as _licence
        _licence.start_renewal()
    except Exception as exc:  # noqa: BLE001 - licensing must never block startup
        log.warning("licence renewal not started: %s", exc)
    checkin.start()              # daily update check + usage stats, weekly revoked-licence list
    yield
    log.info("KopiaLens shutting down")
    _bg_stop.set()
    backup_engine.shutdown()     # KopiaLens closed = no scheduled backups from it
    jobrunner.terminate_all()    # a running action must not outlive the registry as an orphan
    if _host.in_docker():
        from .core import mounts as _mounts
        _mounts.unmount_all()    # the container is going away: no dead FUSE mount left on the host
    # A running index worker is left to finish; the next start takes it over (resume_workers).
    SERVERS.shutdown()
    store.close_all()            # the live and retired generations; the index reads come from here
    close_duckdb()               # the scratch/legacy pool, normally already empty
    try:
        from .backblaze import bbdb as bz_bbdb
        bz_bbdb.close()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="KopiaLens", version=APP_VERSION, lifespan=lifespan,
              dependencies=[Depends(writemode.gate)])


if DEMO_INSTANCE:
    @app.middleware("http")
    async def demo_paths_middleware(request: Request, call_next):
        """A demo never shows this PC's own folders: API replies carry stand-ins instead.
        Registered before the gzip wrapper so it sees plain bodies."""
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            return response
        from .core.demoreplies import scrub
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(scrub(body), status_code=response.status_code, headers=headers,
                        media_type=response.media_type)


class _ApiGZip:
    """Compress API replies over 1 KB for browsers on another machine; same-PC browsers and file
    downloads pass through untouched (on loopback compression only costs time)."""

    _SKIP = ("/api/restore/download",)

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        host = (scope.get("client") or ("",))[0] or ""
        local = host == "::1" or host.startswith("127.")
        if path.startswith("/api/") and not path.startswith(self._SKIP) and not local:
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


@app.middleware("http")
async def demo_repo_disk_middleware(request: Request, call_next):
    """A request about a demo repo (the sample data in a normal install) never reads this
    computer's disk: its fake paths can exist here."""
    rid = request.query_params.get("repo")
    if not rid or not is_demo(rid):
        return await call_next(request)
    from .core import disklookup
    token = disklookup.demo_request(True)
    try:
        return await call_next(request)
    finally:
        disklookup.end_demo_request(token)


app.add_middleware(_ApiGZip)


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Standard safe headers. No frame blocking on purpose: Home Assistant shows add-ons inside its
    own page, and sign-in plus the Host check already cover what framing protection would."""
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


def _blocked_page(text: str) -> HTMLResponse:
    from html import escape
    return HTMLResponse("<!doctype html><meta charset=utf-8><title>KopiaLens</title>"
                        '<body style="font:14px system-ui;margin:3rem"><p>' + escape(text) + "</p></body>",
                        status_code=403)


@app.get("/shell/open")
def shell_open(code: str = "", next: str = "/") -> Response:
    """The desktop app's window opens through here: a one-time code becomes the window cookie
    that the "This window only" access mode asks for."""
    token = netaccess.redeem(code)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(basepath.url(target), status_code=303)
    if token:
        resp.set_cookie(netaccess.WINDOW_COOKIE, token, httponly=True, samesite="strict", path="/",
                        secure=auth.request_secure())
    return resp


@app.middleware("http")
async def access_middleware(request: Request, call_next):
    """Host validation, the access mode, then the shared-password gate, in that order.

    Host first: a request whose Host header we do not recognise is rejected before anything else
    looks at it, because that is the DNS-rebinding case and a valid session cookie would not make
    it safe. The address list and the window-only mode come next; sign-in last. Sign-in is a no-op
    when auth is disabled, which is the state for anyone who fronts KopiaLens with their own
    reverse proxy."""
    secure = auth.set_secure(request.url.scheme == "https")
    base = basepath.set_current(basepath.for_request(request.client.host if request.client else None,
                                                     request.headers))
    try:
        return await _access(request, call_next)
    finally:
        basepath.reset(base)
        auth._SECURE.reset(secure)


async def _access(request: Request, call_next):
    path = request.url.path
    client = request.client.host if request.client else None
    # HA ingress forwards the browser's own Host (e.g. homeassistant.local), so it skips the Host check.
    ingress = bool(basepath.from_ingress(client) and request.headers.get(basepath.INGRESS_HEADER))
    if not ingress and not auth.host_allowed(request.headers.get("host", "")):
        return JSONResponse(status_code=421, content={
            "detail": f"Host {request.headers.get('host', '')!r} is not allowed. Add it to "
                      "settings.kopialens.allowed_hosts if you reach KopiaLens by that name."})
    mode = netaccess.STARTED.get("mode") or netaccess.mode()
    if not netaccess.client_allowed(client, mode):
        log.info("refused a request from %s (not in the allowed addresses)", client)
        if path.startswith("/api/"):
            return JSONResponse(status_code=403, content={"detail": "This address may not open KopiaLens."})
        return _blocked_page("This address may not open KopiaLens. Ask whoever runs it to add it in "
                             "Settings > Remote access.")
    secret = tokens.bearer(request.headers.get("authorization"))
    if secret is not None:
        return await _token_access(request, call_next, secret, mode, client)
    if (mode == "window" and path not in ("/shell/open", "/health")
            and not auth.shell_request_ok(path, request.headers.get(auth.SHELL_HEADER), client)
            and not netaccess.window_ok(request.cookies.get(netaccess.WINDOW_COOKIE))):
        if path.startswith("/api/"):
            return JSONResponse(status_code=403, content={"detail": "KopiaLens opens only in its own window."})
        return _blocked_page("KopiaLens is set to open only in its own window. Open it from the "
                             "tray icon, or change this in Settings > Remote access.")
    if ingress:
        # Home Assistant ingress: HA has signed its user in, and only its proxy uses this address.
        name = (request.headers.get("x-remote-user-name") or "").strip()
        token = auth.set_actor(f"{name} (Home Assistant)" if name else "Home Assistant")
        try:
            return await call_next(request)
        finally:
            auth._ACTOR.reset(token)
    if auth.shell_request_ok(path, request.headers.get(auth.SHELL_HEADER),
                             request.client.host if request.client else None):
        token = auth.set_actor("desktop app")
        try:
            return await call_next(request)
        finally:
            auth._ACTOR.reset(token)
    # Setup closed with no protection chosen: the API needs the setup code (entered once, kept in
    # a cookie). Pages and static files carry no data and stay reachable to ask for it.
    if (path.startswith("/api/") and auth.first_run_state()["needs_code"]
            and not auth.setup_gate_exempt(path)
            and not auth.setup_cookie_ok(request.cookies.get(auth.SETUP_COOKIE))):
        return JSONResponse(status_code=403, content={
            "detail": "Setup is closed. Enter the setup code from the KopiaLens log.", "setup_code": True})
    if not auth.enabled() or auth.exempt(path):
        return await call_next(request)
    user = auth.session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is not None:
        token = auth.set_actor(user["username"])
        try:
            return await call_next(request)
        finally:
            auth._ACTOR.reset(token)
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "Not signed in.", "login": "/login"})
    return HTMLResponse(_login_html(), status_code=401)


async def _token_access(request: Request, call_next, secret: str, mode: str, client: str | None):
    """An API token alone decides who the request is (no cookie, no setup code); what it may do is
    checked per route in the write gate. Window-only mode accepts tokens from this computer only."""
    rec = tokens.verify(secret)
    if rec is None:
        return JSONResponse(status_code=401, content={
            "detail": "This API token isn't valid. It may have been revoked, expired or mistyped."})
    try:
        local = ipaddress.ip_address(client or "").is_loopback
    except ValueError:
        local = False
    if mode == "window" and not local:
        return JSONResponse(status_code=403, content={
            "detail": "KopiaLens opens only in its own window, so API tokens work from this computer only."})
    actor = auth.set_actor("token:" + rec["name"])
    using = tokens.use(rec)
    try:
        return await call_next(request)
    finally:
        tokens.reset(using)
        auth._ACTOR.reset(actor)


def _login_html(error: str = "", username: str = "") -> str:
    """The sign-in page; says it will unlock when the index is locked and the password is shared."""
    from .core import users
    from .index import crypto
    try:
        user = users.primary()
        unlocks = bool(user and user.get("unlocks_encryption")
                       and crypto.configured() and not crypto.unlocked())
    except Exception:  # noqa: BLE001 - the sign-in page must always render
        unlocks = False
    return auth.login_page(error, username=username, unlocks=unlocks)


@app.get("/login")
def login_page() -> HTMLResponse:
    return HTMLResponse(_login_html())


async def _form(request: Request) -> dict[str, str]:
    """Parse an urlencoded form body without pulling in python-multipart (not a declared
    dependency, and the login form has no file uploads)."""
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    form = await _form(request)
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    persist = bool(form.get("remember"))
    address = request.client.host if request.client else "?"
    wait = auth.login_delay(address)                      # increasing wait, never a lockout
    if wait:
        return HTMLResponse(_login_html(f"Too many attempts. Try again in {int(wait)} s.", username),
                            status_code=429, headers={"Retry-After": str(int(wait) + 1)})
    user = auth.authenticate(username, password)
    if user is None:
        auth.record_login_failure(address)
        log.warning("failed login attempt from %s", address)
        return HTMLResponse(_login_html("Wrong username or password.", username), status_code=401)
    auth.record_login_success(address)
    from .api.account import unlock_if_linked
    unlock_if_linked(user, password)
    resp = RedirectResponse(basepath.url("/"), status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.issue_session(user, persist), **auth.cookie_kwargs(persist))
    return resp


@app.post("/api/index/stop")
def post_index_stop(repo: str):
    """ Stop this repo's index run AND keep it stopped.

    The worker is terminated, then the slot is closed so the scheduler's retry (a killed worker
    looks like a crash) does not relaunch it 15 minutes later. A later manual Index or the next
    nightly starts normally."""
    out = nightly.stop_index_pass(repo)
    if out.get("stopped"):
        mark_index_stopped(repo)
    return JSONResponse(out)


def _recovery_by_key() -> bool:
    """The recovery key can reset the sign-in password only while that password is also the
    encryption password and a recovery key exists."""
    from .core import users
    from .index import crypto
    try:
        user = users.primary()
        return bool(user and user.get("unlocks_encryption") and crypto.configured()
                    and crypto.has_recovery())
    except Exception:  # noqa: BLE001 - the page must always render
        return False


@app.get("/login/recover")
def recover_page() -> HTMLResponse:
    return HTMLResponse(auth.recover_page(with_key=_recovery_by_key()))


@app.post("/api/auth/recover")
async def auth_recover(request: Request):
    """Forgotten password: the recovery key sets a new shared (sign-in + encryption) password,
    signs this browser in and unlocks the index. Throttled like sign-in."""
    from .core import users
    from .index import crypto
    form = await _form(request)
    key = str(form.get("recovery_key") or "").strip()
    new, confirm = str(form.get("new") or ""), str(form.get("confirm") or "")
    address = request.client.host if request.client else "?"

    def page(error: str, status: int) -> HTMLResponse:
        return HTMLResponse(auth.recover_page(error, with_key=_recovery_by_key()), status_code=status)

    wait = auth.login_delay(address)
    if wait:
        return page(f"Too many attempts. Try again in {int(wait)} s.", 429)
    if not _recovery_by_key():
        return page("", 409)
    if len(new) < auth.MIN_PASSWORD:
        return page(f"Use a password of at least {auth.MIN_PASSWORD} characters.", 400)
    if new != confirm:
        return page("The new passwords don't match.", 400)
    ok, _reason = crypto.reset_password_with_recovery(key, new)
    if not ok:
        auth.record_login_failure(address)
        log.warning("failed password recovery attempt from %s", address)
        return page("That recovery key didn't work. Check it and try again.", 401)
    auth.record_login_success(address)
    user = users.set_password_hash(users.primary()["username"], auth.hash_password(new))
    log.warning("password reset with the recovery key from %s", address)
    from .api.account import unlock_if_linked
    unlock_if_linked(user, new)
    resp = RedirectResponse(basepath.url("/"), status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.issue_session(user, True), **auth.cookie_kwargs(True))
    return resp


@app.post("/api/auth/logout")
def auth_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.middleware("http")
async def generation_pin_middleware(request: Request, call_next):
    """Give every API request a per-repo generation pin scope.

    ``store.read_cursor`` resolves a repo's generation once into the scope on first use and
    reuses it for the rest of the request, so a publish landing mid-request can never answer one
    query from generation N and the next from N+1. The scope is filled lazily by the reads
    themselves - which is what keeps POST actions whose repo arrives in the JSON body covered,
    where reading it out of the query string pinned the wrong repo - and repos a request never
    touches are never opened at all."""
    if not request.url.path.startswith("/api"):
        return await call_next(request)
    if request.url.path not in _POLL_PATHS:
        store.touch()    # real activity only - a polled status endpoint must not defeat idle close
    token = store.begin_pins()
    try:
        return await call_next(request)
    finally:
        store.end_pins(token)


# Endpoints the UI polls every few seconds: one summary line per interval, not one line each.
_POLL_PATHS = frozenset({
    "/api/jobs/list", "/api/jobs/status", "/api/index/freshness", "/api/index/status",
    "/api/backblaze/status", "/api/settings/table", "/api/settings/tables", "/api/repos",
    "/api/index/runs", "/api/logs/app", "/api/audit", "/api/index/pending", "/api/shell/state",
    "/api/engine/status", "/api/engine/sources", "/api/tasks",
})
_SLOW_REQUEST_MS = 3000.0
_request_stats = RequestStats()

# Column fills a screen asks for after it has drawn: background lane (lanes.py).


class LaneMiddleware:
    """Puts each API request in its read lane and stops its index work when the client goes away.

    The only reader of the server's ``receive``: messages are handed on to the app, and an
    ``http.disconnect`` before the response finished cancels the request's scope."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api"):
            return await self.app(scope, receive, send)
        # The page marks its column fills (fillJson); the same route asked for by a click stays in front.
        fill = any(k == b"x-kopialens-fill" for k, _ in scope.get("headers") or [])
        lane = lanes.FREE if path in _POLL_PATHS else lanes.BG if fill else lanes.FG
        query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
        live = any(v and v[0].lower() in ("1", "true") for k, v in query.items() if k in ("force", "live", "refresh"))
        sc, token = lanes.open_scope(lane, scope.get("method", ""), path, live)
        queue: asyncio.Queue = asyncio.Queue()
        done = False

        async def pump():
            while True:
                msg = await receive()
                await queue.put(msg)
                if msg["type"] == "http.disconnect":
                    if not done:
                        sc.cancel()
                    return

        async def app_receive():
            msg = await queue.get()
            if msg["type"] == "http.disconnect":
                queue.put_nowait(msg)       # every later read sees the disconnect too
            return msg

        reader = asyncio.create_task(pump())
        try:
            await self.app(scope, app_receive, send)
        finally:
            done = True
            reader.cancel()
            lanes.close_scope(sc, token)


@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    slot = [0, 0.0]                                  # this request's own spawns
    db = [0, 0.0]                                    # this request's index queries
    token = kopia_mod.REQUEST_CALLS.set(slot)
    db_token = store.REQUEST_DB.set(db)
    try:
        response = await call_next(request)
    finally:
        kopia_mod.REQUEST_CALLS.reset(token)
        store.REQUEST_DB.reset(db_token)
    path = request.url.path
    if path.startswith("/api"):
        if response.status_code < 400:
            checkin.note_request(path)
        ms = (time.perf_counter() - start) * 1000
        kc, kms = slot[0], slot[1] * 1000
        args = (request.method, path, response.status_code, ms, db[0], db[1] * 1000, kc, kms)
        line = "%s %s -> %s  %.0fms  db=%d/%.0fms  kopia_subproc=%d/%.0fms"
        if response.status_code >= 500:
            log.warning(line, *args)
        elif response.status_code == 499:
            log.debug("cancelled: " + line, *args)
        elif ms >= _SLOW_REQUEST_MS:
            log.info("slow request: " + line, *args)
        elif path in _POLL_PATHS:
            summary = _request_stats.add(path, ms)
            if summary:
                log.debug(summary)
        else:
            log.debug(line, *args)
        if debug_on():
            response.headers["Server-Timing"] = (
                f"app;dur={ms:.1f}, db;dur={db[1] * 1000:.1f};desc=\"{db[0]} queries\", "
                f"kopia;dur={kms:.1f};desc=\"{kc} calls\"")
    return response


app.add_middleware(LaneMiddleware)     # outermost: the one reader of the request's receive


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """FastAPI's default shape, plus a LOG for 5xx.

    Routes turn a failure into ``HTTPException(500, str(exc))`` at their boundary, so the real
    traceback rides ``exc.__cause__`` - and HTTPException never reaches the handler below, which is
    why those errors vanished from the log. A 4xx is a normal answer and stays quiet."""
    if exc.status_code >= 500:
        cause = exc.__cause__
        log.error("%s %s -> %s: %s\n%s", request.method, request.url.path, exc.status_code,
                  cause or exc.detail,
                  "".join(traceback.format_exception(cause)) if cause else "")
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(lanes.RequestCancelled)
async def _cancelled_handler(_request: Request, _exc: lanes.RequestCancelled):
    """The browser gave up on the request; nobody reads this answer."""
    return JSONResponse(status_code=499, content={"detail": "Cancelled."})


@app.exception_handler(ListingFailed)
async def _listing_failed_handler(_request: Request, exc: ListingFailed):
    """kopia couldn't list the repository: the page shows one notice naming it (getJson)."""
    name = (config_repo_record(exc.repo) or {}).get("name") or exc.repo
    return JSONResponse(status_code=503, content={
        "listing_failed": True, "repo": exc.repo, "name": name, "reason": exc.reason,
        "detail": f"Couldn't list {name}: {exc.reason}"})


@app.exception_handler(FileNotFoundError)
async def _no_generation_handler(_request: Request, exc: FileNotFoundError):
    """A repo that has never been indexed has no manifest, so the store cannot open a generation.

    That is a normal state (fresh install, or a repo added since the last run), not a server fault.
    It reuses the offline response shape because the UI already renders that as "back shortly"
    rather than as an error.
    """
    msg = str(exc)
    if "no index" not in msg:
        raise exc                     # an unrelated missing file is a real 500, not this
    return JSONResponse(
        status_code=503,
        content={"detail": "This repo has not been indexed yet. Run an index to populate it.",
                 "offline": True, "reason": "not indexed"},
    )


@app.exception_handler(RepoOffline)
async def _repo_offline_handler(_request: Request, exc: RepoOffline):
    """A repo whose DuckDB the index worker currently holds. 503 + a human message rather than a
    hang: the whole point of failing fast is that the UI can say "back shortly" instead of freezing.
    Every other repo (and the Backblaze view, which has its own database) is unaffected."""
    since = float(exc.info.get("since") or 0)
    mins = max(0, int((time.time() - since) / 60)) if since else 0
    return JSONResponse(
        status_code=503,
        content={
            "detail": f"Repo {exc.repo} is being indexed (started {mins} min ago). "
                      "It will be back shortly - other repos are unaffected.",
            "offline": True, "repo": exc.repo,
            "reason": exc.info.get("reason", "indexing"), "since": since,
        },
    )


@app.exception_handler(StoreLocked)
def _locked_store_handler(request: Request, exc: StoreLocked) -> JSONResponse:
    """Encrypted and locked: one structured answer for the unlock screen, never a raw 500.

    Every index-backed route raises this while the data key is not in memory (a restart with
    "remember" off, or a Docker restart), so the UI can show the unlock prompt instead of a
    wall of red errors. 423 Locked; the detail already names the store.
    """
    return JSONResponse(status_code=423, content={
        "locked": True, "detail": str(exc), "repos": gated_repos(),
        "unlock": "/api/encryption/status"})


def gated_repos() -> list[str]:
    """The repos whose reads are refused right now, for the unlock screen's wording."""
    out = []
    try:
        from .index import store as _store
        for repo in list(REPOS):
            if repo_kind(repo) == "backblaze":
                continue
            if _store.generation_state(repo) and not crypto_mod.unlocked():
                out.append(repo)
    except Exception:  # noqa: BLE001 - a diagnostic for the message only
        pass
    return out


def _index_mismatch_payload(exc: IndexVersionMismatch) -> dict:
    """The 503 body for a generation written by another KopiaLens.

    An OLDER generation is being upgraded - the app started that work itself at startup, so the
    answer is a progress state ("upgrading"), not an error. A NEWER one is a hard stop: nothing can
    be migrated forward, and the way out is the explicit rebuild with this version."""
    upgrading = exc.mode != "newer" and (
        exc.mode == "migrate"
        or exc.repo in index_upgrade.queued_rebuilds()
        or nightly.is_running(exc.repo))
    detail = (f"Repo {exc.repo}: its index is being upgraded for this version of KopiaLens and "
              f"becomes readable when that pass finishes.") if upgrading else str(exc)
    return {"detail": detail, "index_upgrade": True, "upgrading": upgrading, "repo": exc.repo,
            "found": exc.found, "expected": exc.expected, "mode": exc.mode}


@app.exception_handler(IndexVersionMismatch)
async def _index_version_handler(_request: Request, exc: IndexVersionMismatch):
    """Not `offline` (nothing comes back on its own): the message says which way the mismatch runs
    and what actually happens next."""
    return JSONResponse(status_code=503, content=_index_mismatch_payload(exc))


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Log the FULL traceback to kopialens.log and return JSON (never a plain-text 500). Fixes the
    frontend 'Unexpected token I' crash AND finally records what actually threw. HTTPException has its
    own handler and never reaches here."""
    log.error("UNHANDLED %s %s: %s\n%s", request.method, request.url.path, exc, traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


app.include_router(dashboard.router, prefix="/api")
app.include_router(writemode.router, prefix="/api")
app.include_router(snapshots.router, prefix="/api")
app.include_router(tree.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(simulate.router, prefix="/api")
app.include_router(actions.router, prefix="/api")
app.include_router(deleteplan.router, prefix="/api")
app.include_router(prune.router, prefix="/api")
app.include_router(access.router, prefix="/api")
app.include_router(restore.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(engine.router, prefix="/api")
app.include_router(newsnapshot.router, prefix="/api")
app.include_router(cli.router, prefix="/api")
app.include_router(notify.router, prefix="/api")
app.include_router(stats.router, prefix="/api")
app.include_router(audit.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(excluded.router, prefix="/api")
app.include_router(policies.router, prefix="/api")
app.include_router(insights.router, prefix="/api")
app.include_router(encryption.router, prefix="/api")
app.include_router(setup.router, prefix="/api")
app.include_router(account.router, prefix="/api")
app.include_router(onboarding.router, prefix="/api")
app.include_router(appconfig.router, prefix="/api")
app.include_router(shell.router, prefix="/api")
app.include_router(licence.router, prefix="/api")
app.include_router(help.router, prefix="/api")
app.include_router(tokens_api.router, prefix="/api")
if not DEMO_INSTANCE or DEMO_BACKBLAZE:   # a demo reads only its own generated bzdata
    app.include_router(backblaze.router)  # router already carries prefix="/api/backblaze"
devaddons.load(app, ROOT)
if not devaddons.HTML:      # a source run with the Notes add-on keeps its own; a build gets this
    devnotes.load(app, ROOT)

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


def _asset_version() -> str:
    """Stamp derived from the app's own JS and CSS mtimes. Every script and stylesheet tag in
    index.html carries it, so an updated file is always fetched, without a build step and
    without touching the version-pinned vendor filenames."""
    stamp = 0.0
    for sub, ext in (("js", "*.js"), ("css", "*.css")):
        for path in (FRONTEND / sub).rglob(ext):
            try:
                stamp = max(stamp, path.stat().st_mtime)
            except OSError:
                pass
    return str(int(stamp))


# Templates are read from disk per request (auto_reload): the frontend is static, so an edit
# must show up on a page reload without restarting the server.
_JINJA = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(FRONTEND)),
    auto_reload=True,
    autoescape=False,          # the partials ARE the markup; nothing here is user data
)


def _compose_html(client_host: str | None = None) -> str:
    """Render index.html with its ``{% include %}`` partials resolved. The appearance values
    are stamped onto <html> so the page paints in the saved theme before any JS runs, and this
    computer's own formats so the first table already follows them."""
    from .core import host as _host
    info = _host.info(client_host)["formats"]
    os_formats = json.dumps(info).replace("</", "<\\/")
    kl = load_settings().get("kopialens", {})
    _THEMES = ("daylight", "parchment", "slate", "lilac", "kopia", "sky", "teal", "ember",
               "midnight", "obsidian", "lens", "graphite", "ink")
    _DENSITY = ("comfortable", "normal", "compact", "dense", "custom")
    _FONTS = ("inter", "geist", "plex", "system")
    theme = "lens" if kl.get("theme") == "pine" else kl.get("theme")   # Pine was removed
    try:
        pct = min(115, max(80, int(kl.get("density_pct", 100))))
    except (TypeError, ValueError):
        pct = 100
    base = basepath.current()
    html = _JINJA.get_template("index.html").render(
        base=base,
        base_json=json.dumps(base),
        ha_json=json.dumps(basepath.from_ingress(client_host)),
        asset_v=_asset_version(),
        theme=theme if theme in _THEMES else "system",
        density=kl.get("density") if kl.get("density") in _DENSITY else "normal",
        density_pct=pct,
        font=kl.get("font") if kl.get("font") in _FONTS else "inter",
        sortarrows="all" if kl.get("sort_arrows") == "all" else "sorted",
        os_formats=os_formats,
    )
    extra = "\n".join(devaddons.HTML) if devaddons.HTML else devnotes.markup()
    if extra:
        html = html.replace("</body>", extra + "\n</body>", 1)
    return html


_INCLUDE_RE = re.compile(r'\{%-?\s*include\s+"([^"]+)"')

_BROKEN_INSTALL_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>KopiaLens can't start</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 16px;color:#1f2933;background:#fff}}
@media (prefers-color-scheme:dark){{body{{color:#e4e7eb;background:#16191d}}}}code{{font-size:13px;word-break:break-all}}</style>
</head><body><h1>Some KopiaLens files are missing</h1>
<p>This page can't load because the installation is incomplete. Reinstall KopiaLens to fix it. Your settings and index are kept.</p>
<p>Missing: <code>{files}</code></p></body></html>"""


def missing_partials() -> list[str]:
    """Every {% include %} target reachable from index.html that is not on disk."""
    missing: list[str] = []
    seen: set[str] = set()
    todo = ["index.html"]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            text = (FRONTEND / name).read_text(encoding="utf-8")
        except OSError:
            missing.append(name)
            continue
        todo.extend(_INCLUDE_RE.findall(text))
    return sorted(missing)


@app.get("/")
def index(request: Request) -> Response:
    # no-store on the shell itself: a cached index.html would keep pointing at the old asset
    # stamp, which is exactly how stale JS survived a reload before.
    try:
        page = _compose_html(request.client.host if request.client else None)
    except jinja2.TemplateNotFound as exc:
        log.error("page file missing: %s (incomplete install)", exc.name)
        import html as _html
        files = _html.escape(", ".join(missing_partials()) or str(exc.name))
        return HTMLResponse(_BROKEN_INSTALL_PAGE.format(files=files), status_code=500,
                            headers={"Cache-Control": "no-store"})
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health() -> dict:
    """``api`` goes up only when a stable route (docs/api.md) changes incompatibly."""
    return {"ok": True, "version": APP_VERSION, "api": API_VERSION}


@app.get("/debug/memory")
def debug_memory() -> dict:
    """On-demand RSS + cache/table sizes (the same snapshot the background worker logs each cycle)."""
    return memory_snapshot()
