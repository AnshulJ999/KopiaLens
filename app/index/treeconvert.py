"""One-off conversion of the SQLite walk cache into the tree ( section 10).

For an install that already has a warm walk cache, this produces layer 1 without asking kopia
for anything: the cache already holds exactly the folder listings the tree wants. On repo 1 that
is the difference between a few minutes and re-reading 1.4 million folder objects.

It is deliberately NOT shipped as an app feature. A new install builds its tree from kopia the
normal way; this exists so an existing install does not pay for the move.

Two rules it follows:

  * Read the cache through SQLite, read-only, never as a raw file copy. The cache is a live WAL
    database - a byte copy can catch it mid-transaction and silently lose recent rows.
  * Convert EVERYTHING in the cache, then let the merge's GC drop what no current snapshot
    reaches. The alternative - walking in Python to find the reachable set first - is the very
    thing this refactor exists to delete, and the cache is roughly half dead weight anyway
    (repo 1: 860,519 live of 1.43M rows), so the GC is doing useful work either way.
"""

from __future__ import annotations

import os
import sqlite3
import time

from config import CACHEDIR

from ..core.applog import get_logger
from . import treesync

log = get_logger("tree")

ROWS = 4000          # sqlite rows read per fetch
# Batches are capped by BYTES, not row count. Cached listings range from a few bytes to 2.6 MB
# (repo 2, measured), so 4,000 rows was anywhere between trivial and a 367 MB blob - which is
# what actually ran the converter out of memory. Rows are a useless unit here.
MAX_BATCH_BYTES = 32 * 1024 * 1024
CHECKPOINT_EVERY = 50_000            # folders between checkpoints during a long ingest


def cache_path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"repo{repo}.db")


