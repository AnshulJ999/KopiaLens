"""Build the KopiaLens DuckDB index from the CLI, with live progress.

Usage (run from the kopialens/ folder):
    python reindex.py            # both repos (1 and 2)
    python reindex.py 1          # just repo 1
    python reindex.py 2

IMPORTANT - QUIT THE KOPIALENS APP FIRST (tray -> Quit, or close the console window).
While it runs, the app holds the DuckDB file open read-write AND binds the kopia-server
ports 51831/51832 - this script needs both, so they would collide. This script starts
its own kopia servers (for the snapshot listing), builds the index, then shuts them
down. Restart the app afterwards; it will read the freshly-built index.
"""
import sys
import time

from app.index import builder
from app.index.schema import close_all
from app.core.server import SERVERS
from app.core.sources import sources_index

repos = [a for a in sys.argv[1:] if a in ("1", "2")] or ["1", "2"]

for repo in repos:
    srcs = sources_index(repo)
    total = sum(len(v) for v in srcs.values())
    print(f"\n=== repo {repo}: {len(srcs)} sources, {total} snapshots -> building (delta, from warm cache) ===", flush=True)
    t0 = time.time()
    builder.start_build(repo)
    last = -1
    while True:
        st = builder.index_status(repo)
        cur = builder._JOBS.get(repo, {}).get("current_source", "")
        idx = st.get("indexed_snapshots", 0) or 0
        if idx != last:
            print(f"  {idx}/{total} snapshots indexed  | now: {cur}", flush=True)
            last = idx
        if not st.get("building"):
            break
        time.sleep(3)
    err = builder._JOBS.get(repo, {}).get("error")
    final = builder.index_status(repo)
    print(f"=== repo {repo} DONE in {time.time()-t0:.0f}s | snapshots indexed={final.get('indexed_snapshots')} "
          f"file rows={final.get('files')} | error={err} ===", flush=True)

SERVERS.shutdown()  # stop the kopia servers this script started (frees the ports)
close_all()         # close the DuckDB connection (frees the file)
print("\nAll done. Restart the KopiaLens app to use the new index.", flush=True)
