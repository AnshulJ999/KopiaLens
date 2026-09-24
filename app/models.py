"""Pydantic response schemas used by the API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ApiError(BaseModel):
    error: str


class RepoInfo(BaseModel):
    id: str
    name: str
    kind: str = "kopia"


class SourceRow(BaseModel):
    source: str
    snaps: int
    logical_latest: int
    logical_sum: int
    physical: int
    physical_latest: int
    accum: float
    repo_pct: float
    freeable: int
    dedup: float
    pins: int
    first: str
    last: str


class SnapshotRow(BaseModel):
    id: str
    source: str
    start_time: str = Field(alias="startTime")
    end_time: str | None = Field(default=None, alias="endTime")
    logical: int
    files: int = 0
    dirs: int = 0
    pins: Any = None


class FileHit(BaseModel):
    source: str
    full_path: str
    name: str
    size: int
    versions: int
    first_seen: Any
    last_seen: Any
    in_latest: bool
    on_disk: bool | None = None


class RetentionPolicy(BaseModel):
    latest: int = 100
    hourly: int = 96
    daily: int = 30
    weekly: int = 52
    monthly: int = 36
    annual: int = 5


class SimRequest(BaseModel):
    repo: str
    source: str | None = None
    mode: str = "all"
    refresh: bool = False
    policy: RetentionPolicy = Field(default_factory=RetentionPolicy)
