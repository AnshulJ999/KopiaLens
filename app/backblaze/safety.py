"""Read-only guard for the Backblaze data tree.

The whole BZ backend is read-only on `C:\\ProgramData\\Backblaze`. Every file read
goes through ``open(..., 'r')`` / ``zipfile`` only. This module is belt-and-suspenders:
any code path that ever tries to WRITE under the Backblaze root raises immediately, so
a future bug (or a careless edit) can never modify the user's live backup state.
"""

from __future__ import annotations

import os

# The whole Backblaze install tree is off-limits for writes (covers bzdata + anything else).
BZ_PROTECTED_ROOT = os.path.normcase(os.path.normpath(r"C:\ProgramData\Backblaze"))


def _under_protected(path: str) -> bool:
    try:
        p = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except Exception:
        return False
    return p == BZ_PROTECTED_ROOT or p.startswith(BZ_PROTECTED_ROOT + os.sep)


def guard_no_write(path: str) -> None:
    """Raise if `path` resolves under the protected Backblaze tree. Call this before ANY
    open-for-write / rename / delete that could touch a Backblaze-controlled path."""
    if _under_protected(path):
        raise PermissionError(
            f"Refusing to write under the read-only Backblaze tree: {path!r}. "
            "The BZ backend is strictly read-only on C:\\ProgramData\\Backblaze."
        )


def open_ro(path: str, **kwargs):
    """Open a Backblaze file read-only. Forces mode 'r'/'rb' and asserts no write intent."""
    mode = kwargs.pop("mode", "r")
    if any(c in mode for c in "wax+"):
        raise PermissionError(f"open_ro refuses write mode {mode!r} for {path!r}")
    return open(path, mode, **kwargs)
