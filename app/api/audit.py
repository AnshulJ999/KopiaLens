"""Audit-trail reader. The JSONL itself is written by ``core/audit.py``; this is the
read-only surface the Stats tab renders."""

from __future__ import annotations

from fastapi import APIRouter

from ..core import audit

router = APIRouter(tags=["audit"])


@router.get("/audit")
def audit_tail(limit: int = 200) -> dict:
    """The last ``limit`` audit records (oldest first) + the file size, so the Stats tab can show
    growth at a glance without anyone having to go look at the file."""
    limit = max(1, min(limit, 2000))
    return {"records": audit.read_tail(limit), "size": audit.size_bytes()}
