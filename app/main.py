"""KopiaLens FastAPI application."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import actions, dashboard, search, simulate, snapshots, tree

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

app = FastAPI(title="KopiaLens", version="0.1.0")

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
