"""Compact a KopiaLens DuckDB in place (reclaim dead space) WITHOUT re-walking kopia.

DuckDB never reclaims space on DELETE, so the per-cycle map rewrites bloat the file (repo1 grew
4 GB -> 18.7 GB in days). This rewrites each repo's `.duckdb` into a fresh, compact file via
`COPY FROM DATABASE` (a pure DB-to-DB copy - seconds, no kopia walk), verifies row counts, then
swaps it in (the original is kept as a timestamped `.bak`).

USAGE: close KopiaLens first, then `python compact.py` (interactive) or `python compact.py 2`.
"""

from __future__ import annotations

import os
import sys
import time

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, REPOS  # noqa: E402

VERIFY_TABLES = ("files", "paths", "content_refs", "content_sizes", "snapshot_analysis")


def _size(p: str) -> int:
    return os.path.getsize(p) if os.path.exists(p) else 0


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _sql_path(p: str) -> str:
    return p.replace("\\", "/")  # DuckDB takes forward slashes on Windows; avoids escaping issues


def compact(repo: str) -> None:
    live = os.path.join(DATA_DIR, f"kopialens_repo{repo}.duckdb")
    if not os.path.exists(live):
        print(f"repo{repo}: no DB at {live} - skipping")
        return
    new = live + ".compact"
    for f in (new, new + ".wal"):
        if os.path.exists(f):
            os.remove(f)

    # Fail fast if the app still holds the DB open (a live connection = swap would be unsafe).
    try:
        probe = duckdb.connect(live, read_only=True)
        probe.close()
    except Exception as exc:  # noqa: BLE001
        print(f"repo{repo}: cannot open (is KopiaLens still running?) -> {exc}")
        return

    before = _size(live)
    print(f"repo{repo}: compacting {_fmt(before)} ...")
    t0 = time.time()
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{_sql_path(live)}' AS s (READ_ONLY)")
        con.execute(f"ATTACH '{_sql_path(new)}' AS d")
        con.execute("COPY FROM DATABASE s TO d")
        checks: dict[str, tuple[int, int]] = {}
        for tbl in VERIFY_TABLES:
            try:
                a = con.execute(f"SELECT count(*) FROM s.{tbl}").fetchone()[0]
                b = con.execute(f"SELECT count(*) FROM d.{tbl}").fetchone()[0]
                checks[tbl] = (a, b)
            except Exception:  # noqa: BLE001 - table may not exist; skip
                pass
    finally:
        con.execute("DETACH s")
        con.execute("DETACH d")
        con.close()

    mismatched = [t for t, (a, b) in checks.items() if a != b]
    if mismatched:
        print(f"repo{repo}: VERIFY FAILED on {mismatched} - original left untouched. New file: {new}")
        return

    bak = live + f".bak-{time.strftime('%Y%m%d_%H%M%S')}"
    os.replace(live, bak)
    if os.path.exists(live + ".wal"):
        os.remove(live + ".wal")
    os.replace(new, live)
    after = _size(live)
    print(f"repo{repo}: {_fmt(before)} -> {_fmt(after)}  (saved {_fmt(before - after)}) in {time.time() - t0:.1f}s")
    print(f"          verified {checks}")
    print(f"          original kept as {os.path.basename(bak)} - delete it once you've confirmed the app works")


def main() -> None:
    args = [a for a in sys.argv[1:] if a in REPOS]
    if not args:
        print("KopiaLens DuckDB compaction (run with KopiaLens CLOSED).")
        print("Repos: " + ", ".join(f"{k} = {v['name']}" for k, v in REPOS.items()))
        sel = input("Compact which repo? (number, or 'all'): ").strip().lower()
        args = list(REPOS) if sel in ("all", "") else [sel]
    for repo in args:
        if repo not in REPOS:
            print(f"unknown repo '{repo}' - skipping")
            continue
        try:
            compact(repo)
        except Exception as exc:  # noqa: BLE001
            print(f"repo{repo}: FAILED - {exc}")
    input("\nDone. Press Enter to exit...")


if __name__ == "__main__":
    main()
