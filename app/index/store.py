"""Parquet-backed index store - M1 reader.

Replaces the mutable per-repo ``.duckdb`` file with immutable Parquet artifacts published as
numbered generations. This module is the READER: it opens an in-memory DuckDB and binds the
manifest's explicit file lists as views, exposing the raw artifact tables (``filev``,
``pathx``, ``dir``, ``anc``, ``src``, ``cid``, ``snap``, ``csize``, ``crefs``, ``iphys``,
``sanalysis``, ``isnaps``). M2 deleted the compatibility views that used to reassemble the
old table shapes here - every read path resolves integer ids directly through
``resolve.py`` and the rewritten query modules.

The writer (``publish.py``, ``artifacts.py``, ``ids.py``) lands in the same pass; this module
only ever READS. No directory is ever globbed - the manifest names every file.

 added three things here: the tree artifacts (layer 1) bind alongside the index ones, every
connection is an encrypted session when encryption is on, and nothing stays open - the generation
opens on first use and closes again after ``store_idle_close`` seconds without a user request.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time

import duckdb

from config import DATA_DIR, REPOS

from ..core.applog import get_logger
from . import crypto, upgrade

log = get_logger("store")


# ---------------------------------------------------------------------------- RAM tiers
# Auto-detected from total RAM ( plan 4d). 8 GB is the design floor and was proven to
# carry the heaviest query (P10: the layer-3 rebuild at 512 MB / 4 threads, both repos).
#
# Threads are pinned explicitly in EVERY tier, never left at the DuckDB default: the default
# tracks core count (24 here), and a high thread count inflates hash-aggregate memory enough to
# OOM the small tier. Measured at 512 MB, 4 threads was as fast or faster than 24.
# (max GB, memory_limit, threads) - the FIRST tier whose ceiling this machine is under wins;
# 0 is the catch-all. Named as a ceiling because that is how it is compared.
_TIERS = ((8, "512MB", 4), (24, "1GB", 8), (0, "3GB", 16))


def resolve_limits() -> tuple[str, int]:
    """(memory_limit, threads) for this machine: explicit settings win, then env, then the tier."""
    try:
        from ..settings import load_settings
        kl = load_settings()["kopialens"]
    except Exception:  # noqa: BLE001 - a settings problem must not stop the store opening
        kl = {}
    total_gb = 0.0
    try:
        import psutil
        total_gb = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:  # noqa: BLE001 - unknown RAM falls through to the smallest tier
        pass
    limit, threads = _TIERS[-1][1], _TIERS[-1][2]
    for max_gb, mem, thr in _TIERS:
        if total_gb <= max_gb or max_gb == 0:
            limit, threads = mem, thr
            break
    limit = (kl.get("duckdb_memory") or os.environ.get("KOPIALENS_DUCKDB_MEMORY") or limit)
    threads = int(kl.get("duckdb_threads") or threads)
    return str(limit), max(1, threads)


# (The old module-level MEMORY_LIMIT constant is gone: nothing read it once resolve_limits()
# became the single source, so it was a hardcoded default sitting next to the real answer.)

# Per-request pin scope: a dict of repo -> Generation that read_cursor populates lazily on
# first use and the request middleware releases at the end (begin_pins/end_pins). A request
# therefore sees ONE generation per repo it touches - including POST routes whose repo arrives
# in the JSON body - while repos it never touches are never opened. Background threads have no
# scope (None) and resolve per call.
_PIN: contextvars.ContextVar = contextvars.ContextVar("kopialens_generation_pins", default=None)

_GENS: dict[str, "Generation"] = {}   # repo -> current generation handle
# Superseded generations whose readers have not finished. Closing one out from under a live
# cursor is the same class of bug as mixing generations, so a replaced generation is retired
# here and closed only when its last reader releases it.
_RETIRED: list["Generation"] = []
_LOCK = threading.Lock()

# Artifact names bound as views. Layer 1 (the tree) is tdir/tent/tpart; layer 2 is the object
# maps; layer 3 is the index schema. Some are a list of files (pathx/filev are per-source).
_ARTIFACT_TABLES = ("tdir", "tent", "tpart",
                    "filev", "pathx", "dir", "anc", "src", "cid", "snap",
                    "csize", "crefs", "iphys", "sanalysis", "isnaps")


class Generation:
    """One published generation: a manifest plus an in-memory DuckDB with views bound to it."""

    def __init__(self, repo: str, gen_id: int, manifest: dict, con: duckdb.DuckDBPyConnection,
                 mtime_ns: int, session=None):
        self.repo = repo
        self.id = gen_id
        self.manifest = manifest
        self._con = con
        self._mtime_ns = mtime_ns
        self._refs = 0
        self._session = session        # crypto._Session: the attached encrypted scratch db

    def cursor(self):
        cur = self._con.cursor()
        path = crypto.cursor_search_path(self._session)
        if path:
            # The artifact views live in the attached encrypted session database, and a cursor
            # does not inherit the USE that made it current (see crypto.cursor_search_path).
            cur.execute("SET search_path='%s'" % path)
        return cur

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
        if self._session is not None:
            self._session.cleanup()    # the encrypted scratch db dies with its connection


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


def generation_state(repo: str) -> bool:
    """Whether this repo's data is encrypted - from its MANIFEST, which is the fact.

    A repo with no generation yet follows the setting: that is what its first build will write.
    The setting can be ahead of the data while a switch is pending, so anything that decides by
    the setting alone gets the wrong answer for exactly one transition window.
    """
    manifest = read_manifest(repo)
    if manifest is None:
        return crypto.writes_encrypted()
    return bool(manifest.get("encrypted"))


def _one_table(con, name: str, files: list[str], encrypted: bool) -> None:
    """Bind one artifact as a read_parquet view over an explicit file list (never a glob).

    The argument list comes from ``crypto`` so the encryption config rides along when the
    generation is encrypted - a view built without it fails at read time with "Computed AES tag
    differs", which is a confusing place to learn the key was missing."""
    if not files:
        raise ValueError(f"artifact {name} has no files")
    args = crypto.parquet_read_list([f.replace("\\", "/").replace("'", "''") for f in files],
                                    encrypted=encrypted)
    con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet({args})")


