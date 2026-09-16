"""Walk-cache value codec.

The invariant: a cache is ALWAYS a mix of formats while a transform is pending (and forever, if
one is never run), so the read path must accept both without being told which it is getting.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app.core import cache

ENTRIES = [{"name": "a.txt", "obj": "k1", "type": "f", "size": 10},
           {"name": "sub", "obj": "k2", "type": "d"}]


@pytest.fixture
def level(request):
    """Force a compression level, bypassing the settings read."""
    old = cache._level
    cache._level = request.param
    yield request.param
    cache._level = old


@pytest.mark.parametrize("level", [0, 1, 3], indirect=True)
def test_round_trip_at_every_level(level):
    assert cache.decode_json(cache.encode_json(ENTRIES)) == ENTRIES


@pytest.mark.parametrize("level", [0], indirect=True)
def test_level_zero_stores_plain_text(level):
    assert isinstance(cache.encode_json(ENTRIES), str)


@pytest.mark.parametrize("level", [3], indirect=True)
def test_a_compressed_value_is_bytes_and_smaller(level):
    blob = cache.encode_json(ENTRIES * 50)
    assert isinstance(blob, bytes)
    cache._level = 0
    assert len(blob) < len(cache.encode_json(ENTRIES * 50))


def test_both_formats_survive_a_sqlite_round_trip():
    """sqlite's dynamic typing is what discriminates the two formats - if TEXT came back as bytes
    (or vice versa) the decoder would guess wrong, so pin it against a real sqlite."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE dir_children (oid TEXT PRIMARY KEY, children TEXT)")
    cache._level = 0
    db.execute("INSERT INTO dir_children VALUES (?,?)", ("plain", cache.encode_json(ENTRIES)))
    cache._level = 3
    db.execute("INSERT INTO dir_children VALUES (?,?)", ("zstd", cache.encode_json(ENTRIES)))

    stored = dict(db.execute("SELECT oid, children FROM dir_children").fetchall())
    assert isinstance(stored["plain"], str)
    assert isinstance(stored["zstd"], (bytes, bytearray))
    # ...and the SAME reader handles both, whichever level is currently configured.
    for lvl in (0, 3):
        cache._level = lvl
        assert cache.decode_json(stored["plain"]) == ENTRIES
        assert cache.decode_json(stored["zstd"]) == ENTRIES
    cache._level = None


def test_an_empty_list_round_trips():
    """A directory with no children is common and must not decode to None."""
    for lvl in (0, 3):
        cache._level = lvl
        assert cache.decode_json(cache.encode_json([])) == []
    cache._level = None


# ---- regressions: two P1 bugs shipped 2026-09-05, both found in review -------------------------

def _hammer(fn, threads: int = 8, loops: int = 200) -> list:
    errors: list = []

    def run() -> None:
        try:
            for _ in range(loops):
                fn()
        except Exception as exc:  # noqa: BLE001 - collect, do not raise on a worker thread
            errors.append(exc)

    ts = [threading.Thread(target=run) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return errors


def test_decode_is_safe_under_concurrency():
    """Bug 2. python-zstandard contexts are NOT thread safe, and this codec runs on the request
    threadpool and the build thread at once. A shared context corrupts into an access violation -
    a process kill, not an exception - so this test passing at all IS the assertion."""
    cache._level = 1
    try:
        blob = cache.encode_json(ENTRIES * 20)
        assert isinstance(blob, bytes)
        errors = _hammer(lambda: cache.decode_json(blob))
        assert not errors, errors
    finally:
        cache._level = None


def test_encode_is_safe_under_concurrency():
    """Same contract on the write side - the build thread and a request can both populate the cache."""
    cache._level = 1
    try:
        errors = _hammer(lambda: cache.decode_json(cache.encode_json(ENTRIES)) == ENTRIES)
        assert not errors, errors
    finally:
        cache._level = None
