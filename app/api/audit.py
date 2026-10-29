"""Audit-trail reader. The JSONL itself is written by ``core/audit.py``; this is the
read-only surface the Logs tab renders."""

from __future__ import annotations

from fastapi import APIRouter

from ..core import audit

router = APIRouter(tags=["audit"])


@router.get("/audit")
def audit_tail(limit: int = 200, groups: str = "", hide_routine: bool = False) -> dict:
    """The last ``limit`` audit rows the Logs tab shows (oldest first), the file size and a count
    per filter chip. ``groups`` lists the chips that are on (none = everything); ``hide_routine``
    is the tickbox's filter, used when the chips are switched off in the frontend."""
    limit = max(1, min(limit, 2000))
    chosen = {g for g in groups.split(",") if g in audit.GROUPS}
    if chosen:
        keep = lambda r: audit.in_groups(r, chosen)  # noqa: E731
    elif hide_routine:
        keep = lambda r: audit.group(r) == "changes"  # noqa: E731
    else:
        keep = None
    return {"records": audit.read_view(limit, keep), "size": audit.size_bytes(),
            "counts": audit.group_counts()}
