"""the dashboard payload must carry the cache snaphash.

The frontend's dashboard change detection keys its re-render signature on ``data.hash`` so a
change in ANY source's numbers re-renders, not just a change in the first row's last_time.
This pins the disk-cache read: it injects the blob's hash.
"""

from __future__ import annotations

import json

from app.core import metrics


def test_read_cache_injects_the_blob_hash(tmp_path, monkeypatch):
    from app.index import sidecache
    monkeypatch.setattr(sidecache, "CACHEDIR", str(tmp_path))
    sidecache.write(metrics._table_cache_name("1"),
                    {"version": metrics.TABLE_CACHE_VERSION, "hash": "h1", "data": {"rows": [1, 2]}})

    valid = metrics._read_cache("1", "h1")
    assert valid is not None
    assert valid["hash"] == "h1"    # the frontend keys its change detection on this
    assert valid["cached"] is True

    stale = metrics._read_cache("1", None)   # serve-stale path
    assert stale is not None
    assert stale["hash"] == "h1"    # the STALE blob's own hash - it describes the data served

    assert metrics._read_cache("1", "other") is None   # hash-valid check still rejects
