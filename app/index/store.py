"""Parquet-backed index store - M1 reader.

Replaces the mutable per-repo ``.duckdb`` file with immutable Parquet artifacts published as
numbered generations. This module is the READER: it opens an in-memory DuckDB and binds the
manifest's explicit file lists as views, exposing (a) the raw artifact tables (``filev``,
``pathx``, ``dir``, ``anc``, ``src``, ``cid``, ``snap``, ``csize``, ``crefs``, ``iphys``,
``sanalysis``, ``isnaps``) for M2, and (b) compatibility views (``files``, ``paths``,
``content_sizes``, ``content_refs``, ``indirect_phys``, ``snapshot_analysis``,
``indexed_snapshots``, ``first_seen_meta``, ``kl_meta``) that make the new data look like
today's tables so the existing queries run unchanged.

The writer (``publish.py``, ``artifacts.py``, ``ids.py``) lands in the same pass; this module
only ever READS. No directory is ever globbed - the manifest names every file.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading

import duckdb

from config import DATA_DIR, REPOS

from ..core.applog import get_logger

log = get_logger("store")

MEMORY_LIMIT = os.environ.get("KOPIALENS_DUCKDB_MEMORY", "3GB")

# A request pins one generation for all its queries so a publish mid-request cannot mix
# generations (the one class of bug that must not ship). Set by middleware, read by read_cursor.
_PIN: contextvars.ContextVar = contextvars.ContextVar("kopialens_generation", default=None)

_GENS: dict[str, "Generation"] = {}   # repo -> current generation handle
# Superseded generations whose readers have not finished. Closing one out from under a live
# cursor is the same class of bug as mixing generations, so a replaced generation is retired
# here and closed only when its last reader releases it.
_RETIRED: list["Generation"] = []
_LOCK = threading.Lock()

# Artifact names that are a list of files (history/pathx are per-source lists) vs single files.
_ARTIFACT_TABLES = ("filev", "pathx", "dir", "anc", "src", "cid", "snap",
                    "csize", "crefs", "iphys", "sanalysis", "isnaps")


class Generation:
    """One published generation: a manifest plus an in-memory DuckDB with views bound to it."""

    def __init__(self, repo: str, gen_id: int, manifest: dict, con: duckdb.DuckDBPyConnection,
                 mtime_ns: int):
        self.repo = repo
        self.id = gen_id
        self.manifest = manifest
        self._con = con
        self._mtime_ns = mtime_ns
        self._refs = 0

    def cursor(self):
        return self._con.cursor()

    def acquire(self) -> None:
        with _LOCK:
            self._refs += 1

    def release(self) -> None:
        with _LOCK:
            self._refs -= 1
            _reap_locked()

    @property
    def refs(self) -> int:
        with _LOCK:
            return self._refs

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # noqa: BLE001 - a failed close must not break the swap
            pass


def repo_dir(repo: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"repo{repo}")


def manifest_path(repo: str) -> str:
    return os.path.join(repo_dir(repo), "manifest.json")


def read_manifest(repo: str) -> dict | None:
    """The current generation's manifest, or None for a not-yet-indexed repo."""
    p = manifest_path(repo)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _one_table(con, name: str, files: list[str]) -> None:
    """Bind one artifact as a read_parquet view over an explicit file list (never a glob)."""
    if not files:
        raise ValueError(f"artifact {name} has no files")
    paths = ", ".join("'%s'" % f.replace("\\", "/").replace("'", "''") for f in files)
    con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet([{paths}])")


def _bind_generation(con, repo: str, artifacts: dict[str, list[str]]) -> None:
    """Bind the raw artifact views and the compatibility views over them.

    ``artifacts`` maps artifact name -> list of absolute file paths. The ``repo`` id is baked
    into the compatibility views as a literal (the per-repo data dir makes the column redundant);
    the ``WHERE repo = ?`` predicates keep working and are deleted in M2, not here.
    """
    for name in _ARTIFACT_TABLES:
        if name in artifacts:
            _one_table(con, name, artifacts[name])

    repo_lit = str(repo)
    con.execute(f"""
        CREATE VIEW paths AS
        SELECT '{repo_lit}' AS repo, s.source, p.full_path, p.name, p.ext, p.size,
               p.versions, p.first_seen, p.last_seen, p.in_latest, c.content_id,
               p.physical, lower(p.full_path) AS path_lc, p.exclusive
        FROM pathx p
        JOIN src s ON s.src_id = p.src_id
        LEFT JOIN cid c ON c.cid_id = p.cid_id
    """)
    con.execute(f"""
        CREATE VIEW files AS
        SELECT '{repo_lit}' AS repo, s.source, p.full_path, p.name, f.size, c.content_id,
               sn.snapshot_id, sn.start_time, f.ordinal, f.mtime
        FROM filev f
        JOIN pathx p ON p.path_id = f.path_id
        JOIN src s ON s.src_id = p.src_id
        JOIN snap sn ON sn.snap_id = f.snap_id
        LEFT JOIN cid c ON c.cid_id = f.cid_id
    """)
    con.execute(f"CREATE VIEW content_sizes AS "
                f"SELECT '{repo_lit}' AS repo, c.content_id AS cid, m.packed FROM csize m "
                f"JOIN cid c USING (cid_id)")
    con.execute(f"CREATE VIEW content_refs AS "
                f"SELECT '{repo_lit}' AS repo, c.content_id AS cid, m.refs::BIGINT AS refs "
                f"FROM crefs m JOIN cid c USING (cid_id)")
    con.execute(f"CREATE VIEW indirect_phys AS "
                f"SELECT '{repo_lit}' AS repo, c.content_id AS oid, m.packed FROM iphys m "
                f"JOIN cid c USING (cid_id)")
    # The small tables were copied whole into their artifacts, so they already match the old
    # schema (repo included) and need no column surgery.
    con.execute("CREATE VIEW snapshot_analysis AS SELECT * FROM sanalysis")
    con.execute("CREATE VIEW indexed_snapshots AS SELECT * FROM isnaps")
    # Read-only stubs: these are builder-only tables (rewrite/backfill tracking). Empty but
    # correctly typed so any accidental read returns nothing instead of raising.
    con.execute(f"CREATE VIEW first_seen_meta AS "
                f"SELECT NULL::VARCHAR AS repo, NULL::VARCHAR AS source, "
                f"NULL::TIMESTAMP AS done_at WHERE false")
    con.execute("CREATE VIEW kl_meta AS "
                "SELECT NULL::VARCHAR AS key, NULL::VARCHAR AS value, "
                "NULL::TIMESTAMP AS set_at WHERE false")


