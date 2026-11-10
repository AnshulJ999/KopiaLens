"""Parquet store for the Backblaze `files` table ().

Mirrors app/index/store.py + publish.py's generation machinery, scoped to the BZ index:
immutable per-drive Parquet files under ``data/bz/pool/`` published as numbered generations via
an atomically-swapped ``manifest.json`` (tmp + fsync + os.replace). A reader never globs a
directory - the manifest names every file - and a superseded generation is RETIRED, not closed,
until its last reader releases. The small history/state tables (folder_snapshots, events_archive,
backup_days, kv, ingest, queue, events, churn, folder_rollup) stay in the slim SQLite (bbdb.py).

The active store is chosen per call by the ``backblaze.store`` setting ("parquet" | "sqlite" -
parquet is the DEFAULT, sqlite is the legacy path kept for rollback). Switching to parquet
bootstraps a full reindex from the read-only filelists: an empty store has no drives, so every
non-stub drive fails its have_rows check regardless of the ingest watermarks.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pqt

from config import DATA_DIR

from ..core.applog import get_logger
from ..core import atomicfile

log = get_logger("bz.pq")

# Column order matches the SQLite `files` table exactly, so the read-side SQL is shared.
SCHEMA = pa.schema([
    ("drive", pa.string()), ("full_path", pa.string()), ("path_lc", pa.string()),
    ("name", pa.string()), ("ext", pa.string()), ("size", pa.int64()),
    ("mtime_hex", pa.string()), ("excluded", pa.int64()), ("excl_rule", pa.string()),
])
COLUMNS = [f.name for f in SCHEMA]
SCHEMA_VERSION = 1

MEMORY_LIMIT = os.environ.get("KOPIALENS_BZ_DUCKDB_MEMORY", "2GB")
_GC_GRACE_S = 3600  # same grace as index/publish: a reader-held file is deleted on a later pass

# Test hook: when set, root() resolves here instead of DATA_DIR/bz.
ROOT: str | None = None


def root() -> str:
    r = ROOT or os.path.join(DATA_DIR, "bz")
    os.makedirs(r, exist_ok=True)
    return r


def manifest_path() -> str:
    return os.path.join(root(), "manifest.json")


def read_manifest() -> dict | None:
    p = manifest_path()
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def available() -> bool:
    """True when a published generation holds at least one drive file. A cleared store (another
    computer whose first scan has not landed) keeps a manifest with no files, so reads stay empty."""
    manifest = read_manifest() or {}
    return bool((manifest.get("artifacts") or {}).get("files"))


def staging_dir() -> str:
    d = os.path.join(root(), "_staging")
    os.makedirs(d, exist_ok=True)
    return d


def staged_path(drive: str, gen: int) -> str:
    return os.path.join(staging_dir(), f"files_{drive}_g{gen}.parquet")


def pool_rel(drive: str, gen: int) -> str:
    return f"pool/files_{drive}/g{gen}.parquet"


# ---- writer ---------------------------------------------------------------------------------

class WriterLock:
    """One-process-at-a-time publication via an O_CREAT|O_EXCL lock file. Same technique as
    index/publish.WriterLock (stale detection by pid + process start time)."""

    def __init__(self):
        self.path = os.path.join(root(), ".writer")

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._stale():
                try:
                    os.remove(self.path)
                except Exception:  # noqa: BLE001
                    pass
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                from ..index.publish import WriterBusy
                raise WriterBusy(f"another writer holds {self.path}")
        try:
            import psutil
            stamp = f"{os.getpid()}:{psutil.Process(os.getpid()).create_time()}"
        except Exception:  # noqa: BLE001 - psutil absent: pid alone, _stale degrades gracefully
            stamp = str(os.getpid())
        with os.fdopen(fd, "w") as fh:
            fh.write(stamp)

    def _stale(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as fh:
                pid_s, _, started_s = fh.read().strip().partition(":")
            pid = int(pid_s or 0)
        except Exception:  # noqa: BLE001 - unreadable lock is a stale lock
            return True
        if not pid:
            return True
        try:
            import psutil
            if not started_s:
                return True
            return abs(psutil.Process(pid).create_time() - float(started_s)) > 1.0
        except Exception:  # noqa: BLE001
            return True

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except Exception:  # noqa: BLE001
            pass


def target_state() -> bool:
    """Whether a NEW BZ file should be encrypted: the published manifest decides, and a
    first build follows the Full level - the same rule the index build uses."""
    manifest = read_manifest()
    if manifest is not None:
        return bool(manifest.get("encrypted"))
    from ..index import crypto
    return crypto.covers_files()


def ensure_staged_state(staged: str) -> bool:
    """Re-encode a staged plaintext drive file into the encrypted state when it is due.

    The BZ writer is PyArrow, which cannot write DuckDB-encrypted Parquet; DuckDB can read a
    PyArrow file and write an encrypted one, so the file is converted on its way into the
    pool and the plaintext original is removed at once. Only a crash mid-refresh can leave
    one behind, in the app's own staging folder (cleared by the next refresh).
    """
    if not target_state():
        return False
    from ..index import crypto
    tmp = staged + ".enc"
    con = duckdb.connect()
    try:
        crypto.register_key(con, encrypted=True)
        con.execute("COPY (SELECT * FROM read_parquet('%s')) TO '%s' (%s)"
                    % (staged.replace(chr(92), "/").replace("'", "''"),
                       tmp.replace(chr(92), "/").replace("'", "''"),
                       crypto.copy_options(encrypted=True)))
    finally:
        con.close()
    atomicfile.replace(tmp, staged)
    return True


def open_writer(staged: str) -> pqt.ParquetWriter:
    """Streaming zstd Parquet writer over SCHEMA for one staged drive file (caller closes)."""
    return pqt.ParquetWriter(staged, SCHEMA, compression="zstd", compression_level=3)


def table_from_rows(batch: list[tuple]) -> pa.Table:
    """One batch of plain tuples (in SCHEMA column order) -> an Arrow table."""
    return pa.table(dict(zip(COLUMNS, zip(*batch))), schema=SCHEMA)


def clear(gen: int) -> None:
    """Publish an EMPTY generation: no drives, no files. Used when the folder now points at another
    computer whose first scan has not produced a usable file list, so no reader can see the old
    computer's data under the new name. The old pool files are cleaned by the next real publish."""
    global _GEN
    state = target_state()
    manifest = {"generation": gen, "schema": SCHEMA_VERSION,
                "artifacts": {"files": []}, "drives": {}, "encrypted": state}
    path = manifest_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
        fh.flush()
        os.fsync(fh.fileno())
    atomicfile.replace(tmp, path)
    with _LOCK:
        if _GEN is not None and _GEN._refs <= 0:
            _GEN.close()
            _GEN = None
    log.info("published an empty BZ parquet generation %s (waiting for Backblaze's first scan)", gen)


