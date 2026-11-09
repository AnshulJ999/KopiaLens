"""Read-only guard for the Backblaze data tree.

The whole BZ backend is read-only on the Backblaze data it reads (this computer's install or a
shared folder). Every file read goes through ``open_ro`` / ``zipfile`` only. This module is
belt-and-suspenders: any code path that ever tries to WRITE under a Backblaze root raises
immediately, so a future bug (or a careless edit) can never modify the user's live backup state.
"""

from __future__ import annotations

import os

# The Backblaze install trees (Windows, Mac) are off-limits for writes, plus whatever bzdata
# folder is being read (a shared folder from another computer).
BZ_PROTECTED_ROOT = os.path.normcase(os.path.normpath(r"C:\ProgramData\Backblaze"))
_INSTALL_ROOTS = (r"C:\ProgramData\Backblaze", "/Library/Backblaze.bzpkg")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _protected_roots() -> list[str]:
    from . import paths
    return [_norm(r) for r in (*_INSTALL_ROOTS, paths.bzdata_root()) if r]


def _under_protected(path: str) -> bool:
    try:
        p = _norm(path)
        roots = _protected_roots()
    except Exception:
        return False
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def guard_no_write(path: str) -> None:
    """Raise if `path` resolves under a protected Backblaze tree. Call this before ANY
    open-for-write / rename / delete that could touch a Backblaze-controlled path."""
    if _under_protected(path):
        raise PermissionError(
            f"Refusing to write under the read-only Backblaze data: {path!r}. "
            "KopiaLens only ever reads Backblaze's files."
        )


def open_ro(path: str, **kwargs):
    """Open a Backblaze file read-only. Forces mode 'r'/'rb' and asserts no write intent."""
    mode = kwargs.pop("mode", "r")
    if any(c in mode for c in "wax+"):
        raise PermissionError(f"open_ro refuses write mode {mode!r} for {path!r}")
    return open(path, mode, **kwargs)
