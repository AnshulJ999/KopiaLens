"""KopiaLens FastAPI application."""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import REPOS

from .api import actions, dashboard, search, simulate, snapshots, stats, tree
from .core import kopia as kopia_mod
from .core.applog import get_logger, setup_logging
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
_DEFAULT_REINDEX_INTERVAL = 1800  # fallback seconds between background passes (overridable in settings)


def _reindex_interval() -> float:
    """Background-pass interval, read live from settings so a change takes effect without restart."""
    try:
        val = float(load_settings().get("kopialens", {}).get("reindex_interval", _DEFAULT_REINDEX_INTERVAL))
        return max(60.0, val)  # never tighter than 1 minute
    except Exception:
        return _DEFAULT_REINDEX_INTERVAL


def _refresh_dashboard(repo: str) -> None:
    """Recompute the dashboard cache so the open UI auto-updates without a manual Refresh. Cheap
    now that build_global streams the refcount; unchanged sources hit the warm srcset cache."""
    try:
        data = table_rows(repo, refresh=True, mode="all")
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


def _bg_worker() -> None:
    """Self-healing background maintenance: keep the index complete + per-snapshot churn +
    next-snapshot times + dashboard fresh. All work is DuckDB + bounded subprocess bursts."""
    for repo in REPOS:
        try:
            auto_reindex(repo)  # full rebuild if schema migrated, else incremental no-op
        except Exception as exc:  # noqa: BLE001
            log.error("startup reindex repo=%s failed: %s", repo, exc)
        _warm(repo)
    while not _bg_stop.wait(_reindex_interval()):
        for repo in REPOS:
            try:
                auto_reindex(repo)  # incremental; cheap no-op when nothing changed
            except Exception as exc:  # noqa: BLE001
                log.error("periodic reindex repo=%s failed: %s", repo, exc)
            _warm(repo)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    log.info("KopiaLens starting up")
    if load_settings().get("listing_backend") == "server":
        SERVERS.warmup()  # only when explicitly opted into the persistent-server backend
    threading.Thread(target=_bg_worker, daemon=True).start()
    yield
    log.info("KopiaLens shutting down")
    _bg_stop.set()
    SERVERS.shutdown()
    close_duckdb()


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


app.include_router(dashboard.router, prefix="/api")
app.include_router(snapshots.router, prefix="/api")
app.include_router(tree.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(simulate.router, prefix="/api")
app.include_router(actions.router, prefix="/api")
app.include_router(stats.router, prefix="/api")

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True}
