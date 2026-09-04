"""Per-source path rules.

Separator and case sensitivity are properties of the backup SOURCE, not of the host running
KopiaLens: a Linux box analysing Windows-sourced snapshots must still treat those paths as
backslash-separated and case-insensitive. ``os.sep`` is therefore never the right answer.

Rules are auto-detected from the shape of the source's root path and can be overridden per
source on the repository record (POSIX vs macOS is indistinguishable from path shape alone -
macOS users override to case-insensitive).
"""

from __future__ import annotations

WIN_SEP = chr(92)
POSIX_SEP = "/"

_WIN = {"sep": WIN_SEP, "case_sensitive": False}
_POSIX = {"sep": POSIX_SEP, "case_sensitive": True}

# Per-source overrides, keyed by kopia source key. Seeded once at startup from the repository
# records; a source key identifies the same machine+path in any repo, so one registry is right.
_OVERRIDES: dict[str, dict] = {}
_loaded = False


def configure(overrides: dict | None) -> None:
    """Replace the override registry. Explicit configuration wins over the record store."""
    global _loaded
    _OVERRIDES.clear()
    if overrides:
        _OVERRIDES.update(overrides)
    _loaded = True


def _ensure_loaded() -> None:
    """Self-load from the repository records on first use, so neither the app nor the worker
    subprocess has a wiring step that can be forgotten."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        import config
        _OVERRIDES.update(config.PATH_OVERRIDES or {})
    except Exception:  # noqa: BLE001 - overrides are optional; detection alone is valid
        pass


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
