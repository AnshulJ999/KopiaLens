"""KopiaLens FastAPI application."""

from __future__ import annotations

import datetime as dt
import gc
import json
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import DATA_DIR, REPOS, repo_kind

from .api import (actions, backblaze, dashboard, excluded, insights, search, simulate,
                  snapshots, stats, tree)
from .core import kopia as kopia_mod
from .core import nightly
from .core.applog import get_logger, setup_logging
from .core.instrument import log_memory, memory_snapshot
from .core.metrics import table_rows
from .core.schedule import compute_next_snapshots
from .core.server import SERVERS
from .index.schema import RepoOffline, offline_status
from .index.schema import close_all as close_duckdb
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
    """``daily`` (default: one heavy pass at a fixed wall-clock time) or ``interval`` (the older
    every-N-seconds behaviour, kept as an option rather than removed)."""
    return "interval" if str(_kl().get("index_mode", "daily")).lower() == "interval" else "daily"


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
        with open(_LAST_CYCLE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        data.setdefault("nightly", {})
        return data
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _LAST_CYCLE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _LAST_CYCLE_FILE)
    except Exception as exc:  # noqa: BLE001 - never fail the bg loop on housekeeping
        log.warning("bg worker state write failed: %s", exc)


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
    """When this repo's heavy pass last STARTED. Seeded from the legacy global ``ts`` the first
    time round, so upgrading does not look like "never indexed" and fire an immediate catch-up."""
    data = _read_state()
    val = (data.get("nightly") or {}).get(repo)
    if val is None:
        val = data.get("ts") or 0
    try:
        return float(val)
    except Exception:
        return 0.0


def _mark_nightly(repo: str) -> None:
    """Stamp the attempt at START, not on success: a build that fails should be retried by the
    user (or tomorrow), not re-launched every 60 seconds for the rest of the night."""
    data = _read_state()
    data.setdefault("nightly", {})[repo] = time.time()
    data["ts"] = time.time()
    _write_state(data)


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
        data = table_rows(repo, refresh=False, mode="all")
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
        from .api.stats import refresh_stats
        refresh_stats(repo)
    except Exception as exc:  # noqa: BLE001
        log.error("warm stats repo=%s failed: %s", repo, exc)
    try:
        from .index.queries import warm_landing
        warm_landing(repo)
    except Exception as exc:  # noqa: BLE001
        log.error("warm search-landing repo=%s failed: %s", repo, exc)
    if not _dashboard_nightly_only():  # opt-in: rebuild the dashboard during the day as well
        now = time.time()
        if now - _last_dashboard.get(repo, 0.0) >= _dashboard_interval():
            _last_dashboard[repo] = now
            _refresh_dashboard(repo)


def _light_pass() -> None:
    """One light warm pass over every kopia repo that is currently available."""
    for repo in REPOS:
        if _bg_stop.is_set():
            return
        if repo_kind(repo) == "backblaze":
            continue  # BZ has no cheap warm step - its refresh is heavy and rides with the nightly pass
        if nightly.is_running(repo) or offline_status(repo):
            continue  # the worker owns the DuckDB right now
        try:
            _warm(repo)
        except RepoOffline:
            continue  # raced with the start of a nightly pass; nothing to do
        except Exception as exc:  # noqa: BLE001
            log.error("light pass repo=%s failed: %s", repo, exc)


def _index_due(repo: str, now_dt: dt.datetime, now_ts: float) -> bool:
    """Daily mode: due when the most recent scheduled time has passed and this repo has not run
    since. That single comparison also GIVES us catch-up for free - if the PC was off at 23:00,
    the scheduled time is still in the past on the next boot, so the pass runs then."""
    last = nightly_ts(repo)
    if _index_mode() == "interval":
        return now_ts - last >= _index_interval()
    hh, mm = _index_time()
    today = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    due = today if today <= now_dt else today - dt.timedelta(days=1)
    return last < due.timestamp()


