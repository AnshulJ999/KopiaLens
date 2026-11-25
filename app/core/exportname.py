"""Names for files KopiaLens hands the user (CSV exports, scripts, backups, diagnostics).

`kopialens-<what>-<about>-<repo>-<date-time>.<ext>`: lowercase, only a-z 0-9 . _ -, repeats
collapsed, ends trimmed. <about> (the file, folder or search words it is for) is cut at a word
near 24 characters and never holds a path. Twin of `exportName` in frontend/js/util.js.
"""
from __future__ import annotations

import re
import time

ABOUT_MAX = 24
NAME_MAX = 100
_BAD = re.compile(r"[^a-z0-9._-]+")
_DASHES = re.compile(r"[-._]*-[-._]*")


def slug(text: str, limit: int = 0) -> str:
    """``text`` as a safe name part; with ``limit``, cut at a dash near it."""
    s = _DASHES.sub("-", _BAD.sub("-", str(text or "").lower())).strip("-._")
    if limit and len(s) > limit:
        cut = s.rfind("-", 0, limit + 1)
        s = (s[:cut] if cut >= limit // 2 else s[:limit]).strip("-._")
    return s


def about_part(about: str) -> str:
    """The last part of a path or label, cut near ABOUT_MAX; "...-and-3-more" stays whole."""
    tail = re.split(r"[\\/]", str(about or "").rstrip("\\/"))[-1]
    more = re.search(r"-and-\d+-more$", tail)
    head = slug(tail[: more.start()] if more else tail, ABOUT_MAX)
    return head + (more.group(0) if more and head else "")


def repo_label(repo: str) -> str:
    """The repository's short name, as chips show it ("Repo 1", "BZ")."""
    from . import repos
    rec = next((r for r in repos.all_records() if str(r.get("id")) == str(repo)), None) or {}
    return rec.get("short_name") or repos.default_short_name(str(repo), rec.get("kind") or ("backblaze" if repo == "bz" else "kopia"))


def client_name(name: str, ext: str = "csv") -> str:
    """A name the page built with exportName, cleaned again; "" when it isn't one."""
    s = slug(name)
    return s[:120] if s.startswith("kopialens-") and s.endswith("." + ext) else ""


def export_name(what: str, about: str = "", repo: str = "", ext: str = "csv", when: float | None = None) -> str:
    stamp = time.strftime("%Y-%m-%d-%H%M", time.localtime(when))
    parts = ["kopialens", slug(what), about_part(about), slug(repo, 16)]
    stem = "-".join(p for p in parts if p)
    stem = stem[: NAME_MAX - len(stamp) - len(ext) - 2].rstrip("-._")
    return f"{stem}-{stamp}.{slug(ext) or 'txt'}"
