"""`sidecache` - the one-row-Parquet side caches.

Pins the three rules that matter: a round trip preserves the payload, a locked encrypted store is
never written in cleartext, and anything unreadable reads as "no cache" rather than raising.
"""

from __future__ import annotations

import os

from app.index import sidecache


def test_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecache, "CACHEDIR", str(tmp_path))
    assert sidecache.read("x") is None
    assert sidecache.write("x", {"a": 1, "b": [2, 3], "c": {"d": None}}) is True
    assert sidecache.read("x") == {"a": 1, "b": [2, 3], "c": {"d": None}}


def test_a_locked_encrypted_store_is_never_written_in_cleartext(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecache, "CACHEDIR", str(tmp_path))
    monkeypatch.setattr(sidecache.crypto, "enabled", lambda: True)
    monkeypatch.setattr(sidecache.crypto, "unlocked", lambda: False)
    assert sidecache.write("y", {"secret": 1}) is False
    assert not os.path.exists(sidecache.path("y"))
    assert sidecache.read("y") is None


def test_a_corrupt_file_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecache, "CACHEDIR", str(tmp_path))
    sidecache.write("z", {"ok": True})
    with open(sidecache.path("z"), "wb") as fh:
        fh.write(b"not a parquet file at all")
    assert sidecache.read("z") is None


def test_the_base_directory_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecache, "CACHEDIR", str(tmp_path / "default"))
    other = tmp_path / "other"
    assert sidecache.write("w", {"k": 1}, base=str(other)) is True
    assert os.path.exists(other / "w.parquet")
    assert sidecache.read("w", base=str(other)) == {"k": 1}
    assert sidecache.read("w") is None          # not in the default dir