def _bind_generation(con, repo: str, artifacts: dict[str, list[str]], encrypted: bool) -> None:
    """Bind the raw artifact views over an explicit file list (never a glob).

    M2 deleted the compatibility views that used to reassemble the old ``files``/``paths``/
    ``content_sizes``/... shapes here - the rewritten query modules read the raw artifacts and
    resolve integer ids via ``resolve.py``. ``first_seen_meta``/``kl_meta`` stay as typed empty
    stubs: builder-only tables, so an accidental read returns nothing instead of raising.
    """
    for name in _ARTIFACT_TABLES:
        if name in artifacts:
            _one_table(con, name, artifacts[name], encrypted)

    # Read-only stubs: these are builder-only tables (rewrite/backfill tracking). Empty but
    # correctly typed so any accidental read returns nothing instead of raising.
    con.execute(f"CREATE VIEW first_seen_meta AS "
                f"SELECT NULL::VARCHAR AS repo, NULL::VARCHAR AS source, "
                f"NULL::TIMESTAMP AS done_at WHERE false")
    con.execute("CREATE VIEW kl_meta AS "
                "SELECT NULL::VARCHAR AS key, NULL::VARCHAR AS value, "
                "NULL::TIMESTAMP AS set_at WHERE false")


def open_generation(repo: str) -> Generation:
    """Open the current generation: read the manifest, bind views, return the handle.

    The manifest's mtime is captured BEFORE the manifest is read. The other order pairs the
    views of one manifest with the mtime of a newer one (a publication landing between the read
    and the stat), and ``current()`` would then keep serving the older views as current until
    the NEXT publish. Stamping the older mtime can only cause one extra reopen."""
    p = manifest_path(repo)
    if not os.path.exists(p):
        raise FileNotFoundError(f"repo {repo!r} has no index (no manifest.json)")
    mtime = os.stat(p).st_mtime_ns
    manifest = read_manifest(repo)
    if manifest is None:
        raise FileNotFoundError(f"repo {repo!r} has no index (no manifest.json)")
    found = int(manifest.get("schema_version") or 0)
    if found != upgrade.MANIFEST_VERSION:
        # Refusing is the point: an older layout read by newer code misreads silently, and a NEWER
        # layout must never be read (or written) by an older app. The startup routine migrates the
        # older case in place; the newer case is the user's to resolve.
        raise upgrade.IndexVersionMismatch(repo, found, upgrade.MANIFEST_VERSION)
    encrypted = bool(manifest.get("encrypted"))
    if encrypted and not crypto.unlocked():
        # Two different problems with two different fixes - a locked key wants a password, a
        # missing keystore wants a restore or a rebuild.
        if not crypto.configured():
            raise crypto.StoreLocked("the index is encrypted but its keystore is gone - restore "
                                     "the keystore file or rebuild the index from kopia")
        raise crypto.StoreLocked("the index is encrypted and locked - unlock it to read this repo")
    limit, threads = resolve_limits()
    tmp = os.path.join(DATA_DIR, f"duck_tmp_repo{repo}")
    con = duckdb.connect()
    session = crypto.apply(con, tmp, limit, threads, encrypted=encrypted)
    base = repo_dir(repo)
    artifacts = {
        name: [p2 if os.path.isabs(p2) else os.path.join(base, p2) for p2 in files]
        for name, files in manifest.get("artifacts", {}).items()
    }
    try:
        _bind_generation(con, repo, artifacts, encrypted)
    except Exception:
        con.close()
        if session is not None:
            session.cleanup()
        raise
    return Generation(repo, manifest.get("generation"), manifest, con, mtime, session)