def _tick() -> None:
    """One scheduler heartbeat: start at most ONE heavy pass, then do the light work when due."""
    global _last_light
    now_ts = time.time()
    now_dt = dt.datetime.now()
    if not nightly.any_running():
        for repo in REPOS:
            if _bg_stop.is_set():
                return
            if _index_due(repo, now_dt, now_ts):
                log.info("heavy pass due for repo=%s (mode=%s)", repo, _index_mode())
                _mark_nightly(repo)                 # stamp the attempt before launching, not after
                nightly.start_index_pass_async(repo)
                break  # strictly sequential - the next repo gets its turn on a later tick
    if now_ts - _last_light >= _light_interval():
        _last_light = now_ts
        _light_pass()
        gc.collect()  # return the pass's transient arenas (pymalloc will not on its own)
        log_memory("light pass done")


def _bg_worker() -> None:
    """Scheduler thread. Ticks once a minute so the wall-clock check is accurate, but only does
    work when something is actually due: at most one heavy pass at a time (delegated to a worker
    subprocess), plus a cheap warm pass on its own interval."""
    global _last_light
    wait = _startup_wait()
    log.info("bg worker: waiting %.0fs before the first pass", wait)
    if _bg_stop.wait(wait):
        return  # shutdown requested during the startup delay
    _last_light = time.time()
    _light_pass()
    gc.collect()
    _write_last_cycle_ts()
    log_memory("startup light pass done")
    while not _bg_stop.wait(_TICK):
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything a pass throws
            log.error("bg tick failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    log.info("KopiaLens starting up")
    try:  # apply the configurable in-process dir-cache RAM budget (default 1.5 GB)
        from .core import cache as _cache
        mb = float(load_settings().get("kopialens", {}).get("dir_memo_max_mb", 1500))
        _cache.DIR_MEMO_MAX_BYTES = int(mb * 1024 * 1024)
        log.info("DIR_MEMO_MAX_BYTES=%d (%.0f MB)", _cache.DIR_MEMO_MAX_BYTES, mb)
    except Exception as exc:  # noqa: BLE001 - never block startup on a tuning read
        log.warning("dir_memo_max_mb setting read failed: %s", exc)
    try:  # point the read-only Backblaze backend at the configured bzdata root
        from .backblaze import paths as bz_paths
        bz_paths.set_bzdata_root(load_settings().get("backblaze", {}).get("bzdata_path")
                                 or bz_paths.bzdata_root())
    except Exception as exc:  # noqa: BLE001
        log.error("BZ bzdata path setup failed: %s", exc)
    if load_settings().get("listing_backend") == "server":
        SERVERS.warmup()  # only when explicitly opted into the persistent-server backend
    threading.Thread(target=_bg_worker, daemon=True).start()
    yield
    log.info("KopiaLens shutting down")
    _bg_stop.set()
    SERVERS.shutdown()
    close_duckdb()
    try:
        from .backblaze import bbdb as bz_bbdb
        bz_bbdb.close()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="KopiaLens", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    calls0, ktime0 = kopia_mod.KOPIA_CALLS, kopia_mod.KOPIA_TIME
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        ms = (time.perf_counter() - start) * 1000
        kc = kopia_mod.KOPIA_CALLS - calls0
        kms = (kopia_mod.KOPIA_TIME - ktime0) * 1000
        log.info(
            "%s %s -> %s  %.0fms  kopia_subproc=%d/%.0fms",
            request.method, request.url.path, response.status_code, ms, kc, kms,
        )
    return response


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


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Log the FULL traceback to kopialens.log and return JSON (never a plain-text 500). Fixes the
    frontend 'Unexpected token I' crash AND finally records what actually threw. HTTPException has its
    own handler and never reaches here."""
    log.error("UNHANDLED %s %s: %s\n%s", request.method, request.url.path, exc, traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


app.include_router(dashboard.router, prefix="/api")
app.include_router(snapshots.router, prefix="/api")
app.include_router(tree.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(simulate.router, prefix="/api")
app.include_router(actions.router, prefix="/api")
app.include_router(stats.router, prefix="/api")
app.include_router(excluded.router, prefix="/api")
app.include_router(insights.router, prefix="/api")
app.include_router(backblaze.router)  # router already carries prefix="/api/backblaze"

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/debug/memory")
def debug_memory() -> dict:
    """On-demand RSS + cache/table sizes (the same snapshot the background worker logs each cycle)."""
    return memory_snapshot()