def publish(gen: int, drive_rels: dict[str, str], moves: list[tuple[str, str]],
            encrypted: bool | None = None) -> None:
    """Move staged per-drive files into pool/ (atomic per file), swap the manifest, GC.

    Caller holds WriterLock. Unchanged drives keep their previous rel path (carried by
    reference), so only reindexed drives cost new bytes."""
    r = root()
    previous: list[str] = []
    try:
        with open(manifest_path(), encoding="utf-8") as fh:
            previous = list(json.load(fh).get("artifacts", {}).get("files", []))
    except Exception:
        previous = []
    state = target_state() if encrypted is None else bool(encrypted)
    for staged, rel in moves:
        dst = os.path.join(r, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        atomicfile.replace(staged, dst)
    # The same self-check the index build runs: every file this generation will name must be
    # in the state the manifest is about to declare. A PyArrow write that skipped the
    # re-encode would otherwise land plaintext under an `encrypted: true` manifest.
    from ..index import crypto
    misplaced = [rel for rel in drive_rels.values()
                 if crypto.file_is_encrypted(os.path.join(r, rel)) != state]
    if misplaced:
        raise RuntimeError(f"BZ generation {gen}: {len(misplaced)} file(s) are not in the "
                           f"declared encryption state, e.g. {misplaced[:3]}")
    manifest = {
        "generation": gen,
        "schema": SCHEMA_VERSION,
        "artifacts": {"files": sorted(drive_rels.values())},
        "drives": dict(drive_rels),
        "encrypted": state,
    }
    path = manifest_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(12):
        try:
            atomicfile.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 11:
                raise
            import gc
            gc.collect()
            time.sleep(0.3)
    _gc(manifest["artifacts"]["files"], previous)
    log.info("published BZ parquet generation %s (%d drives)", gen, len(drive_rels))


def _gc(current: list[str], previous: list[str]) -> None:
    keep = {c.replace("\\", "/") for c in current} | {p.replace("\\", "/") for p in previous}
    pool = os.path.join(root(), "pool")
    if not os.path.isdir(pool):
        return
    now = time.time()
    for dirpath, _dirs, files in os.walk(pool):
        for fn in files:
            if not fn.endswith(".parquet"):
                continue
            abs_ = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_, root()).replace("\\", "/")
            if rel in keep or fn.startswith("."):
                continue
            try:
                if now - os.path.getmtime(abs_) < _GC_GRACE_S:
                    continue
                os.remove(abs_)
            except Exception:  # noqa: BLE001 - a reader-held file is deleted next cycle
                pass


# ---- reader ---------------------------------------------------------------------------------

def _bind(con: duckdb.DuckDBPyConnection, files: list[str], encrypted: bool) -> None:
    """Bind the `files` view over the manifest's explicit file list (never a glob)."""
    if not files:
        raise FileNotFoundError("BZ parquet manifest lists no files")
    from ..index import crypto
    args = crypto.parquet_read_list([f.replace("\\", "/").replace("'", "''") for f in files],
                                    encrypted=encrypted)
    con.execute(f"CREATE VIEW files AS SELECT * FROM read_parquet({args})")


