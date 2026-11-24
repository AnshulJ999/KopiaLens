"""Sync the object tree from kopia ( plan 4a step 1) - the writer for layer 1.

Replaces the one-folder-at-a-time Python walk. Each level of the tree is fetched in batches of
800 with ``kopia content show``, kopia's whole stdout is handed to DuckDB as ONE string, split
once, and parsed with ``from_json``; only the LEVEL loop is Python, never the folders inside it.
Measured against the previous pyarrow path: 4.2 s vs 7.3 s per 50k folders, at a third of the RAM.

Every invariant of the old walker is carried, and they are the reason this file is careful:

  * ``content show`` output carries NO object id. The only mapping from answer back to request
    is POSITION, and the command ABORTS THE WHOLE BATCH at the first unreadable object - printing
    the answers up to that point and exiting non-zero. So pairing by position is sound, but the
    unanswered TAIL must never be recorded as empty folders - nor written off, which is why the
    batch retries past the one dead object instead of losing everything behind it ().
  * A transient failure must never become permanent state. An object we could not read is left
    out of the tree entirely and reported, so the next run retries it. Writing it down as "empty"
    is how 492,587 file rows once went missing.
  * Indirect DIRECTORIES (``Ik...``) need ``kopia show``, one serial call each - ``content show``
    errors on them. Indirect FILES (``Ix...``) are split objects: strip the leading ``I`` and
    ``content show`` the rest to get the piece list, and the index object itself counts as a piece.
    A file large enough that its piece list was split again (``IIx...``) is read level by level
    in ``split_pieces``.
  * A short batch leaves its sources marked incomplete so they rebuild next run (), rather
    than being recorded as fully indexed and then skipped forever.
"""

from __future__ import annotations

import contextlib
import json
import os
import time

from ..core.applog import get_logger
from ..core.kopia import kopia
from . import ids, perf, tree

log = get_logger("tree")


@contextlib.contextmanager
def timed(label: str, out: dict | None = None):
    """Time ONE statement and log it.

    Per-phase timing is not enough. The merge was a single opaque 18-minute log line for four
    reviews, and the nested-loop join inside it was invisible because nothing said which of its
    statements the time went to. Anything here that touches the whole tree gets its own line.
    """
    t0 = time.perf_counter()
    try:
        yield
    finally:
        secs = round(time.perf_counter() - t0, 1)
        if out is not None:
            out[label] = secs
        if secs >= 1.0:
            log.info("  %-22s %7.1fs", label, secs)

BATCH = 800                     # kopia CLI overhead is 0.42 s/call, so batching is near-perfect
MAX_SKIPS = 50                  # dead objects to step over in one batch before giving the rest up
DEFAULT_PARALLEL = 4            # kopia reads in flight while the tree is read (above 8 GB RAM)
_PARSE_SHARE = 16               # one parse gets at most this fraction of the DuckDB memory limit
_KNOWN = ("name", "type", "mode", "size", "mtime", "obj", "summ")
_SUMM_KNOWN = ("size", "files", "symlinks", "dirs", "maxTime", "numFailed", "incomplete",
               "numIgnoredErrors", "errors")

# from_json, not a CAST to STRUCT: a cast REFUSES an object missing a key ("does not have key
# size"), and directory entries legitimately have no size. from_json fills absent keys with NULL.
_SHAPE = '{"entries":["JSON"]}'


class SyncResult:
    """What one sync pass did, and what it could not read."""

    def __init__(self) -> None:
        self.fetched = 0            # folders newly read from kopia
        self.levels = 0
        self.unreadable: list[str] = []   # objects kopia refused; their subtrees are INCOMPLETE
        self.parts = 0
        self.indirect = 0           # indirect directories, each a serial `kopia show`
        self.seconds = 0.0

    @property
    def complete(self) -> bool:
        return not self.unreadable

    def as_dict(self) -> dict:
        return {"fetched": self.fetched, "levels": self.levels, "parts": self.parts,
                "unreadable": len(self.unreadable), "complete": self.complete,
                "seconds": round(self.seconds, 1)}


def _split_lines(stdout: str) -> list[str]:
    """kopia's stdout -> its non-blank lines, in order, counted the way ``_answered`` counts."""
    return [ln.strip("\r") for ln in (stdout or "").split("\n") if ln.strip()]


def _chunks(lines: list[str], budget: int):
    """(start, lines) runs of consecutive lines holding at most ``budget`` characters; a longer
    single line stands alone. Bounds what one parse holds in memory, which cannot spill."""
    start, size = 0, 0
    for i, ln in enumerate(lines):
        if i > start and size + len(ln) > budget:
            yield start, lines[start:i]
            start, size = i, 0
        size += len(ln)
    if start < len(lines):
        yield start, lines[start:]