def _reap_locked() -> None:
    """Close retired generations nothing is reading. Caller holds ``_LOCK``."""
    for gen in [g for g in _RETIRED if g._refs <= 0]:
        _RETIRED.remove(gen)
        gen.close()


def current(repo: str) -> Generation:
    """The live generation for a repo, returned WITH one reference already held.

    Selection and acquisition happen under the same lock: the previous shape returned the
    generation first and acquired the reference in the caller's next statement, so a publication
    landing in that gap could retire and reap a zero-reference generation out from under the
    caller. Callers that do not hand the reference to someone else must release it.

    A superseded generation is RETIRED, not closed: a request holding a cursor off it must be
    able to finish. It closes when its last reader releases.
    """
    p = manifest_path(repo)
    mtime = os.stat(p).st_mtime_ns if os.path.exists(p) else 0
    with _LOCK:
        gen = _GENS.get(repo)
        if gen is not None and gen._mtime_ns == mtime:
            gen._refs += 1
            return gen
    fresh = open_generation(repo)
    with _LOCK:
        old = _GENS.get(repo)
        # Another thread may have opened the same generation while we were binding views; keep
        # theirs and discard ours rather than retiring a generation readers just picked up.
        if old is not None and old._mtime_ns >= fresh._mtime_ns:
            fresh.close()
            old._refs += 1
            return old
        if old is not None:
            _RETIRED.append(old)
        _GENS[repo] = fresh
        fresh._refs += 1
        _reap_locked()
        return fresh


class _PinnedCursor:
    """Curser off a generation that releases the handle on close (schema._PooledCursor's contract)."""

    def __init__(self, gen: Generation, cur):
        self._gen = gen
        self._cur = cur
        self._closed = False

    @property
    def generation(self) -> Generation:
        """The handle this cursor reads from - resolve.py scopes its lookup caches to it."""
        return self._gen

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
    """Cursor off the current generation, honoring the request's per-repo pin scope so a request
    touching several queries all sees one generation (mixed = silent wrong numbers).

    Inside a pin scope (middleware, or ``pinned()``), the repo's generation is resolved once,
    kept in the scope's dict, and reused for every later read - which is how POST routes whose
    repo arrives in the JSON body get the same guarantee as query-param routes. Outside a scope
    (background threads) each call resolves the current generation."""
    pins = _PIN.get()
    gen = pins.get(repo) if pins is not None else None
    if gen is not None:
        gen.acquire()             # the cursor's own reference; released by cursor.close()
    else:
        gen = current(repo)       # returned with one reference held ...
        if pins is not None:
            pins[repo] = gen      # ... and that reference now belongs to the pin scope
            gen.acquire()         # the cursor takes its own, on top of the scope's
    try:
        return _PinnedCursor(gen, gen.cursor())
    except Exception:
        gen.release()
        raise


def begin_pins():
    """Open a request pin scope (see ``read_cursor``). Returns the contextvar token."""
    return _PIN.set({})


def end_pins(token) -> None:
    """Close a pin scope: release every generation it pinned. Runs even when the request raised."""
    pins = _PIN.get() or {}
    _PIN.reset(token)
    for gen in pins.values():
        gen.release()


@contextlib.contextmanager
def pinned(repo: str):
    """Pin one generation for the duration of a block - the standalone form of the request pin
    scope (the middleware calls ``begin_pins``/``end_pins`` instead). Reads inside the block via
    ``read_cursor`` resolve through this generation."""
    try:
        gen = current(repo)       # returned acquired; the scope holds the reference
    except FileNotFoundError:     # not indexed yet: nothing to pin, let callers raise normally
        yield None
        return
    token = _PIN.set({repo: gen})
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


