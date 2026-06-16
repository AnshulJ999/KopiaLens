"""KopiaLens FastAPI application."""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import os
import pickle

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import REPOS

from .api import actions, dashboard, search, simulate, snapshots, stats, tree
from .core import kopia as kopia_mod
from .core.applog import get_logger, setup_logging
from .core.metrics import refcount_path
from .core.schedule import compute_next_snapshots
from .core.server import SERVERS
from .index.builder import auto_reindex
from .index.physical import materialize_refcount
from .index.schema import close_all as close_duckdb, get_db
from .index.snapanalysis import prewarm as snap_prewarm
from .settings import load_settings

log = get_logger("app")

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

_bg_stop = threading.Event()
_REINDEX_INTERVAL = 1800  # seconds between background incremental reindex passes


def _materialize_refcount_from_pkl(repo: str) -> None:
    """Populate content_refs (powers accurate freeable) from the dashboard's refcount pkl when
    it already exists - never forces the heavy global walk here (file_detail falls back lazily)."""
    path = refcount_path(repo, "all")
    if not os.path.exists(path):
        return
    with open(path, "rb") as fh:
        cached = pickle.load(fh)
    con = get_db(repo).cursor()
    try:
        materialize_refcount(con, repo, cached.get("count", {}))
        con.commit()
    finally:
        con.close()


def _warm(repo: str) -> None:
    """Post-index warm pass: next-snapshot times, churn pre-warm (persisted), refcount table."""
    for fn in (compute_next_snapshots, snap_prewarm, _materialize_refcount_from_pkl):
        try:
            fn(repo)
        except Exception as exc:  # noqa: BLE001
            log.error("warm %s repo=%s failed: %s", getattr(fn, "__name__", fn), repo, exc)


def _bg_worker() -> None:
    """Self-healing background maintenance: keep the index complete + per-snapshot churn +
    next-snapshot times fresh. All work is DuckDB + bounded subprocess bursts - no kopia server."""
    for repo in REPOS:
        try:
            auto_reindex(repo)  # full rebuild if schema migrated, else incremental no-op
        except Exception as exc:  # noqa: BLE001
            log.error("startup reindex repo=%s failed: %s", repo, exc)
        _warm(repo)
    while not _bg_stop.wait(_REINDEX_INTERVAL):
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
