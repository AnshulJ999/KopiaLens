"""KopiaLens FastAPI application."""

from __future__ import annotations

import gc
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import REPOS, repo_kind

from .api import actions, backblaze, dashboard, excluded, search, simulate, snapshots, stats, tree
from .core import kopia as kopia_mod
from .core.applog import get_logger, setup_logging
from .core.instrument import log_memory, memory_snapshot
from .core.metrics import table_rows
from .core.schedule import compute_next_snapshots
from .core.server import SERVERS
from .index.builder import auto_reindex
from .index.schema import close_all as close_duckdb
from .index.snapanalysis import prewarm as snap_prewarm
from .settings import load_settings

log = get_logger("app")

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

_bg_stop = threading.Event()
_DEFAULT_REINDEX_INTERVAL = 1800   # fallback seconds between background passes (overridable in settings)
_DEFAULT_RECLAIM_INTERVAL = 10800  # fallback seconds between reclaimable pre-warm passes (heavy)
_last_reclaim: dict[str, float] = {}


def _reindex_interval() -> float:
    """Background-pass interval, read live from settings so a change takes effect without restart."""
    try:
        val = float(load_settings().get("kopialens", {}).get("reindex_interval", _DEFAULT_REINDEX_INTERVAL))
        return max(60.0, val)  # never tighter than 1 minute
    except Exception:
        return _DEFAULT_REINDEX_INTERVAL


def _reclaim_interval() -> float:
    """How often to pre-warm per-snapshot reclaimable (heavy op), read live from settings."""
    try:
        val = float(load_settings().get("kopialens", {}).get("reclaim_interval", _DEFAULT_RECLAIM_INTERVAL))
        return max(300.0, val)  # never tighter than 5 minutes (it is expensive)
    except Exception:
        return _DEFAULT_RECLAIM_INTERVAL


def _maybe_warm_reclaim(repo: str) -> None:
    """Pre-compute per-snapshot reclaimable on a slow cadence so it is always cached/available.
    Runs AFTER _warm (which refreshes the dashboard -> content_refs), so the refcount it reads is
    fresh (the overestimate guard). Heavy (refcount + forward+backward walk), hence its own timer."""
    now = time.time()
    if now - _last_reclaim.get(repo, 0.0) < _reclaim_interval():
        return
    try:
        snap_prewarm(repo, with_reclaim=True)
        _last_reclaim[repo] = now
        log.info("reclaim pre-warm done repo=%s", repo)
    except Exception as exc:  # noqa: BLE001
        log.error("reclaim pre-warm repo=%s failed: %s", repo, exc)


def _maybe_compact(repo: str) -> None:
    """Auto-compact the DuckDB when it has grown past a ratio over its last-compacted size, and the
    min interval has passed (default 1.8x / 24h, from settings). DuckDB never reclaims dead space on
    its own, so this is its 'GC'. Skips a freshly-built DB (just records the baseline)."""
    kl = load_settings().get("kopialens", {})
    if not kl.get("auto_compact", True):
        return
    from .index.schema import compact_in_place, compact_state, db_path, record_compact_size
    live = db_path(repo)
    if not os.path.exists(live):
        return
    cur = os.path.getsize(live)
    st = compact_state().get(repo, {})
    base = st.get("size")
    if not base:
        record_compact_size(repo, cur)  # establish baseline; never compact a just-rebuilt DB
        return
    ratio = float(kl.get("compact_ratio", 1.8))
    interval = float(kl.get("compact_min_interval", 86400))
    if time.time() - st.get("ts", 0) < interval or cur < base * ratio:
        return
    log.info("repo=%s auto-compact: size %d >= %.2fx baseline %d", repo, cur, ratio, base)
    try:
        compact_in_place(repo)  # records the new baseline on success
    except Exception as exc:  # noqa: BLE001
        log.error("auto-compact repo=%s failed: %s", repo, exc)


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
    """Post-index warm pass (background): next-snapshot times + per-snapshot churn pre-warm
    (forward walk only) + dashboard + stats + search-landing cache refresh. Everything cacheable
    is kept warm here so the open UI always serves from cache (serve-stale-while-revalidate)."""
    try:
        compute_next_snapshots(repo)
    except Exception as exc:  # noqa: BLE001
        log.error("warm next_snapshots repo=%s failed: %s", repo, exc)
    try:
        snap_prewarm(repo)  # churn only (with_reclaim=False) - light forward walk
    except Exception as exc:  # noqa: BLE001
        log.error("warm snap_prewarm repo=%s failed: %s", repo, exc)
    _refresh_dashboard(repo)
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


def _maintain(repo: str) -> None:
    """One background-maintenance pass for a single repo, kind-dispatched. Kopia repos run the
    DuckDB reindex + warm + reclaim + compact; the Backblaze repo runs its own SQLite refresh
    (a ~9-file stat no-op in steady state) and never touches any kopia/DuckDB code."""
    if repo_kind(repo) == "backblaze":
        try:
            from .backblaze import indexer as bz_indexer
            summary = bz_indexer.refresh_via_worker()  # subprocess - keeps the GIL off the app's own reads
            if summary.get("changed"):
                log.info("BZ refresh repo=%s: %s", repo, summary)
        except Exception as exc:  # noqa: BLE001
            log.error("BZ refresh repo=%s failed: %s", repo, exc)
        try:
            # Opportunistic WAL trim from the app's own connection: the worker's own TRUNCATE (end
            # of refresh()) can be a no-op while the app holds a read snapshot; retry it here.
            from .backblaze import bbdb as bz_bbdb
            row = bz_bbdb.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row and row[0] == 0:
                log.info("BZ wal_checkpoint(TRUNCATE) ok: busy=%s log=%s checkpointed=%s", *row)
        except Exception as exc:  # noqa: BLE001 - never fail the cycle on housekeeping
            log.warning("BZ wal_checkpoint(TRUNCATE) skipped: %s", exc)
        return
    try:
        auto_reindex(repo)  # full rebuild if schema migrated, else incremental no-op
    except Exception as exc:  # noqa: BLE001
        log.error("reindex repo=%s failed: %s", repo, exc)
    _warm(repo)
    _maybe_warm_reclaim(repo)
    _maybe_compact(repo)


def _bg_worker() -> None:
    """Self-healing background maintenance: keep the index complete + per-snapshot churn +
    next-snapshot times + dashboard fresh. All work is DuckDB + bounded subprocess bursts."""
    for repo in REPOS:
        _maintain(repo)
    gc.collect()  # return the cycle's transient sizemap/refcount arenas to the OS (pymalloc won't on its own)
    log_memory("startup cycle done")
    while not _bg_stop.wait(_reindex_interval()):
        for repo in REPOS:
            _maintain(repo)
        gc.collect()
        log_memory("cycle done")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    log.info("KopiaLens starting up")
    try:  # apply the configurable in-process dir-cache LRU cap (RAM lever; default 50k)
        from .core import cache as _cache
        _cache.DIR_MEMO_MAX = int(load_settings().get("kopialens", {}).get("dir_memo_max", _cache.DIR_MEMO_MAX))
        log.info("DIR_MEMO_MAX=%d", _cache.DIR_MEMO_MAX)
    except Exception as exc:  # noqa: BLE001 - never block startup on a tuning read
        log.warning("dir_memo_max setting read failed: %s", exc)
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