def close_repo(repo: str) -> dict:
    """Close this repo's live generation (and any retired ones) so its files can be deleted -
    Windows refuses to unlink a Parquet file another handle holds open. A generation still being
    read is left for the reaper and reported as busy, so a request never loses its cursor mid-flight;
    the caller warns about the leftovers (or leaves the files alone) instead of pretending the
    delete was complete."""
    closed, busy = 0, 0
    with _LOCK:
        gen = _GENS.pop(repo, None)
        if gen is not None:
            if gen._refs <= 0:      # the property would take _LOCK again (plain Lock, not RLock)
                gen.close()
                closed += 1
            else:
                _RETIRED.append(gen)
                busy += 1
        keep = []
        for g in _RETIRED:
            if getattr(g, "repo", None) == repo:
                if g._refs <= 0:
                    g.close()
                    closed += 1
                    continue
                busy += 1
            keep.append(g)
        _RETIRED[:] = keep
    return {"closed": closed, "busy": busy}


@contextlib.contextmanager
def scratch_connection(repo: str):
    """A standalone, fully configured DuckDB connection for build and publish work.

    For writers that need an in-memory connection (``publish.replace_artifacts``). A connection
    built by hand misses three things at once: the RAM tier, the encrypted session that keeps
    spilled temp files off disk in the clear, and the registered Parquet key without which a COPY
    writes a PLAINTEXT file into an otherwise encrypted generation - and that one fails silently,
    since the file is perfectly readable, just not encrypted.

    The index build needs a FILE-backed scratch (it holds the whole tree), so it goes through
    ``crypto.open_scratch`` instead - same four guarantees, a database on disk.

    The state comes from the repo's manifest, not the setting: a writer must not flip a
    generation's state on its own, and while a switch is pending the setting says the opposite.
    """
    encrypted = generation_state(repo)
    if encrypted and not crypto.unlocked():
        raise crypto.StoreLocked("the store is encrypted and locked - unlock it before writing")
    limit, threads = resolve_limits()
    tmp = os.path.join(DATA_DIR, f"duck_tmp_repo{repo}")
    con = duckdb.connect()
    session = crypto.apply(con, tmp, limit, threads, encrypted=encrypted)
    try:
        yield con
    finally:
        try:
            con.close()
        finally:
            if session is not None:
                session.cleanup()


# ---------------------------------------------------------------------------- idle close
# Nothing opens at app start and nothing stays open: the first use opens a generation (~0.05 s)
# and after `store_idle_close` seconds without a USER request every connection is closed again,
# which measurably returns the memory (three load/close cycles left ~10 MB residual, no creep).
#
# USER request is the operative word. The UI polls a handful of status endpoints every few
# seconds and the light pass runs every 30 minutes; if either counted as activity an open browser
# tab would pin the store open forever, and the light pass would churn open/close cycles. Only
# `touch()` marks activity, and `main.py` calls it for non-polled API paths only.

_LAST_TOUCH = time.time()
_REAPER_STARTED = False
_IDLE_DEFAULT = 1800


def touch() -> None:
    """Mark real user activity (see above). Cheap enough to call on every request."""
    global _LAST_TOUCH
    _LAST_TOUCH = time.time()


def idle_seconds() -> int:
    try:
        from ..settings import load_settings
        return int(load_settings()["kopialens"].get("store_idle_close", _IDLE_DEFAULT))
    except Exception:  # noqa: BLE001
        return _IDLE_DEFAULT


def close_idle(now: float | None = None) -> bool:
    """Close every generation nothing is reading, if the idle window has passed. True if closed."""
    limit = idle_seconds()
    if limit <= 0:
        return False
    now = time.time() if now is None else now
    if now - _LAST_TOUCH < limit:
        return False
    with _LOCK:
        if any(g._refs > 0 for g in list(_GENS.values()) + list(_RETIRED)):
            return False               # a background build is mid-read; try again next tick
        if not _GENS and not _RETIRED:
            return False
        for gen in list(_GENS.values()) + list(_RETIRED):
            gen.close()
        _GENS.clear()
        _RETIRED.clear()
    log.debug("store idle for %ds - closed every generation", limit)
    return True


def start_idle_reaper(interval: int = 60) -> None:
    """Start the one background thread that applies the idle close. Called from the app lifespan;
    the worker never calls it (that process exits when its run ends)."""
    global _REAPER_STARTED
    with _LOCK:
        if _REAPER_STARTED:
            return
        _REAPER_STARTED = True

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                close_idle()
            except Exception as exc:  # noqa: BLE001 - a reaper hiccup must never kill the thread
                log.warning("idle close failed: %s", exc)

    threading.Thread(target=_loop, name="store-idle", daemon=True).start()