def _open_ro(path: str) -> sqlite3.Connection:
    """Read-only, but NOT immutable: the cache has a live WAL and immutable=1 would hide it."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA mmap_size=2147483648")
    return db


def _text(value) -> str:
    """The cached value as its JSON text, whichever of the two formats the row is in.

    Rows are TEXT (plain JSON) or BLOB (zstd) and a cache is permanently allowed to hold both -
    sqlite's dynamic typing IS the format discriminator. Decompression has to happen in Python
    because DuckDB cannot read a zstd frame out of a column, but no JSON is parsed here: the
    stored value is already the entry ARRAY, so it only needs wrapping.
    """
    if isinstance(value, (bytes, bytearray)):
        import zstandard
        return zstandard.ZstdDecompressor().decompress(bytes(value)).decode("utf-8")
    return value


def convert(con, repo: str, *, progress=None, alloc=None,
            segment_folders: int = 50_000) -> dict:
    """Fill ``_sync_ent`` / ``_sync_part`` / ``_fetched`` from the walk cache.

    Leaves the connection in exactly the state ``treesync.sync`` would have left it in, so the
    rest of the pipeline (materialize, reach, merge) is the shipped code, not a parallel path.
    """
    path = cache_path(repo)
    if not os.path.exists(path):
        raise FileNotFoundError(f"no walk cache for repo {repo} at {path}")
    started = time.perf_counter()
    db = _open_ro(path)
    treesync._shape_table(con)
    con.execute("CREATE OR REPLACE TABLE _sync_ent AS SELECT * FROM _ent_shape WHERE false")
    con.execute("DROP TABLE IF EXISTS _sync_part")
    con.execute("CREATE TABLE _sync_part (iobj VARCHAR, part VARCHAR, ord INTEGER)")
    con.execute("CREATE OR REPLACE TABLE _fetched (oid VARCHAR)")

    n_dirs = db.execute("SELECT count(*) FROM dir_children").fetchone()[0]
    n_ind = db.execute("SELECT count(*) FROM indirect").fetchone()[0]
    log.info("repo=%s converting walk cache: %d folders, %d split objects", repo, n_dirs, n_ind)

    def _flush(oids: list[str], texts: list[str]) -> None:
        # The stored value is the entries ARRAY; the shared parser reads kopia's object shape,
        # so wrap it. String concatenation only - nothing is parsed twice.
        blob = "\n".join('{"entries":' + t + "}" for t in texts)
        treesync._lines_table(con, treesync._split_lines(blob), "_lines")
        answered = con.execute("SELECT count(*) FROM _lines").fetchone()[0]
        if answered != len(oids):
            # A cached listing contained a newline, so POSITION no longer pairs answers to
            # objects and nothing in this batch can be trusted. Skip the batch rather than abort
            # the migration: those folders simply stay unknown, and the sync reads them from
            # kopia straight afterwards. Slower for this batch, correct, and the run survives.
            log.warning("repo=%s skipping a batch of %d cached folders: %d line(s) came back, so "
                        "the positional pairing is unsafe - they will be read from kopia instead",
                        repo, len(oids), answered)
            return
        treesync._register_oids(con, oids, "_oids")
        treesync._entries_from_lines(con, "_lines", "_oids", "_ent_new")
        con.execute("INSERT INTO _sync_ent SELECT * FROM _ent_new")
        treesync._register_oids(con, oids, "_read_ok")
        con.execute("INSERT INTO _fetched SELECT oid FROM _read_ok")

    done = 0
    last_seg = [0]
    pend_o: list[str] = []
    pend_t: list[str] = []
    pend_b = 0
    cur = db.execute("SELECT oid, children FROM dir_children")
    while True:
        rows = cur.fetchmany(ROWS)
        if not rows:
            break
        for oid, value in rows:
            text = _text(value)
            pend_o.append(oid)
            pend_t.append(text)
            pend_b += len(text)
            if pend_b >= MAX_BATCH_BYTES:
                _flush(pend_o, pend_t)
                done += len(pend_o)
                pend_o, pend_t, pend_b = [], [], 0
                if alloc is not None and done - last_seg[0] >= segment_folders:
                    # Fold this segment into the tree tables and drop its raw rows, so memory
                    # tracks the segment size rather than the size of the repository.
                    last_seg[0] = done
                    treesync.materialize(con, alloc, base_known="tdir", segment=True)
                    con.execute("CHECKPOINT")
                if progress:
                    progress(done=done, total=n_dirs)
    if pend_o:
        _flush(pend_o, pend_t)
        done += len(pend_o)
        if progress:
            progress(done=done, total=n_dirs)
    if alloc is not None:
        treesync.materialize(con, alloc, base_known="tdir", segment=True)

    def _flush_parts(oids: list[str], texts: list[str]) -> None:
        # The index object itself is a piece (ordinal 0), exactly as the live sync records it.
        con.execute("CREATE OR REPLACE TEMP TABLE _seed (iobj VARCHAR, part VARCHAR)")
        con.executemany("INSERT INTO _seed VALUES (?, ?)", [(o, o.lstrip("I")) for o in oids])
        con.execute("INSERT INTO _sync_part SELECT iobj, part, 0 FROM _seed")
        # The cached piece list is an array of chunk id STRINGS, not kopia's {"o": ...} objects -
        # walk.py stored the extracted ids. Parsed once into its own table, same reason as above.
        treesync._lines_table(con, treesync._split_lines(
            "\n".join('{"entries":' + t + "}" for t in texts)), "_lines")
        treesync._register_oids(con, oids, "_oids")
        con.execute("""CREATE OR REPLACE TEMP TABLE _pparsed AS
            SELECT o.oid, from_json(l.line, '{"entries":["VARCHAR"]}').entries AS es
            FROM _lines l JOIN _oids o ON o.pos = l.pos""")
        con.execute("""INSERT INTO _sync_part
            SELECT q.oid, x.p, x.i FROM _pparsed q,
            LATERAL (SELECT unnest(q.es) AS p, generate_subscripts(q.es, 1) AS i) x
            WHERE x.p IS NOT NULL""")
        con.execute("DROP TABLE _pparsed")

    pend_o, pend_t, pend_b = [], [], 0
    cur = db.execute("SELECT oid, parts FROM indirect")
    while True:
        rows = cur.fetchmany(ROWS)
        if not rows:
            break
        for oid, value in rows:
            text = _text(value)
            pend_o.append(oid)
            pend_t.append(text)
            pend_b += len(text)
            if pend_b >= MAX_BATCH_BYTES:
                _flush_parts(pend_o, pend_t)
                pend_o, pend_t, pend_b = [], [], 0
    if pend_o:
        _flush_parts(pend_o, pend_t)
    if alloc is not None:
        treesync.materialize(con, alloc, base_known="tdir", segment=True)
    db.close()

    # Read the counts from the tree tables, not the raw buffers: with segmenting on those have
    # already been folded in and cleared, so reading them reports zero for a full conversion.
    seg = alloc is not None
    stats = {
        "cached_folders": n_dirs,
        "cached_split_objects": n_ind,
        "entries": con.execute(
            "SELECT count(*) FROM %s" % ("_tent_new" if seg else "_sync_ent")).fetchone()[0],
        "parts": con.execute(
            "SELECT count(*) FROM %s" % ("_tpart_new" if seg else "_sync_part")).fetchone()[0],
        "seconds": round(time.perf_counter() - started, 1),
    }
    log.info("repo=%s walk cache converted: %s", repo, stats)
    return stats
