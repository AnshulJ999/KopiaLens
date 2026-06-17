"""Lightweight RSS / cache-size instrumentation for tuning RAM + perf.

Cheap to call (one psutil read + a few COUNT(*) queries). Used by the background
worker to emit one compact memory line per cycle and by the /debug/memory endpoint
for on-demand inspection. Kept deliberately non-intrusive: no behaviour change, just
observation. All heavy imports are lazy so importing this module is free.
"""

from __future__ import annotations

import os

from .applog import get_logger

log = get_logger("mem")


def _rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - psutil missing / platform quirk: instrumentation must never crash
        return None


def memory_snapshot() -> dict:
    """Process RSS + the in-process caches + per-repo DuckDB table sizes (the real RAM drivers)."""
    from .cache import DIR_MEMO, DIR_MEMO_MAX

    snap: dict = {
        "rss_mb": _rss_mb(),
        "dir_memo": len(DIR_MEMO),
        "dir_memo_max": DIR_MEMO_MAX,
        "repos": {},
    }
    try:
        from config import REPOS
        from ..index.schema import read_cursor

        for repo in REPOS:
            try:
                cur = read_cursor(repo)
                try:
                    files = cur.execute("SELECT count(*) FROM files WHERE repo=?", [repo]).fetchone()[0]
                    paths = cur.execute("SELECT count(*) FROM paths WHERE repo=?", [repo]).fetchone()[0]
                    crefs = cur.execute("SELECT count(*) FROM content_refs WHERE repo=?", [repo]).fetchone()[0]
                    csizes = cur.execute("SELECT count(*) FROM content_sizes WHERE repo=?", [repo]).fetchone()[0]
                finally:
                    cur.close()
                snap["repos"][repo] = {
                    "files": files, "paths": paths, "content_refs": crefs, "content_sizes": csizes,
                }
            except Exception as exc:  # noqa: BLE001
                snap["repos"][repo] = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        snap["repos_error"] = str(exc)
    return snap


def log_memory(tag: str = "") -> dict:
    """Emit ONE compact line (not spammy) and return the snapshot."""
    s = memory_snapshot()
    rss = f"{s['rss_mb']:.0f}MB" if s.get("rss_mb") is not None else "?"
    parts = [f"rss={rss}", f"dir_memo={s['dir_memo']}/{s['dir_memo_max']}"]
    for repo, r in s.get("repos", {}).items():
        if "error" not in r:
            parts.append(f"r{repo}[files={r['files']} paths={r['paths']} refs={r['content_refs']}]")
    log.info("%s%s", f"{tag} " if tag else "", " ".join(parts))
    return s