def _open_generation(gen_id: int, manifest: dict, mtime_ns: int):
    from ..index import crypto
    r = root()
    files = [p if os.path.isabs(p) else os.path.join(r, p)
             for p in manifest.get("artifacts", {}).get("files", [])]
    encrypted = bool(manifest.get("encrypted"))
    if encrypted and not crypto.unlocked():
        raise crypto.StoreLocked("the Backblaze store is encrypted and locked - unlock KopiaLens")
    con = duckdb.connect()
    tmp = os.path.join(r, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    session = crypto.apply(con, tmp, MEMORY_LIMIT, None, encrypted=encrypted)
    # crypto.apply sets the memory limit itself; BZ keeps DuckDB's own thread default.
    _bind(con, files, encrypted)
    return _Generation(gen_id, manifest, con, mtime_ns, session)


class _Generation:
    """One published generation: the manifest plus an in-memory DuckDB bound to it (mirrors
    index/store.Generation, minus the per-request pin scope - a BZ publish lands at most once
    per reindex gap, and every query function holds one cursor across its statements)."""

    def __init__(self, gen_id, manifest, con, mtime_ns, session=None):
        self.id = gen_id
        self.manifest = manifest
        self._con = con
        self._mtime_ns = mtime_ns
        self._refs = 0
        self._session = session          # index.crypto._Session: the encrypted scratch db

    def cursor(self):
        cur = self._con.cursor()
        from ..index import crypto
        path = crypto.cursor_search_path(self._session)
        if path:
            cur.execute("SET search_path='%s'" % path)
        return cur

    def acquire(self) -> None:
        with _LOCK:
            self._refs += 1

    def release(self) -> None:
        with _LOCK:
            self._refs -= 1
            _reap_locked()

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # noqa: BLE001 - a failed close must not break the swap
            pass
        if self._session is not None:
            self._session.cleanup()


_LOCK = threading.Lock()
_GEN: _Generation | None = None
_RETIRED: list[_Generation] = []


def _reap_locked() -> None:
    for gen in [g for g in _RETIRED if g._refs <= 0]:
        _RETIRED.remove(gen)
        gen.close()


def close_open_generation() -> bool:
    """Close the live/retired BZ generation if nothing is reading it. False = busy (the
    switch then leaves the superseded files to the normal GC, exactly like the index side)."""
    global _GEN
    with _LOCK:
        gens = ([_GEN] if _GEN is not None else []) + list(_RETIRED)
        if any(g._refs > 0 for g in gens):
            return False
        for gen in gens:
            gen.close()
        _GEN = None
        _RETIRED.clear()
    return True


def current() -> _Generation:
    """The live generation WITH one reference held (mirrors index/store.current)."""
    global _GEN
    p = manifest_path()
    mtime = os.stat(p).st_mtime_ns if os.path.exists(p) else 0
    with _LOCK:
        gen = _GEN
        if gen is not None and gen._mtime_ns == mtime:
            gen._refs += 1
            return gen
    if not os.path.exists(p):
        raise FileNotFoundError("BZ parquet store has no manifest (never built)")
    mtime = os.stat(p).st_mtime_ns
    with open(p, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not (manifest.get("artifacts") or {}).get("files"):
        raise FileNotFoundError("BZ parquet store is empty (waiting for Backblaze's first scan)")
    fresh = _open_generation(manifest.get("generation"), manifest, mtime)
    with _LOCK:
        old = _GEN
        if old is not None and old._mtime_ns >= fresh._mtime_ns:
            fresh.close()
            old._refs += 1
            return old
        if old is not None:
            _RETIRED.append(old)
        _GEN = fresh
        fresh._refs += 1
        _reap_locked()
        return fresh


@contextlib.contextmanager
def files_cursor():
    """A DuckDB cursor over the current generation's `files` view; releases the generation on exit."""
    gen = current()  # returns with one reference held
    try:
        cur = gen.cursor()
        try:
            yield cur
        finally:
            cur.close()
    finally:
        gen.release()


def has_drive(drive: str) -> bool:
    """Drive present in the current manifest (manifest read, no query)."""
    m = read_manifest()
    return bool(m and drive in (m.get("drives") or {}))


def drive_counts(drive: str) -> tuple[int, int, int]:
    """(total, backed, excluded) for one drive in the current generation."""
    with files_cursor() as cur:
        row = cur.execute(
            "SELECT count(*), "
            "sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END), "
            "sum(CASE WHEN excluded=1 THEN 1 ELSE 0 END) FROM files WHERE drive=?",
            [drive]).fetchone()
    return (row[0] or 0, row[1] or 0, row[2] or 0)