def _parse_budget() -> int:
    """Characters of kopia output per parse: 1/16 of the DuckDB memory limit, at least 4 MB."""
    from .store import memory_limit_bytes
    return max(4 * 2**20, memory_limit_bytes() // _PARSE_SHARE)


def _lines_table(con, lines: list[str], table: str) -> None:
    """Lines -> a table numbered 1..n. ``pos`` is the ONLY mapping from a kopia answer back to
    the object it was requested for, so it is assigned here, never by a window function (whose
    order is not stable once ``preserve_insertion_order`` is off, as in the encrypted session)."""
    import pyarrow as pa
    con.register("_line_arrow", pa.table({"pos": pa.array(range(1, len(lines) + 1), pa.int64()),
                                          "line": pa.array(lines, pa.string())}))
    con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT pos, line FROM _line_arrow")
    con.unregister("_line_arrow")


def _entries_from_lines(con, lines: str, oid_table: str, out: str) -> None:
    """Parse the fetched lines into raw entry rows, paired to their request by POSITION.

    ``extra`` holds the whole original entry JSON whenever the entry is not fully representable
    by the typed columns - an unknown key, an unknown type/mode, an mtime that is not in kopia's
    format. That keeps the round trip lossless without needing a per-key reconstruction, and it
    stays NULL for the overwhelming majority of entries.
    """
    known = ", ".join("'%s'" % k for k in _KNOWN)
    summ_known = ", ".join("'%s'" % k for k in _SUMM_KNOWN)
    types = ", ".join("'%s'" % t for t in tree.TYPES)
    modes = ", ".join("'%s'" % m for m in tree.MODES)
    mt = tree.ts_to_ns("json_extract_string(e, '$.mtime')")
    smt = tree.ts_to_ns("json_extract_string(e, '$.summ.maxTime')")
    # Parse ONCE, into its own table. Referencing from_json twice in the query below (unnest and
    # generate_subscripts both need the list) parses every line twice, and folder listings run to
    # 2.6 MB each - the same trap the plan records for string_split on a large parameter, where
    # one extra evaluation turned 4 s into 142 s. Here it turned a batch into an out-of-memory.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _parsed AS
        SELECT o.oid AS parent_oid, from_json(l.line, '{_SHAPE}').entries AS es
        FROM {lines} l JOIN {oid_table} o ON o.pos = l.pos""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE {out} AS
        SELECT p.parent_oid,
               x.i AS pos,
               json_extract_string(e, '$.name') AS name,
               json_extract_string(e, '$.type') AS etype_s,
               json_extract_string(e, '$.mode') AS mode_s,
               TRY_CAST(json_extract_string(e, '$.size') AS BIGINT) AS size,
               {mt} AS mtime,
               json_extract_string(e, '$.obj') AS obj,
               TRY_CAST(json_extract_string(e, '$.summ.size') AS BIGINT) AS s_size,
               TRY_CAST(json_extract_string(e, '$.summ.files') AS BIGINT) AS s_files,
               TRY_CAST(json_extract_string(e, '$.summ.symlinks') AS BIGINT) AS s_symlinks,
               TRY_CAST(json_extract_string(e, '$.summ.dirs') AS BIGINT) AS s_dirs,
               {smt} AS s_maxtime,
               TRY_CAST(json_extract_string(e, '$.summ.numFailed') AS BIGINT) AS s_numfailed,
               json_extract_string(e, '$.summ.incomplete') AS s_incomplete,
               TRY_CAST(json_extract_string(e, '$.summ.numIgnoredErrors') AS BIGINT)
                   AS s_numignorederrors,
               CASE WHEN json_extract(e, '$.summ.errors') IS NOT NULL
                    THEN json_extract(e, '$.summ.errors')::VARCHAR END AS s_errors,
               CASE WHEN
                     len(list_filter(json_keys(e), k -> k NOT IN ({known}))) > 0
                  OR json_extract_string(e, '$.type') NOT IN ({types})
                  OR (json_extract_string(e, '$.mode') IS NOT NULL
                      AND json_extract_string(e, '$.mode') NOT IN ({modes}))
                  OR (json_extract_string(e, '$.mtime') IS NOT NULL AND {mt} IS NULL)
                  OR (json_extract(e, '$.summ') IS NOT NULL
                      AND len(list_filter(json_keys(json_extract(e, '$.summ')),
                                          k -> k NOT IN ({summ_known}))) > 0)
                  OR (json_extract_string(e, '$.summ.maxTime') IS NOT NULL AND {smt} IS NULL)
                 THEN e::VARCHAR END AS extra
        FROM _parsed p,
        LATERAL (SELECT unnest(p.es) AS e, generate_subscripts(p.es, 1) AS i) x""")
    con.execute("DROP TABLE _parsed")


def _register_oids(con, oids: list[str], table: str) -> None:
    import pyarrow as pa
    con.register("_oid_arrow", pa.table({"pos": pa.array(range(1, len(oids) + 1), pa.int64()),
                                         "oid": pa.array(oids, pa.string())}))
    con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT pos, oid FROM _oid_arrow")
    con.unregister("_oid_arrow")


def _skip_dead(repo: str, pending: list[str], answered: int, res, result: SyncResult,
               skips: int, what: str) -> tuple[list[str], int]:
    """Step over the ONE object that aborted the batch and return what is left to retry.

    ``content show`` stops at the first object it cannot read, so the object at position
    ``answered`` is the dead one and everything after it simply never got asked. Recording the
    whole tail as unreadable (what this did before) is permanent: the ids are fetched in
    a stable order, so every later run aborts in the same place and those objects are never
    priced. Skipping one per attempt converges at one extra kopia call per genuinely dead object.

    ``MAX_SKIPS`` bounds the case this cannot distinguish from a dead object: kopia failing
    wholesale answers nothing every time, and without a cap that would be one call per object in
    the batch.
    """
    dead = pending[answered]
    result.unreadable.append(dead)
    log.warning("repo=%s content show answered %d of %d %ss (rc=%d): %s - %s is unreadable, "
                "retrying the %d behind it", repo, answered, len(pending), what, res.returncode,
                (res.stderr or "").strip()[:200], dead, len(pending) - answered - 1)
    rest = pending[answered + 1:]
    if skips + 1 >= MAX_SKIPS and rest:
        result.unreadable.extend(rest)
        log.warning("repo=%s gave up on this batch after %d unreadable %ss - %d object(s) left "
                    "unread, NOT recorded as empty", repo, skips + 1, what, len(rest))
        return [], skips + 1
    return rest, skips + 1


def kopia_parallel() -> int:
    """How many kopia reads run at once while the tree is read: the setting, else 4 (2 at 8 GB of
    RAM or less - each kopia process holds ~170 MB). Measured on repo 2: 1 -> 4 halves the tree
    read; 8 adds nothing because parsing on this side becomes the limit."""
    try:
        from ..settings import load_settings
        n = int(load_settings()["kopialens"].get("index_kopia_parallel") or 0)
    except Exception:  # noqa: BLE001 - a settings problem must not stop an index
        n = 0
    if not n:
        from ..core.host import ram_bytes
        ram = ram_bytes()   # 0 when unknown: the cautious default
        n = 2 if ram <= 8.5 * 2**30 else DEFAULT_PARALLEL
    return max(1, min(n, 16))


def _answered(stdout: str) -> int:
    """Non-blank lines, counted the way ``_split_lines`` counts them."""
    return sum(1 for ln in (stdout or "").split("\n") if ln.strip())


def _read_batch(repo: str, batch: list[str], result: SyncResult, what: str,
                to_arg=lambda o: o) -> list[tuple[list[str], str]]:
    """``content show`` one batch, stepping over dead objects. Returns (answered ids, stdout)
    pieces in order. Runs on a worker thread when reads are parallel: kopia only, no DuckDB."""
    pieces: list[tuple[list[str], str]] = []
    pending, skips, checked = batch, 0, False
    while pending:
        res = kopia(repo, ["content", "show"] + [to_arg(o) for o in pending])
        answered = _answered(res.stdout)
        if answered:
            pieces.append((pending[:answered], res.stdout))
        if answered >= len(pending):
            break
        if not answered and res.returncode != 0 and _CTX and not checked:
            if _wait_until_reachable(repo):
                continue    # the repository was away, not the object: ask again
            checked = True  # it answers: the failure belongs to the objects
        pending, skips = _skip_dead(repo, pending, answered, res, result, skips, what)
    return pieces


_WAIT_START, _WAIT_MAX = 15, 300     # seconds between reachability checks while waiting


def _wait_limit() -> float:
    """Settings > Advanced: how long a repository may stay unreachable before the run gives up."""
    try:
        from ..settings import load_settings
        return max(60.0, float(load_settings()["kopialens"].get("index_wait_unreachable") or 1800))
    except Exception:  # noqa: BLE001 - a settings problem must not stop an index
        return 1800.0


def _wait_until_reachable(repo: str) -> bool:
    """kopia answered nothing. True after waiting out an unreachable repository (retry the batch);
    False when the repository answers, so the failure belongs to the object. Past the wait limit
    it raises ``GaveUpWaiting``: the read so far is saved and the next run continues from it."""
    since, delay, limit = None, _WAIT_START, _wait_limit()
    while True:
        if kopia(repo, ["repository", "status", "--json"]).returncode == 0:
            if since is not None:
                log.info("repo=%s reachable again after %.0fs", repo, time.time() - since)
            return since is not None
        if since is None:
            since = time.time()
            log.warning("repo=%s the repository can't be reached - waiting for it", repo)
        waited = time.time() - since
        if waited >= limit:
            log.warning("repo=%s unreachable for %.0f min - giving up this run; the read so far is "
                        "saved and the next run continues from it", repo, waited / 60)
            raise GaveUpWaiting(f"the repository couldn't be reached for {waited / 60:.0f} min")
        waiting = _CTX.get("waiting")
        if waiting:
            waiting(waiting_since=since)
        end = time.monotonic() + delay
        while time.monotonic() < end:
            stop = _CTX.get("should_stop")
            if stop and stop():
                raise StopRequested()
            time.sleep(min(5.0, max(0.0, end - time.monotonic())))
        delay = min(delay * 2, _WAIT_MAX)


def _in_order(fn, items: list, workers: int):
    """Yield ``fn(item)`` in input order, with at most ``workers`` calls in flight."""
    if workers <= 1 or len(items) <= 1:
        for item in items:
            yield fn(item)
        return
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kopia-read") as pool:
        it = iter(items)
        window: deque = deque(pool.submit(fn, item) for _, item in zip(range(workers), it))
        while window:
            done = window.popleft().result()
            for nxt in it:
                window.append(pool.submit(fn, nxt))
                break
            yield done


def _fetch_dirs(con, repo: str, oids: list[str], result: SyncResult, out: str,
                tick=lambda done: None) -> None:
    """Fetch one level's folders and append their entries to ``out``.

    Normal objects go through batched ``content show``; indirect directories need ``kopia show``
    one at a time. Both paths refuse to record a failed read. kopia calls may run in parallel;
    parsing stays on this thread, in request order. ``tick(done)`` reports progress within the
    level (one level of a big repository can take hours).
    """
    normal = [o for o in oids if not o.startswith("I")]
    indirect = [o for o in oids if o.startswith("I")]
    con.execute(f"CREATE TABLE IF NOT EXISTS {out} AS SELECT * FROM _ent_shape WHERE false")
    workers = kopia_parallel()

    def _read(group: list[str]) -> None:
        """Record folders we really did read. An EMPTY folder produces no entry rows, so a tree
        built from the entries alone silently loses every empty directory - it then looks
        unknown and is re-fetched from kopia on every single run."""
        if group:
            _register_oids(con, group, "_read_ok")
            con.execute("INSERT INTO _fetched SELECT oid FROM _read_ok")

    budget = _parse_budget()
    batches = [normal[i:i + BATCH] for i in range(0, len(normal), BATCH)]
    done = 0
    for batch_no, pieces in enumerate(_in_order(lambda b: _read_batch(repo, b, result, "folder"),
                                                batches, workers)):
        for got, stdout in pieces:
            lines = _split_lines(stdout)
            got = got[:len(lines)]
            if not got:
                continue
            for first, chunk in _chunks(lines[:len(got)], budget):
                _lines_table(con, chunk, "_lines")
                _register_oids(con, got[first:first + len(chunk)], "_oids")
                _entries_from_lines(con, "_lines", "_oids", "_ent_new")
                con.execute(f"INSERT INTO {out} SELECT * FROM _ent_new")
            _read(got)
            result.fetched += len(got)
        done += len(batches[batch_no])
        tick(done)

    for i, (oid, res) in enumerate(_in_order(lambda o: (o, kopia(repo, ["show", o])), indirect, workers)):
        if i % 50 == 49:
            tick(done + i + 1)
        if res.returncode != 0:
            result.unreadable.append(oid)
            log.warning("repo=%s kopia show %s failed (rc=%d): %s - left unread",
                        repo, oid, res.returncode, (res.stderr or "").strip()[:200])
            continue
        lines = _split_lines(res.stdout)
        if len(lines) != 1:
            result.unreadable.append(oid)
            log.warning("repo=%s kopia show %s returned no usable object - left unread", repo, oid)
            continue
        _lines_table(con, lines, "_lines")
        _register_oids(con, [oid], "_oids")
        _entries_from_lines(con, "_lines", "_oids", "_ent_new")
        con.execute(f"INSERT INTO {out} SELECT * FROM _ent_new")
        _read([oid])
        result.fetched += 1


def split_pieces(repo: str, obj: str) -> list[str] | None:
    """Every piece of a split object, index pieces first and the top index content at 0.

    kopia adds one ``I`` per level of splitting. The top content lists the pieces of the level
    below it, and ``kopia show`` with one ``I`` fewer than the object reassembles each lower
    level's list, so this is one kopia call per level. None when kopia could not read it.
    """
    n = len(obj) - len(obj.lstrip("I"))
    top = obj[n:]
    pieces = [top]
    for level in range(n):
        args = ["content", "show", top] if level == 0 else ["show", "I" * level + top]
        res = kopia(repo, args)
        entries = None
        if res.returncode == 0:
            try:
                entries = json.loads(res.stdout).get("entries") or []
            except ValueError:
                pass
        if entries is None:
            log.warning("repo=%s could not read the piece list of %s (rc=%d): %s", repo, obj,
                        res.returncode, (res.stderr or "").strip()[:200])
            return None
        pieces += [e["o"] for e in entries if e.get("o")]
    return pieces


def _fetch_parts(con, repo: str, iobjs: list[str], result: SyncResult, out: str,
                 tick=lambda done: None) -> None:
    """Piece lists of split objects. ``Ix...`` -> ``content show x...``; the index object itself
    is a piece, so it leads the list and its ordinal is 0. Objects split more than once are read
    one at a time with ``split_pieces``."""
    import pyarrow as pa
    con.execute(f"""CREATE TABLE IF NOT EXISTS {out} (
        iobj VARCHAR, part VARCHAR, ord INTEGER)""")
    multi = [o for o in iobjs if o.startswith("II")]
    iobjs = [o for o in iobjs if not o.startswith("II")]
    # the object's own index content always counts
    con.register("_seed_arrow", pa.table({"iobj": pa.array(iobjs, pa.string()),
                                          "part": pa.array([o[1:] for o in iobjs], pa.string())}))
    con.execute(f"INSERT INTO {out} SELECT iobj, part, 0 FROM _seed_arrow")
    con.unregister("_seed_arrow")
    batches = [iobjs[i:i + BATCH] for i in range(0, len(iobjs), BATCH)]

    def read(batch: list[str]):
        return batch, _read_batch(repo, batch, result, "split object", to_arg=lambda o: o[1:])

    budget = _parse_budget()
    done = 0
    for batch, pieces in _in_order(read, batches, kopia_parallel()):
        done += len(batch)
        tick(done)
        answered_all: set[str] = set()
        for got, stdout in pieces:
            lines = _split_lines(stdout)
            got = got[:len(lines)]
            if not got:
                continue
            answered_all.update(got)
            for first, chunk in _chunks(lines[:len(got)], budget):
                _lines_table(con, chunk, "_lines")
                _register_oids(con, got[first:first + len(chunk)], "_oids")
                # Parse ONCE into its own table, for the same reason _entries_from_lines does:
                # referencing from_json twice parses every line twice.
                con.execute(f"""CREATE OR REPLACE TEMP TABLE _pparsed AS
                    SELECT o.oid, from_json(l.line, '{_SHAPE}').entries AS es
                    FROM _lines l JOIN _oids o ON o.pos = l.pos""")
                con.execute(f"""INSERT INTO {out}
                    SELECT p.oid, json_extract_string(x.e, '$.o'), x.i
                    FROM _pparsed p,
                    LATERAL (SELECT unnest(p.es) AS e, generate_subscripts(p.es, 1) AS i) x
                    WHERE json_extract_string(x.e, '$.o') IS NOT NULL""")
                con.execute("DROP TABLE _pparsed")
        # Drop the seed rows of the objects we gave up on: a piece list of only the index
        # object would undercount that file's physical size forever.
        gone = sorted(set(batch) - answered_all)
        if gone:
            con.execute(f"DELETE FROM {out} WHERE iobj IN ({','.join('?' * len(gone))})", gone)
    rows: dict[str, list] = {"iobj": [], "part": [], "ord": []}
    for oid, pieces in _in_order(lambda o: (o, split_pieces(repo, o)), multi, kopia_parallel()):
        done += 1
        tick(done)
        if pieces is None:
            result.unreadable.append(oid)
            continue
        rows["iobj"] += [oid] * len(pieces)
        rows["part"] += pieces
        rows["ord"] += range(len(pieces))
    if rows["iobj"]:
        con.register("_multi_arrow", pa.table({"iobj": pa.array(rows["iobj"], pa.string()),
                                               "part": pa.array(rows["part"], pa.string()),
                                               "ord": pa.array(rows["ord"], pa.int32())}))
        con.execute(f"INSERT INTO {out} SELECT iobj, part, ord FROM _multi_arrow")
        con.unregister("_multi_arrow")
    result.parts = con.execute(f"SELECT count(*) FROM {out}").fetchone()[0]


def _shape_table(con) -> None:
    """One empty typed template so every level's INSERT matches without repeating the column list."""
    con.execute("""CREATE OR REPLACE TABLE _ent_shape (
        parent_oid VARCHAR, pos BIGINT, name VARCHAR, etype_s VARCHAR, mode_s VARCHAR,
        size BIGINT, mtime BIGINT, obj VARCHAR, s_size BIGINT, s_files BIGINT,
        s_symlinks BIGINT, s_dirs BIGINT, s_maxtime BIGINT, s_numfailed BIGINT,
        s_incomplete VARCHAR, s_numignorederrors BIGINT, s_errors VARCHAR, extra VARCHAR)""")


class StopRequested(Exception):
    """A stop was asked for: the read so far is saved and the next run continues from it."""


class GaveUpWaiting(StopRequested):
    """The repository stayed unreachable past the wait limit. Saved like a stop, but the run
    counts as failed, so it is retried as usual and the next repository can index."""


# Seconds between save points inside a level (the variable exists for resume tests only).
SAVE_EVERY = int(os.environ.get("KOPIALENS_INDEX_SAVE_EVERY") or 900)
CHUNK = BATCH * 25               # folders read between flushes inside a level

# The running sync's stop check and progress hook, for the kopia-reading threads.
_CTX: dict = {}


def _working_tables(con, known_table: str | None) -> None:
    con.execute("CREATE OR REPLACE TABLE _sync_ent AS SELECT * FROM _ent_shape WHERE false")
    con.execute("DROP TABLE IF EXISTS _sync_part")
    con.execute("CREATE TABLE _sync_part (iobj VARCHAR, part VARCHAR, ord INTEGER)")
    con.execute("CREATE OR REPLACE TABLE _seen (oid VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE _fetched (oid VARCHAR)")
    # Split objects are collected as they are seen, because with segmenting on, the entry rows
    # that revealed them are cleared before the piece-list pass runs.
    con.execute("CREATE OR REPLACE TABLE _iobj_all (oid VARCHAR)")
    # The level being read (_frontier), what of it is read (_level_done), the next level found so
    # far (_next) and folders kopia would not read (_unread): the state a resumed read continues.
    con.execute("CREATE OR REPLACE TABLE _frontier (oid VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE _level_done (oid VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE _next (oid VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE _unread (oid VARCHAR)")
    # The accumulators must exist before the piece-list pass below anti-joins them, and a fully
    # carried tree never enters the level loop (so materialize, which creates them, never runs).
    _new_tables(con)
    if known_table:
        con.execute(f"INSERT INTO _seen SELECT oid FROM {known_table}")


def sync(con, repo: str, roots: list[str], *, known_table: str | None = None,
         progress=None, max_levels: int = 512, alloc: ids.Allocator | None = None,
         resume: dict | None = None, save=None, should_stop=None) -> SyncResult:
    """Fetch every folder reachable from ``roots`` that the tree does not already hold.

    Leaves ``_sync_ent`` (raw entry rows) and ``_sync_part`` (raw piece rows) in the connection
    for ``materialize`` to turn into tree tables. ``known_table`` names a table of object ids
    already in the tree (carried from the previous generation) so an unchanged subtree is never
    re-read - that is what makes a nightly cheap.

    Pass ``alloc`` to materialize as the read goes instead of holding every entry until the end.
    A nightly reads a handful of folders and does not care; a FIRST index of a large repository
    does, and this is what keeps it inside the 8 GB floor.

    Level by level, because each level's folders are only discovered by reading the one above.
    Within a level everything is one anti-join and a handful of batched kopia calls. A level is
    read in chunks, each flushed in one transaction, so ``save(state)`` can be called at any
    flush and ``resume`` (that state) continues the same connection where it stopped. A stop
    (``should_stop()`` true) saves and raises ``StopRequested``.
    """
    started = time.perf_counter()
    result = SyncResult()
    _shape_table(con)
    level = 0
    if resume is None:
        _working_tables(con, known_table)
    else:
        level = int(resume.get("level") or 0)
        result.fetched = int(resume.get("fetched") or 0)
        result.indirect = int(resume.get("indirect") or 0)
        result.unreadable = [r[0] for r in con.execute("SELECT oid FROM _unread").fetchall()]
    unread_saved = len(result.unreadable)

    import pyarrow as pa
    # A resumed read also picks up roots of snapshots taken since it stopped.
    con.register("_roots_arrow", pa.table({"oid": pa.array(sorted(set(roots)), pa.string())}))
    con.execute("INSERT INTO _frontier SELECT oid FROM _roots_arrow "
                "WHERE oid NOT IN (SELECT oid FROM _seen) AND oid NOT IN (SELECT oid FROM _frontier)")
    con.unregister("_roots_arrow")

    def flush() -> None:
        """Move what was read into the next level, the done list and (with ``alloc``) the tree,
        as one transaction: a hard kill leaves the state before or after it, never half."""
        nonlocal unread_saved
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("""INSERT INTO _next
                SELECT DISTINCT e.obj FROM _sync_ent e
                WHERE e.etype_s = 'd' AND e.obj IS NOT NULL
                  AND e.obj NOT IN (SELECT oid FROM _seen) AND e.obj NOT IN (SELECT oid FROM _next)""")
            con.execute("""INSERT INTO _iobj_all
                SELECT DISTINCT e.obj FROM _sync_ent e
                WHERE e.obj LIKE 'I%' AND e.etype_s <> 'd'
                  AND e.obj NOT IN (SELECT oid FROM _iobj_all)""")
            con.execute("INSERT INTO _level_done SELECT DISTINCT oid FROM _fetched "
                        "WHERE oid NOT IN (SELECT oid FROM _level_done)")
            new_unread = result.unreadable[unread_saved:]
            if new_unread:
                con.register("_unread_arrow", pa.table({"oid": pa.array(new_unread, pa.string())}))
                con.execute("INSERT INTO _unread SELECT oid FROM _unread_arrow")
                con.unregister("_unread_arrow")
            if alloc is not None:
                materialize(con, alloc, base_known="tdir", segment=True)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        unread_saved = len(result.unreadable)

    def state(phase: str = "levels") -> dict:
        return {"level": level, "fetched": result.fetched, "indirect": result.indirect,
                "levels": result.levels, "phase": phase}

    def stopping() -> bool:
        return bool(should_stop and should_stop())

    if progress:
        # This run's start, so the panel can show speed even after a resume.
        _report, _since, _read0 = progress, time.time(), result.fetched
        progress = lambda **kw: _report(since=_since, read0=_read0, **kw)  # noqa: E731

    def wait_tick(**kw) -> None:
        if progress:
            progress(read=result.fetched, at=time.time(), **kw)

    _CTX.update(should_stop=stopping, waiting=wait_tick)
    if resume is not None:
        flush()          # rows an interrupted step read but had not flushed
    last_save = time.monotonic()
    try:
        while True:
            if level >= max_levels:
                raise RuntimeError(f"repo {repo}: tree deeper than {max_levels} levels - refusing to loop")
            con.execute("INSERT INTO _seen SELECT oid FROM _frontier WHERE oid NOT IN (SELECT oid FROM _seen)")
            total = con.execute("SELECT count(*) FROM _frontier").fetchone()[0]
            if not total:
                break
            todo = [r[0] for r in con.execute(
                "SELECT oid FROM _frontier WHERE oid NOT IN (SELECT oid FROM _level_done) "
                "ORDER BY oid").fetchall()]
            result.levels = level + 1
            offset = total - len(todo)

            def _tick(done: int, _lvl: int = level + 1, _n: int = total, _off: int = offset) -> None:
                if progress:
                    progress(level=_lvl, level_folders=_n, level_read=_off + done,
                             read=result.fetched, at=time.time())
                if stopping():
                    raise StopRequested()
            t_lvl, (c_lvl, k_lvl) = time.perf_counter(), perf.kopia_now()
            if progress:
                progress(level=level + 1, level_folders=total, level_read=offset,
                         read=result.fetched, at=time.time())
            for i in range(0, len(todo), CHUNK):
                try:
                    _fetch_dirs(con, repo, todo[i:i + CHUNK], result, "_sync_ent",
                                tick=lambda d, _i=i: _tick(_i + d))
                except StopRequested:
                    flush()
                    if save:
                        save(state())
                    raise
                flush()
                if stopping():
                    if save:
                        save(state())
                    raise StopRequested()
                if save and time.monotonic() - last_save >= SAVE_EVERY:
                    save(state())
                    last_save = time.monotonic()
            t_read = time.perf_counter()
            con.execute("BEGIN TRANSACTION")
            con.execute("CREATE OR REPLACE TABLE _frontier AS SELECT oid FROM _next")
            con.execute("DELETE FROM _next")
            con.execute("DELETE FROM _level_done")
            con.execute("COMMIT")
            c_now, k_now = perf.kopia_now()
            indirect = sum(1 for o in todo if o.startswith("I"))
            result.indirect += indirect
            perf.level(level=level + 1, folders=len(todo), indirect=indirect,
                       kopia_calls=c_now - c_lvl, kopia_secs=round(k_now - k_lvl, 2),
                       read_secs=round(t_read - t_lvl, 2),
                       materialize_secs=round(time.perf_counter() - t_read, 2))
            level += 1
            if save:
                save(state())
                last_save = time.monotonic()
    finally:
        _CTX.clear()

    # Split objects the tree already references but holds no piece list for. The walk-cache seed
    # takes piece lists from the cache's `indirect` table, and this sync only collects split
    # objects from the folders it actually reads - so a split object inside an already-known
    # folder whose cache row is missing would never be expanded, and its file's physical size
    # would read as just its index object. That is the silent undercount this store exists to
    # remove (and the old walker's `expand_indirect(fetch=True)` covered it), so it is fetched
    # here. `cid_id` on an entry IS the I-object that `tpart.iobj_cid` names; `ind_cid` is the
    # stripped index content and matches nothing in the piece table.
    con.execute("""INSERT INTO _iobj_all
        SELECT DISTINCT c.content_id
        FROM (SELECT cid_id FROM tent WHERE ind AND cid_id IS NOT NULL
                AND cid_id NOT IN (SELECT iobj_cid FROM tpart)
              UNION
              SELECT cid_id FROM _tent_new WHERE ind AND cid_id IS NOT NULL
                AND cid_id NOT IN (SELECT iobj_cid FROM _tpart_new)) x
        JOIN cid_rev c ON c.cid_id = x.cid_id
        WHERE c.content_id IS NOT NULL
          AND c.content_id NOT IN (SELECT oid FROM _iobj_all)""")
    # Piece lists already in the tree (a resumed run) are not read again; an interrupted piece
    # read starts over.
    con.execute("DELETE FROM _sync_part")
    iobjs = [r[0] for r in con.execute(
        "SELECT oid FROM _iobj_all WHERE oid NOT IN "
        "(SELECT c.content_id FROM _tpart_new t JOIN cid_rev c ON c.cid_id = t.iobj_cid) "
        "ORDER BY oid").fetchall()]
    perf.count(split_files=len(iobjs))
    if iobjs:
        with perf.phase("tree/split-file pieces"):
            def _ptick(done: int) -> None:
                if progress:
                    progress(pieces=len(iobjs), pieces_read=done, read=result.fetched, at=time.time())
                if should_stop and should_stop():
                    if save:
                        save(state("pieces"))
                    raise StopRequested()
            _CTX.update(should_stop=stopping, waiting=wait_tick)
            try:
                _fetch_parts(con, repo, iobjs, result, "_sync_part", tick=_ptick)
            finally:
                _CTX.clear()
            if alloc is not None:
                con.execute("BEGIN TRANSACTION")
                try:
                    materialize(con, alloc, base_known="tdir", segment=True)
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
    perf.count(indirect_dirs=result.indirect)
    result.seconds = time.perf_counter() - started
    log.info("repo=%s tree sync: %s", repo, result.as_dict())
    return result


def _new_tables(con) -> None:
    """The accumulating output tables, created once and appended to by every segment."""
    con.execute("""CREATE TABLE IF NOT EXISTS _tdir_new (
        dir_id INTEGER, cid_id INTEGER, parent_oid VARCHAR, ind BOOLEAN)""")
    con.execute("""CREATE TABLE IF NOT EXISTS _tent_new (
        dir_id INTEGER, pos INTEGER, name VARCHAR, etype TINYINT, mode TINYINT,
        size BIGINT, mtime BIGINT, cid_id INTEGER, ind BOOLEAN, ind_cid INTEGER,
        child INTEGER, s_size BIGINT, s_files BIGINT, s_symlinks BIGINT, s_dirs BIGINT,
        s_maxtime BIGINT, s_numfailed BIGINT, s_incomplete VARCHAR,
        s_numignorederrors BIGINT, s_errors VARCHAR, extra VARCHAR)""")
    con.execute("""CREATE TABLE IF NOT EXISTS _tpart_new (
        iobj_cid INTEGER, part_cid INTEGER, ord INTEGER)""")


def clear_segment(con) -> None:
    """Drop the raw rows a segment consumed, so the next one starts from an empty buffer."""
    for t in ("_sync_ent", "_sync_part", "_fetched"):
        con.execute(f"DELETE FROM {t}")


def materialize(con, alloc: ids.Allocator, *, base_known: str | None = None,
                segment: bool = False) -> dict:
    """Turn the raw sync output into tree rows with integer object ids.

    Allocation goes through the generation's shared ``cid`` dictionary, so a folder object, a
    file's content and a split object's pieces all resolve to the same id space layer 3 uses.
    Ids are never renumbered, so an object keeps its id across generations.

    Output APPENDS to ``_tdir_new`` / ``_tent_new`` / ``_tpart_new``, which is what lets a big
    first index run in bounded memory: the caller materializes every so often and clears the raw
    buffer (``segment=True``) instead of holding every entry in the scratch until the end. Repo 2
    accumulated past 3 GB doing it in one go, which the 8 GB floor would not survive.
    """
    types = ", ".join("'%s'" % t for t in tree.TYPES)
    modes = ", ".join("'%s'" % m for m in tree.MODES)
    _new_tables(con)

    # Every object this sync saw, in one set: the folders themselves, their entries' objects, the
    # index object of every split file, and every piece. UNION ALL plus ONE dedup - plain UNION
    # between each pair would dedup four times over tens of millions of strings.
    con.execute("""CREATE OR REPLACE TABLE _obj_all AS
        SELECT DISTINCT content_id FROM (
            SELECT oid AS content_id FROM _fetched
            UNION ALL SELECT obj FROM _sync_ent WHERE obj IS NOT NULL
            UNION ALL SELECT ltrim(obj, 'I') FROM _sync_ent WHERE starts_with(obj, 'I')
            UNION ALL SELECT iobj FROM _sync_part
            UNION ALL SELECT part FROM _sync_part)
        WHERE content_id IS NOT NULL""")
    ids.anti_join(con, "_obj_new", "SELECT content_id FROM _obj_all",
                  "SELECT content_id FROM cid", ["content_id"])
    alloc.assign(con, "_obj_alloc", "SELECT content_id FROM _obj_new", "content_id",
                 "cid_id", id_col="cid_id")
    con.execute("INSERT INTO cid SELECT cid_id, content_id FROM _obj_alloc")
    con.execute("INSERT INTO cid_rev SELECT content_id, cid_id FROM _obj_alloc")

    # New folder rows. dir_id continues above the carried tree AND above everything earlier
    # segments already allocated; the merge renumbers into walk order afterwards, which is
    # allowed because dir_id is layer-1 internal and nothing outside this layer references it.
    carried = con.execute(f"SELECT coalesce(max(dir_id), -1) FROM {base_known}").fetchone()[0] \
        if base_known else -1
    sofar = con.execute("SELECT coalesce(max(dir_id), -1) FROM _tdir_new").fetchone()[0]
    base = max(carried, sofar)
    # From _fetched, NOT from the entries: a folder with no children is still a folder. A folder
    # an earlier segment already recorded is skipped rather than given a second id.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _tdir_seg AS
        SELECT (row_number() OVER (ORDER BY d.parent_oid) + {base})::INTEGER AS dir_id,
               c.cid_id, d.parent_oid, starts_with(d.parent_oid, 'I') AS ind
        FROM (SELECT DISTINCT oid AS parent_oid FROM _fetched) d
        JOIN cid_rev c ON c.content_id = d.parent_oid
        WHERE c.cid_id NOT IN (SELECT cid_id FROM _tdir_new)""")

    # Joined straight against cid_rev. The `_objmap` copy that used to sit here claimed to save
    # a join and did not - both LEFT JOINs still build their own hash table - so it was copying
    # millions of dictionary rows per segment for nothing.
    con.execute(f"""INSERT INTO _tent_new
        SELECT p.dir_id,
               (e.pos)::INTEGER AS pos,
               e.name,
               CASE {' '.join("WHEN e.etype_s = '%s' THEN %d" % (t, i) for i, t in enumerate(tree.TYPES))}
                    END::TINYINT AS etype,
               CASE {' '.join("WHEN e.mode_s = '%s' THEN %d" % (m, i) for i, m in enumerate(tree.MODES))}
                    END::TINYINT AS mode,
               e.size, e.mtime,
               om.cid_id,
               coalesce(starts_with(e.obj, 'I'), false) AS ind,
               omi.cid_id AS ind_cid,
               -- child is resolved at MERGE time from cid_id, not here: in a segmented build the
               -- folder an entry points at may not be materialized until a later segment, and
               -- the merge renumbers every dir_id anyway.
               NULL::INTEGER AS child,
               e.s_size, e.s_files, e.s_symlinks, e.s_dirs, e.s_maxtime, e.s_numfailed,
               e.s_incomplete, e.s_numignorederrors, e.s_errors, e.extra
        FROM _sync_ent e
        JOIN _tdir_seg p ON p.parent_oid = e.parent_oid
        LEFT JOIN cid_rev om ON om.content_id = e.obj
        -- the join key is NULL for a normal object, so only split objects probe this side
        LEFT JOIN cid_rev omi
               ON omi.content_id = CASE WHEN starts_with(e.obj, 'I')
                                        THEN ltrim(e.obj, 'I') END""")
    con.execute("INSERT INTO _tdir_new SELECT * FROM _tdir_seg")

    con.execute("""INSERT INTO _tpart_new
        SELECT ic.cid_id AS iobj_cid, pc.cid_id AS part_cid, p.ord
        FROM _sync_part p
        JOIN cid_rev ic ON ic.content_id = p.iobj
        JOIN cid_rev pc ON pc.content_id = p.part""")

    unknown = con.execute(
        f"SELECT count(*) FROM _sync_ent WHERE etype_s IS NOT NULL AND etype_s NOT IN ({types})"
    ).fetchone()[0]
    odd_mode = con.execute(
        f"SELECT count(*) FROM _sync_ent WHERE mode_s IS NOT NULL AND mode_s NOT IN ({modes})"
    ).fetchone()[0]
    stats = {
        "folders": con.execute("SELECT count(*) FROM _tdir_new").fetchone()[0],
        "entries": con.execute("SELECT count(*) FROM _tent_new").fetchone()[0],
        "parts": con.execute("SELECT count(*) FROM _tpart_new").fetchone()[0],
        "objects_allocated": con.execute("SELECT count(*) FROM _obj_alloc").fetchone()[0],
        "unknown_type": unknown, "unknown_mode": odd_mode,
        "kept_raw": con.execute("SELECT count(*) FROM _sync_ent WHERE extra IS NOT NULL").fetchone()[0],
    }
    if unknown or odd_mode:
        # Not fatal: those entries keep their original JSON in `extra`, so nothing is lost. Worth
        # a loud line because it means kopia emitted something this codec has not seen before.
        log.warning("tree sync saw %d unknown entry type(s) and %d unknown mode(s) - kept verbatim "
                    "in extra", unknown, odd_mode)
    if segment:
        clear_segment(con)
    log.info("tree materialized: %s", stats)
    return stats


def reach_from_roots(con, roots_table: str, out: str) -> int:
    """Folder objects the given snapshot roots can still reach, across the carried AND new tree.

    This is the input to the merge's GC, and it is the one place where getting it wrong deletes
    data the store still needs. Build it ONLY from a snapshot listing that succeeded: a failed
    ``snapshot list`` prints nothing and exits non-zero, and treating that as "no snapshots"
    would prune the entire tree.
    """
    # A link is an entry whose object is itself a folder we hold. Derived from cid_id rather than
    # the `child` column, because in a segmented build `child` is not filled until the merge.
    t: dict = {}
    with timed("reach/links", t):
        con.execute(f"""CREATE OR REPLACE TABLE _link_all AS
        WITH folders AS (
            SELECT cid_id FROM _tdir_new UNION SELECT cid_id FROM tdir)
        SELECT o.cid_id AS parent, e.cid_id AS child
        FROM _tent_new e JOIN _tdir_new o ON o.dir_id = e.dir_id
        WHERE e.etype = {tree.TYPES.index('d')} AND e.cid_id IN (SELECT cid_id FROM folders)
        UNION
        SELECT o.cid_id, e.cid_id
        FROM tent e JOIN tdir o ON o.dir_id = e.dir_id
        WHERE e.etype = {tree.TYPES.index('d')} AND e.cid_id IN (SELECT cid_id FROM folders)""")
    with timed("reach/recursive", t):
        con.execute(f"""CREATE OR REPLACE TABLE {out} AS
        WITH RECURSIVE r(cid_id) AS (
            SELECT cid_id FROM {roots_table}
            UNION
            SELECT l.child FROM r JOIN _link_all l ON l.parent = r.cid_id)
        SELECT cid_id FROM r""")
    return con.execute(f"SELECT count(*) FROM {out}").fetchone()[0]


def merge(con, *, out_dir: str = "tdir_final", out_ent: str = "tent_final",
          out_part: str = "tpart_final", reachable: str | None = None) -> dict:
    """Fold the new rows into the carried tree, in WALK ORDER, and drop what nothing reaches.

    Walk order is not cosmetic: the bench measured it compressing far better than id order, and
    it is what makes a folder's entries a contiguous row range. Ids are therefore reassigned
    here, which is safe because nothing outside layer 1 references ``dir_id``.

    ``reachable`` names a table of ``cid_id`` values that current snapshots can still reach; it
    is the GC. It must only ever be built from a listing that SUCCEEDED - pruning on a failed or
    partial listing is how a walk cache loses folders it still needs.
    """
    t: dict = {}
    with timed("merge/all_dirs", t):
        con.execute("""CREATE OR REPLACE TABLE _all_dirs AS
            SELECT cid_id, ind FROM tdir
            UNION ALL
            SELECT cid_id, ind FROM _tdir_new WHERE cid_id NOT IN (SELECT cid_id FROM tdir)""")
    keep = ("WHERE cid_id IN (SELECT cid_id FROM %s)" % reachable) if reachable else ""
    with timed("merge/dir_order", t):
        con.execute(f"""CREATE OR REPLACE TABLE _dir_order AS
            SELECT (row_number() OVER (ORDER BY cid_id) - 1)::INTEGER AS dir_id, cid_id, ind
            FROM _all_dirs {keep}""")

    # Entries of every kept folder, from whichever side holds them: the new rows win, because a
    # folder object is immutable - if both sides have it they are identical, and preferring the
    # new side keeps a re-read after a failure authoritative.
    #
    # This is a VIEW, not a table, and the filter to kept folders is inside it. Materializing it
    # first meant writing ~19M rows out and immediately reading them back to sort - two passes
    # over the whole tree where one will do. On a conversion (repo 2: 27.3M entries, of which
    # 543,758 folders reduce to 402,032 live ones) that second pass dominated the merge.
    con.execute("""CREATE OR REPLACE VIEW _ent_all AS
        SELECT n.cid_id AS owner, e.* EXCLUDE (dir_id)
        FROM _tent_new e JOIN _tdir_new n ON n.dir_id = e.dir_id
        WHERE n.cid_id IN (SELECT cid_id FROM _dir_order)
        UNION ALL
        SELECT o.cid_id AS owner, e.* EXCLUDE (dir_id)
        FROM tent e JOIN tdir o ON o.dir_id = e.dir_id
        WHERE o.cid_id NOT IN (SELECT cid_id FROM _tdir_new)
          AND o.cid_id IN (SELECT cid_id FROM _dir_order)""")

    # The child link, as a PURE equality join. Written the obvious way -
    #     LEFT JOIN _dir_order c ON c.cid_id = e.cid_id AND e.etype = 1
    # - DuckDB plans BLOCKWISE_NL_JOIN, because it will not hash-join a LEFT JOIN whose ON mixes
    # an equality with a single-side predicate. That is every entry against every folder: 27.7M x
    # 404k on repo 2, and it is what made this merge take 17m50s. Moving the predicate into the
    # join KEY gives the same answer (a non-folder gets a NULL key and matches nothing) with a
    # HASH_JOIN plan. Verified by EXPLAIN; do not "simplify" it back.
    with timed("merge/tent", t):
        con.execute(f"""CREATE OR REPLACE TABLE {out_ent} AS
        SELECT d.dir_id, e.pos, e.name, e.etype, e.mode, e.size, e.mtime, e.cid_id, e.ind,
               e.ind_cid, c.dir_id AS child, e.s_size, e.s_files, e.s_symlinks, e.s_dirs,
               e.s_maxtime, e.s_numfailed, e.s_incomplete, e.s_numignorederrors, e.s_errors,
               e.extra
        FROM _ent_all e
        JOIN _dir_order d ON d.cid_id = e.owner
        LEFT JOIN _dir_order c
               ON c.cid_id = CASE WHEN e.etype = {tree.TYPES.index('d')} THEN e.cid_id END""")
    # Deliberately NOT sorted here. The store runs with preserve_insertion_order=false, so a
    # CTAS ORDER BY is not guaranteed to survive into storage order anyway - and the artifact
    # writer sorts on its way to Parquet, which is the order a reader actually sees. Sorting in
    # both places meant sorting 19M wide rows twice for one ordered file.
    # The row ranges below are counts in dir_id order, so they describe that written file
    # regardless of how this intermediate table happens to be laid out.

    # The row-range sidecar. tent was just written in dir_id order, so a folder's entries are
    # contiguous and a point read is a range rather than a scan.
    with timed("merge/tdir", t):
        con.execute(f"""CREATE OR REPLACE TABLE {out_dir} AS
        WITH n AS (SELECT dir_id, count(*) AS c FROM {out_ent} GROUP BY dir_id)
        SELECT d.dir_id, d.cid_id,
               coalesce(sum(n.c) OVER (ORDER BY d.dir_id
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS row_lo,
               coalesce(sum(n.c) OVER (ORDER BY d.dir_id
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0) AS row_hi,
               d.ind
        FROM _dir_order d LEFT JOIN n ON n.dir_id = d.dir_id
        ORDER BY d.dir_id""")

    # Piece lists follow their object. A list nothing references is dead weight in exactly the
    # way the old walk cache accumulated it (it was never pruned), so keep only the ones a
    # surviving entry actually points at. The split-DIRECTORY case costs nothing: those carry an
    # ind_cid too but have no tpart rows, so the intersection simply finds none.
    with timed("merge/tpart", t):
        con.execute(f"""CREATE OR REPLACE TABLE {out_part} AS
        SELECT iobj_cid, part_cid, ord FROM (
            SELECT * FROM _tpart_new
            UNION ALL
            SELECT * FROM tpart WHERE iobj_cid NOT IN (SELECT iobj_cid FROM _tpart_new))
        WHERE iobj_cid IN (SELECT cid_id FROM {out_ent} WHERE ind AND cid_id IS NOT NULL)
        ORDER BY iobj_cid, ord""")

    stats = {
        "folders": con.execute(f"SELECT count(*) FROM {out_dir}").fetchone()[0],
        "entries": con.execute(f"SELECT count(*) FROM {out_ent}").fetchone()[0],
        "parts": con.execute(f"SELECT count(*) FROM {out_part}").fetchone()[0],
        "dropped": con.execute(f"SELECT (SELECT count(*) FROM _all_dirs) - "
                               f"(SELECT count(*) FROM {out_dir})").fetchone()[0],
        "seconds": t,
    }
    log.info("tree merged: %s", stats)
    return stats
