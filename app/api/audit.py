"""Audit-trail reader. The JSONL itself is written by ``core/audit.py``; this is the
read-only surface the Stats tab renders."""

from __future__ import annotations

from fastapi import APIRouter

from ..core import audit

router = APIRouter(tags=["audit"])


@router.get("/audit")
def audit_tail(limit: int = 200, hide_routine: bool = False) -> dict:
    """The last ``limit`` audit rows the Logs tab shows (oldest first) + the file size."""
    limit = max(1, min(limit, 2000))
    return {"records": audit.read_view(limit, hide_routine), "size": audit.size_bytes()}
