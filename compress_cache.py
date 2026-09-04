"""Compress (or decompress) a KopiaLens sqlite walk cache WITHOUT re-walking kopia.

The walk caches hold uncompressed JSON - ~11.5 GB across both repos here. Rows are immutable and
written once / read many, so zstd trades a one-time rewrite for a permanent ~4-7x saving; zstd's
decompression speed is near level-independent, so the read path barely notices.

Both formats are readable side by side (TEXT = JSON, BLOB = zstd), so this is optional and can be
run, re-run, or reversed at any time. Nothing is deleted: the original is kept as a timestamped
.bak for you to remove once you are happy.

USAGE: close KopiaLens first, then `python compress_cache.py` (interactive), or
       `python compress_cache.py 1 zstd-3`   /   `python compress_cache.py all none`
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CACHEDIR, kopia_repos  # noqa: E402
from app.core import cache as klcache  # noqa: E402

TABLES = (("dir_children", "oid", "children"), ("indirect", "oid", "parts"))
ROWS_PER_BATCH = 20_000


def _size(p: str) -> int:
    return os.path.getsize(p) if os.path.exists(p) else 0


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _open(path: str, read_only: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro" if read_only else path, uri=read_only, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=OFF")   # rewriting into a throwaway file; a crash just restarts it
    return db


def transform(repo: str, level_name: str) -> None:
    live = os.path.join(CACHEDIR, f"repo{repo}.db")
    if not os.path.exists(live):
        print(f"repo{repo}: no walk cache at {live} - skipping")
        return
    new = live + ".rewrite"
    for f in (new, new + "-wal", new + "-shm"):
        if os.path.exists(f):
            os.remove(f)

    klcache._level = klcache._LEVELS.get(level_name, 0)  # force the level, ignore the setting
    if level_name != "none" and klcache._zstd is None:
        print("the `zstandard` package is not installed - cannot compress")
        return

    before = _size(live)
    print(f"repo{repo}: rewriting {_fmt(before)} as {level_name} ...")
    t0 = time.time()
    src = _open(live, read_only=True)
    dst = _open(new)
    counts: dict[str, tuple[int, int]] = {}
    try:
        for table, keycol, valcol in TABLES:
            dst.execute(f"CREATE TABLE IF NOT EXISTS {table} ({keycol} TEXT PRIMARY KEY, {valcol} TEXT)")
            try:
                total = src.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = (0, 0)
                continue
            cur = src.execute(f"SELECT {keycol}, {valcol} FROM {table}")
            done = 0
            while True:
                rows = cur.fetchmany(ROWS_PER_BATCH)
                if not rows:
                    break
                dst.executemany(
                    f"INSERT OR REPLACE INTO {table} VALUES (?,?)",
                    [(k, klcache.encode_json(klcache.decode_json(v))) for k, v in rows],
                )
                dst.commit()
                done += len(rows)
                print(f"  {table}: {done:,}/{total:,}", end="\r")
            print(f"  {table}: {done:,}/{total:,}")
            counts[table] = (total, dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        src.close()
        dst.close()

    mismatched = [t for t, (a, b) in counts.items() if a != b]
    if mismatched:
        print(f"repo{repo}: VERIFY FAILED on {mismatched} - original left untouched. New file: {new}")
        return

    bak = live + f".bak-{time.strftime('%Y%m%d_%H%M%S')}"
    os.replace(live, bak)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(live + suffix):
            os.remove(live + suffix)
    os.replace(new, live)
    after = _size(live)
    saved = before - after
    print(f"repo{repo}: {_fmt(before)} -> {_fmt(after)}  "
          f"({'saved ' + _fmt(saved) if saved > 0 else 'grew ' + _fmt(-saved)}) in {time.time() - t0:.1f}s")
    print(f"          verified {counts}")
    print(f"          original kept as {os.path.basename(bak)} - delete it once you've confirmed the app works")


def main() -> None:
    repos = kopia_repos()
    args = [a for a in sys.argv[1:]]
    level = next((a for a in args if a in klcache._LEVELS), None)
    targets = [a for a in args if a in repos] or (repos if "all" in args else [])
    if not targets or level is None:
        print("KopiaLens walk-cache compression (run with KopiaLens CLOSED).")
        print("Repos: " + ", ".join(repos))
        if not targets:
            sel = input("Rewrite which repo? (id, or 'all'): ").strip().lower()
            targets = repos if sel in ("all", "") else [sel]
        if level is None:
            print("Levels: none (decompress) | zstd-1 (faster rewrite) | zstd-3 (better ratio, same read speed)")
            level = input("Level? [zstd-3]: ").strip().lower() or "zstd-3"
    if level not in klcache._LEVELS:
        print(f"unknown level '{level}'")
        input("\nPress Enter to exit...")
        return
    for repo in targets:
        if repo not in repos:
            print(f"unknown repo '{repo}' - skipping")
            continue
        try:
            transform(repo, level)
        except Exception as exc:  # noqa: BLE001
            print(f"repo{repo}: FAILED - {exc}")
    input("\nDone. Press Enter to exit...")


if __name__ == "__main__":
    main()
