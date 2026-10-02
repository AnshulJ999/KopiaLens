"""the dashboard payload must carry the cache snaphash.

The frontend's dashboard change detection keys its re-render signature on ``data.hash`` so a
change in ANY source's numbers re-renders, not just a change in the first row's last_time.
These pin the two serve paths: the disk-cache read injects the blob's hash, and a freshly
computed payload embeds the hash it was built for.
"""

from __future__ import annotations

import inspect
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


def test_computed_payload_embeds_the_build_hash():
    # _compute_table_rows needs a live repo (build_global, sizemap, ...); pin the contract at
    # source level instead: the fresh payload must carry the hash that _read_cache validates on,
    # plus computed_at so same-hash rebuilds (nightly next_snapshot changes) still flip the sig.
    src = inspect.getsource(metrics._compute_table_rows)
    assert '"hash": want_hash' in src
    assert '"computed_at"' in src
