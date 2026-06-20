"""Standalone build + verification harness for the Backblaze index (read-only on bzdata).

Builds the SQLite index from the live bzdata, then VALIDATES the rule engine by comparing the
computed per-drive backed counts against Backblaze's own authoritative bzstat_totalbackup.xml.
This is the success criterion for the rule engine: computed `backed` should track `auth` closely.

Run from the KopiaLens folder:
    python -m app.backblaze.bz_verify            # incremental
    python -m app.backblaze.bz_verify --force    # full rebuild
"""

from __future__ import annotations

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.backblaze import bbdb, indexer, paths, queries  # noqa: E402


def _gb(n):
    return (n or 0) / 1_000_000_000


def _fmt(n):
    return f"{n:,}" if n is not None else "n/a"


def main(force: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    if not paths.exists():
        print(f"bzdata not found at {paths.bzdata_root()}")
        return 1
    print(f"bzdata: {paths.bzdata_root()}")
    print(f"sqlite: {bbdb.db_path()}")
    print(f"FTS5 trigram available: {bbdb.fts_available()}")
    vols = paths.volumes()
    print(f"volumes: {', '.join(f'{v.letter}({v.guid[:5]})' for v in vols)}\n")

    t0 = time.time()
    summary = indexer.refresh(force=force)
    print(f"index refresh: {summary}\n")

    # ---- VALIDATION: computed backed vs Backblaze authoritative ----
    ov = queries.overview()
    print("=== Per-drive: computed (this engine) vs Backblaze authoritative (bzstat) ===")
    print(f"{'Drv':<4}{'Total':>12}{'Backed':>12}{'Excluded':>11}{'Auth':>12}{'Diff':>11}{'Backed GB':>12}")
    print("-" * 74)
    tot_backed = 0
    for r in ov["rows"]:
        tot_backed += r["backed_files"]
        diff = f"{r['diff']:+,}" if r["diff"] is not None else "n/a"
        print(f"{r['drive']+':':<4}{r['total']:>12,}{r['backed_files']:>12,}{r['excluded_files']:>11,}"
              f"{_fmt(r['auth_files']):>12}{diff:>11}{_gb(r['backed_bytes']):>12.1f}")
    print("-" * 74)
    print(f"{'ALL':<4}{'':>12}{tot_backed:>12,}{'':>11}{_fmt(ov['auth_grand_files']):>12}"
          f"{(tot_backed - ov['auth_grand_files']):>+11,}")
    print("\n(Computed C:/E: may run a little HIGH if any mandatory PATTERN rule is unmatched - "
          "iterate rules.py until the diffs are small.)\n")

    # ---- spot checks ----
    res = queries.search(query="aeroinsta", limit=5)
    print(f"search 'aeroinsta': {res['total']} hits (showing {len(res['rows'])})")
    for row in res["rows"]:
        print(f"   [{'EXCL' if row['excluded'] else 'back'}] {_gb(row['size']):.2f}G  {row['full_path']}")

    exc = queries.exclusions()
    print(f"\ntop exclusion impact (of {len(exc['impact'])} rules, {len(exc['dormant_folders'])} dormant folders):")
    for i in exc["impact"][:8]:
        print(f"   {_gb(i['bytes']):>8.1f}G  {i['files']:>9,}  {i['rule']}")

    q = queries.queue()
    print(f"\nupload queue: {q['total_files']:,} files / {_gb(q['total_bytes']):.2f} GB")

    h = queries.health()
    print(f"\nhealth: {'OK' if h['ok'] else str(len(h['issues'])) + ' issue(s)'}")
    for i in h["issues"]:
        print("   - " + i)

    s = queries.sessions(limit=4)
    print(f"\nrecent sessions ({len(s['sessions'])} shown of history):")
    for e in s["sessions"][:4]:
        print(f"   {e['ts']}  {e['kind']}  files={e['files']}  {_gb(e['bytes']):.2f}G")

    print(f"\ntotal harness time: {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
