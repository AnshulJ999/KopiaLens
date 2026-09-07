"""Shared sqlite cache for immutable Kopia directory metadata."""

from __future__ import annotations

import collections
import os
import sqlite3
import sys
import threading

from config import CACHEDIR

from .applog import get_logger
from .kopia import kopia

log = get_logger("cache")

try:  # orjson is ~3x faster at parsing the children JSON (the warm-walk hot path)
    import orjson

    _loads = orjson.loads  # direct alias - avoids a wrapper call per dir (hot path)

    def _dumps(obj) -> str:
        return orjson.dumps(obj).decode()
except ImportError:  # pragma: no cover - orjson is a declared dependency
    import json

    _loads = json.loads

    def _dumps(obj) -> str:
        return json.dumps(obj)

try:  # optional: without it the cache simply stays uncompressed
    import zstandard as _zstd
except ImportError:  # pragma: no cover
    _zstd = None

# The walk caches are ~11.5 GB of uncompressed JSON and are written once / read many, so ratio
# matters and compression speed does not. zstd's DECOMPRESSION speed is near level-independent,
# which is why the higher default costs nothing on the hot read path.
_LEVELS = {"none": 0, "zstd-1": 1, "zstd-3": 3}
_level: int | None = None
_LEVEL_LOCK = threading.Lock()

# 🚨 python-zstandard compressor/decompressor objects are NOT thread safe (documented), and this
# codec runs on the uvicorn request threadpool AND the in-process build thread at the same time.
# A shared context used reentrantly corrupts its internal buffers into a process-killing access
# violation - not an exception. One context per thread, mirroring the thread-local DuckDB cursors.
_TLS = threading.local()


def _zlevel() -> int:
    """Compression level from settings, read once. Lazy so the app and the worker subprocess
    behave identically without either needing a wiring step."""
    global _level
    if _level is None:
        with _LEVEL_LOCK:
            if _level is None:  # another thread may have loaded it while we waited
                try:
                    from ..settings import load_settings
                    name = load_settings().get("kopialens", {}).get("walk_cache_compression", "none")
                    _level = _LEVELS.get(str(name), 0) if _zstd else 0
                except Exception:  # noqa: BLE001 - a settings read must never break the walk
                    _level = 0
    return _level


def _cctx(level: int):
    """This thread's compressor for `level` (level is fixed at construction, so it is part of the key)."""
    got = getattr(_TLS, "cctx", None)
    if got is None or got[0] != level:
        got = (level, _zstd.ZstdCompressor(level=level))
        _TLS.cctx = got
    return got[1]


def _dctx():
    """This thread's decompressor."""
    got = getattr(_TLS, "dctx", None)
    if got is None:
        got = _TLS.dctx = _zstd.ZstdDecompressor()
    return got


def encode_json(obj) -> str | bytes:
    """JSON text, or a zstd frame when compression is on. Both are valid column values - sqlite is
    dynamically typed, so TEXT vs BLOB survives the round trip and IS the format discriminator."""
    raw = _dumps(obj)
    lvl = _zlevel()
    if not lvl:
        return raw
    return _cctx(lvl).compress(raw.encode("utf-8"))


def decode_json(value):
    """Read a value written by ``encode_json`` in EITHER format - a cache is always a mix while a
    transform is pending, and a row written before compression was enabled must still be readable."""
    if isinstance(value, (bytes, bytearray)):
        return _loads(_dctx().decompress(bytes(value)))
    return _loads(value)


BATCH = 800
# In-process LRU over directory entries, on TOP of the durable mmap'd SQLite dir cache. The SQLite
# cache is the source of truth; this memo only avoids SQLite round-trips + JSON re-parsing on the
# hot walk path, so a smaller budget just means a few more (fast) SQLite hits, never a kopia re-walk.
# Budgeted by BYTES, not item count - directory sizes here range from ~2 to 50,000+ children, so a
# fixed item cap is not a RAM budget (measured 200MB-7GB for the same 50k-item cap in production).
DIR_MEMO_MAX_BYTES = 1_500 * 1024 * 1024  # 1.5 GB default; overridden from settings at startup
DIR_MEMO: collections.OrderedDict[str, tuple[int, list[dict]]] = collections.OrderedDict()
_MEMO_BYTES = 0  # running total, kept in lockstep with DIR_MEMO under _MEMO_LOCK
_MEMO_LOCK = threading.Lock()  # guards the shared LRU across the request threadpool + build thread


def _entry_bytes(entries: list[dict]) -> int:
    """Shallow-ish size of one DIR_MEMO value (a list of child dicts): list + each dict + its
    str keys/values. Good enough for a RAM budget, not a precise heap walk."""
    total = sys.getsizeof(entries)
    for d in entries:
        total += sys.getsizeof(d)
        if isinstance(d, dict):
            for k, v in d.items():
                total += sys.getsizeof(k) + sys.getsizeof(v)
    return total


def memo_bytes() -> int:
    """Current exact DIR_MEMO footprint, for logging/diagnostics."""
    with _MEMO_LOCK:
        return _MEMO_BYTES


# Failed/unparseable kopia directory reads. A failed read is NEVER written to the cache (below) -
# the walk simply comes up short for that subtree, the next build retries, and this counter is what
# makes a RECURRING failure visible instead of silently missing rows forever (the KL5 class of bug).
_FAILED_READS: dict[str, int] = {}      # repo -> count
_FAILED_SAMPLE: dict[str, list[str]] = {}  # repo -> bounded sample of missed oids
_FAILED_LOCK = threading.Lock()
_FAILED_SAMPLE_MAX = 20