def open_generation(repo: str) -> Generation:
    """Open the current generation: read the manifest, bind views, return the handle."""
    manifest = read_manifest(repo)
    if manifest is None:
        raise FileNotFoundError(f"repo {repo!r} has no index (no manifest.json)")
    con = duckdb.connect()
    con.execute("SET memory_limit='%s'" % MEMORY_LIMIT)
    tmp = os.path.join(DATA_DIR, f"duck_tmp_repo{repo}")
    os.makedirs(tmp, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp.replace(chr(92), chr(47))}'")
    base = repo_dir(repo)
    artifacts = {
        name: [p if os.path.isabs(p) else os.path.join(base, p) for p in files]
        for name, files in manifest.get("artifacts", {}).items()
    }
    _bind_generation(con, repo, artifacts)
    mtime = os.stat(manifest_path(repo)).st_mtime_ns
    return Generation(repo, manifest.get("generation"), manifest, con, mtime)


def _reap_locked() -> None:
    """Close retired generations nothing is reading. Caller holds ``_LOCK``."""
    for gen in [g for g in _RETIRED if g._refs <= 0]:
        _RETIRED.remove(gen)
        gen.close()


def current(repo: str) -> Generation:
    """The live generation for a repo, opened once and reused until the manifest changes.

    A superseded generation is RETIRED, not closed: a request holding a cursor off it must be
    able to finish. It closes when its last reader releases.
    """
    mtime = os.stat(manifest_path(repo)).st_mtime_ns if os.path.exists(manifest_path(repo)) else 0
    with _LOCK:
        gen = _GENS.get(repo)
        if gen is not None and gen._mtime_ns == mtime:
            return gen
    fresh = open_generation(repo)
    with _LOCK:
        old = _GENS.get(repo)
        # Another thread may have opened the same generation while we were binding views; keep
        # theirs and discard ours rather than retiring a generation readers just picked up.
        if old is not None and old._mtime_ns == fresh._mtime_ns:
            fresh.close()
            return old
        if old is not None:
            _RETIRED.append(old)
        _GENS[repo] = fresh
        _reap_locked()
        return fresh


class _PinnedCursor:
    """Curser off a generation that releases the handle on close (schema._PooledCursor's contract)."""

    def __init__(self, gen: Generation, cur):
        self._gen = gen
        self._cur = cur
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._cur.close()
        finally:
            self._gen.release()


def read_cursor(repo: str):
    """Cursor off the current generation, honoring the request-pinned generation so a request
    touching several queries all sees one generation (mixed = silent wrong numbers)."""
    gen = _PIN.get()
    if gen is None or gen.repo != repo:
        gen = current(repo)
    gen.acquire()
    try:
        return _PinnedCursor(gen, gen.cursor())
    except Exception:
        gen.release()
        raise


@contextlib.contextmanager
def pinned(repo: str):
    """Pin one generation for the duration of a request.

    Without this every ``read_cursor`` resolves independently, so a publish landing mid-request
    can serve pathx from one generation and history from the next - a mixture that produces wrong
    numbers with no error. Installed as middleware in ``app/main.py``.
    """
    try:
        gen = current(repo)
    except FileNotFoundError:      # not indexed yet: nothing to pin, let callers raise normally
        yield None
        return
    gen.acquire()
    token = _PIN.set(gen)
    try:
        yield gen
    finally:
        _PIN.reset(token)
        gen.release()


def close_all() -> None:
    with _LOCK:
        for gen in list(_GENS.values()) + list(_RETIRED):
            gen.close()
        _GENS.clear()
        _RETIRED.clear()