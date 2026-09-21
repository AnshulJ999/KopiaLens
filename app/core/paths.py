"""Per-source path rules.

Separator and case sensitivity are properties of the backup SOURCE, not of the host running
KopiaLens: a Linux box analysing Windows-sourced snapshots must still treat those paths as
backslash-separated and case-insensitive. ``os.sep`` is therefore never the right answer.

Rules are auto-detected from the shape of the source's root path and can be overridden per
source on the repository record (POSIX vs macOS is indistinguishable from path shape alone -
macOS users override to case-insensitive).
"""

from __future__ import annotations

import threading

WIN_SEP = chr(92)
POSIX_SEP = "/"

_WIN = {"sep": WIN_SEP, "case_sensitive": False}
_POSIX = {"sep": POSIX_SEP, "case_sensitive": True}

# Per-source overrides, keyed by kopia source key. Seeded once at startup from the repository
# records; a source key identifies the same machine+path in any repo, so one registry is right.
_OVERRIDES: dict[str, dict] = {}
_loaded = False
_LOAD_LOCK = threading.Lock()


def configure(overrides: dict | None) -> None:
    """Replace the override registry. Explicit configuration wins over the record store."""
    global _loaded
    with _LOAD_LOCK:
        _OVERRIDES.clear()
        if overrides:
            _OVERRIDES.update(overrides)
        _loaded = True


def _ensure_loaded() -> None:
    """Self-load from the repository records on first use, so neither the app nor the worker
    subprocess has a wiring step that can be forgotten. Locked, and `_loaded` is set only AFTER
    the registry is populated: a concurrent caller must never observe an empty registry and
    resolve a path identity from detection when an override exists."""
    global _loaded
    if _loaded:
        return
    with _LOAD_LOCK:
        if _loaded:
            return
        try:
            import config
            _OVERRIDES.update(config.PATH_OVERRIDES or {})
        except Exception:  # noqa: BLE001 - overrides are optional; detection alone is valid
            pass
        _loaded = True


def source_root(source_key: str) -> str:
    """Strip the ``user@host:`` prefix off a kopia source key."""
    return source_key.split(":", 1)[1] if ":" in source_key else source_key


def detect(source_key: str) -> dict:
    """``{"sep": str, "case_sensitive": bool}`` inferred from the source root's shape."""
    root = source_root(source_key)
    if root.startswith(POSIX_SEP):
        return dict(_POSIX)
    return dict(_WIN)  # drive letter, UNC, or unknown -> today's behaviour


def rules(source_key: str) -> dict:
    """Detected rules with any per-source override applied."""
    _ensure_loaded()
    out = detect(source_key)
    out.update({k: v for k, v in (_OVERRIDES.get(source_key) or {}).items()
                if k in ("sep", "case_sensitive")})
    return out


def sep(source_key: str) -> str:
    return rules(source_key)["sep"]


def case_sensitive(source_key: str) -> bool:
    return rules(source_key)["case_sensitive"]


def prefix_bounds(prefix: str) -> tuple[str, str]:
    """The half-open ``[lo, hi)`` range covering every string that STARTS WITH ``prefix``.

    ``hi`` advances the prefix's last character by one - it is NOT ``prefix + U+FFFF``: a name may
    start with a character above U+FFFF (any emoji, and the U+FFFF noncharacter is the top of the
    BMP, not of Unicode), and those rows sorted ABOVE the old bound, so a range query silently
    dropped them and the Excluded browser called such a file "not backed up". DuckDB compares
    VARCHAR by codepoint, so advancing one codepoint is the exact bound."""
    if not prefix:
        return "", ""
    last = ord(prefix[-1])
    if last >= 0x10FFFF:                      # cannot advance: nothing exists above it anyway
        return prefix, prefix + chr(0x10FFFF)
    return prefix, prefix[:-1] + chr(last + 1)


def ident_sql(source_key: str, column: str = "full_path") -> str:
    """SQL expression for a path's IDENTITY within this source.

    Case-sensitive sources key on the raw path; case-insensitive ones fold case so that a
    file renamed Photo.jpg -> photo.jpg stays one path. Never used as a search accelerator -
    that is what ``paths.path_lc`` remains for.
    """
    return column if case_sensitive(source_key) else f"lower({column})"


def ident(source_key: str, path: str) -> str:
    """Python twin of ``ident_sql`` - must agree with it exactly."""
    return path if case_sensitive(source_key) else path.lower()


def join(source_key: str, root: str, relative: str) -> str:
    """Join a source root and a walk-relative path using the source's own separator."""
    s = sep(source_key)
    rel = relative.lstrip("\\/")
    if not rel:
        return root
    return root + rel if root.endswith(("\\", "/")) else root + s + rel