def _note_failed_read(repo: str, oids: list[str], detail: str) -> None:
    """Record + loudly log failed directory reads. Warnings stay per-call so a one-off is visible
    in the log stream; the counter is what the build result surfaces."""
    with _FAILED_LOCK:
        _FAILED_READS[repo] = _FAILED_READS.get(repo, 0) + len(oids)
        sample = _FAILED_SAMPLE.setdefault(repo, [])
        for oid in oids:
            if len(sample) < _FAILED_SAMPLE_MAX and oid not in sample:
                sample.append(oid)
    log.warning("kopia dir read FAILED (not cached, subtree skipped) repo=%s oids=%s: %s",
                repo, oids if len(oids) <= 5 else oids[:5] + ["..."], detail)


def read_failure_stats(repo: str) -> dict | None:
    """Drain this process's failed-read counters for one repo (used by the build/worker summary).
    Returns None when nothing failed, so callers can stay quiet on the happy path."""
    with _FAILED_LOCK:
        n = _FAILED_READS.pop(repo, 0)
        sample = _FAILED_SAMPLE.pop(repo, [])
    if not n:
        return None
    return {"count": n, "sample": sample}


def cache_db(repo: str) -> sqlite3.Connection:
    os.makedirs(CACHEDIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(CACHEDIR, f"repo{repo}.db"), timeout=60)
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-262144")    # 256 MB page cache
    db.execute("PRAGMA mmap_size=2147483648")  # 2 GB memory-map cap (mapped pages count as RSS)
    db.execute("CREATE TABLE IF NOT EXISTS dir_children (oid TEXT PRIMARY KEY, children TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS indirect (oid TEXT PRIMARY KEY, parts TEXT)")
    return db


def memo_put(oid: str, entries: list[dict]) -> None:
    global _MEMO_BYTES
    size = _entry_bytes(entries)
    with _MEMO_LOCK:
        old = DIR_MEMO.get(oid)
        if old is not None:
            _MEMO_BYTES -= old[0]
        DIR_MEMO[oid] = (size, entries)
        DIR_MEMO.move_to_end(oid)
        _MEMO_BYTES += size
        while _MEMO_BYTES > DIR_MEMO_MAX_BYTES and DIR_MEMO:
            _, (evicted_size, _) = DIR_MEMO.popitem(last=False)
            _MEMO_BYTES -= evicted_size


def get_children(repo: str, db: sqlite3.Connection, oids: list[str]) -> dict[str, list[dict]]:
    """Return directory entries, preserving the indirect-directory ``kopia show`` fix."""

    out: dict[str, list[dict]] = {}
    misses: list[str] = []
    with _MEMO_LOCK:
        for oid in oids:
            cached = DIR_MEMO.get(oid)
            if cached is not None:
                DIR_MEMO.move_to_end(oid)
                out[oid] = cached[1]
            else:
                misses.append(oid)

    cur = db.cursor()
    still: list[str] = []
    for i in range(0, len(misses), 900):
        group = misses[i : i + 900]
        if not group:
            continue
        found = dict(
            cur.execute(
                "SELECT oid, children FROM dir_children WHERE oid IN (%s)" % ",".join("?" * len(group)),
                group,
            ).fetchall()
        )
        for oid in group:
            row = found.get(oid)
            if row is None:
                still.append(oid)
                continue
            entries = decode_json(row)
            out[oid] = entries
            memo_put(oid, entries)

    wrote = False
    normal = [oid for oid in still if not oid.startswith("I")]
    indirect_dirs = [oid for oid in still if oid.startswith("I")]

    for i in range(0, len(normal), BATCH):
        group = normal[i : i + BATCH]
        res = kopia(repo, ["content", "show"] + group)
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        if res.returncode != 0:
            # kopia aborts a batch at the first unreadable content: the printed prefix is still
            # positionally paired (valid answers, cacheable), the unanswered tail is missing.
            _note_failed_read(repo, group[len(lines):],
                              f"rc={res.returncode} {(res.stderr or '').strip()[:200]}")
        for oid, line in zip(group, lines):
            try:
                entries = _loads(line).get("entries") or []
            # ValueError, NOT json.JSONDecodeError: `json` is imported only in the orjson-missing
            # branch above, so with orjson installed - the normal case here - this handler raised
            # NameError on the first piece of malformed kopia output, turning a recoverable parse
            # failure into a crash. Both libraries' decode errors derive from ValueError.
            except ValueError:
                _note_failed_read(repo, [oid], "unparseable content show output")
                continue  # never cache or memoize a failed read
            out[oid] = entries
            memo_put(oid, entries)
            db.execute("INSERT OR REPLACE INTO dir_children VALUES (?,?)", (oid, encode_json(entries)))
            wrote = True

    for oid in indirect_dirs:
        res = kopia(repo, ["show", oid])
        entries = None
        if res.returncode != 0:
            # a failed command is not an answer: empty stdout would parse as an error and then
            # read as an "empty folder" - the exact poisoning this module must never write down
            _note_failed_read(repo, [oid], f"rc={res.returncode} {(res.stderr or '').strip()[:200]}")
        else:
            try:
                entries = _loads(res.stdout).get("entries") or []
            except ValueError:  # see the note above - `json` is undefined when orjson is installed
                _note_failed_read(repo, [oid], "unparseable kopia show output")
        if entries is None:
            continue  # failed read: leave uncached and out of the answer, the next build retries
        out[oid] = entries
        memo_put(oid, entries)
        db.execute("INSERT OR REPLACE INTO dir_children VALUES (?,?)", (oid, encode_json(entries)))
        wrote = True

    if wrote:
        db.commit()
    return out
