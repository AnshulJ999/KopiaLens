"""KopiaLens FastAPI application."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import actions, dashboard, search, simulate, snapshots, tree
from .core import kopia as kopia_mod
from .core.applog import get_logger, setup_logging
from .core.server import SERVERS
from .index.schema import close_all as close_duckdb

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

log = get_logger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    log.info("KopiaLens starting up")
    SERVERS.warmup()  # start the per-repo kopia servers (lazy retry if one fails)
    yield
    log.info("KopiaLens shutting down")
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

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True}
