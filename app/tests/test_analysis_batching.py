"""One published generation per analysis stage, not one per source.

Every `_persist` publishes a generation. The analysis stage runs per source, so on a 100-source
repo that was 100 manifest rewrites in a single pass, and every reader rebinds its views on each
one. `batched()` collects the stage and publishes once.
"""

from __future__ import annotations

import pytest

from app.index import snapanalysis


class _Cur:
    """Enough of a cursor for the merge query: returns the rows it was given as an Arrow table."""

    def __init__(self, existing):
        self._existing = existing
        self._last = None

    def register(self, _name, table):
        self._incoming = table

    def unregister(self, _name):
        pass

    def execute(self, _sql, _params=None):
        return self

    def to_arrow_table(self):
        # Deliberately NOT `arrow()`: that returns a lazy reader in real DuckDB and the publisher
        # then writes an empty artifact. Going back to it must fail here, not in production.
        return self._incoming

    def close(self):
        pass


@pytest.fixture
def published(monkeypatch):
    calls = []
    monkeypatch.setattr(snapanalysis, "read_cursor", lambda repo: _Cur([]))
    import app.index.publish as publish

    def _replace(repo, tables):
        calls.append((repo, tables["sanalysis"].num_rows))
        return len(calls)          # a generation number, as the real publisher returns

    monkeypatch.setattr(publish, "replace_artifacts", _replace)
    snapanalysis._BATCH.clear()
    return calls


def _rows(n):
    return [{"id": f"snap{i}", "churn": i, "physical": i, "reclaimable": i} for i in range(n)]


def test_unbatched_persist_publishes_immediately(published):
    snapanalysis._persist("1", "srcA", _rows(2), "h")
    snapanalysis._persist("1", "srcB", _rows(3), "h")
    assert len(published) == 2, "outside a batch each source publishes its own generation"


def test_a_batch_publishes_once_with_every_source(published):
    with snapanalysis.batched("1"):
        snapanalysis._persist("1", "srcA", _rows(2), "h")
        snapanalysis._persist("1", "srcB", _rows(3), "h")
        assert not published, "nothing is published until the batch closes"
    assert len(published) == 1
    assert published[0][1] == 5, "the single publish carries every staged source's rows"


def test_an_empty_batch_publishes_nothing(published):
    with snapanalysis.batched("1"):
        pass
    assert not published


def test_a_failing_stage_still_publishes_what_it_staged(published):
    """The stage swallows per-source errors; whatever was computed before a raise must survive."""
    with pytest.raises(RuntimeError):
        with snapanalysis.batched("1"):
            snapanalysis._persist("1", "srcA", _rows(2), "h")
            raise RuntimeError("stage blew up")
    assert len(published) == 1 and published[0][1] == 2


def test_the_published_table_is_materialised_not_a_lazy_stream():
    """`_publish_analysis` unregisters `_sa_incoming` before the publisher consumes the result.
    With a lazy `.arrow()` reader the scan then finds nothing and the artifact publishes EMPTY,
    with no error - which is what happened to every analysis generation until 2026-09-07."""
    import duckdb
    import pyarrow as pa

    con = duckdb.connect()
    con.register("_inc", pa.table({"a": [1, 2, 3]}))
    lazy = con.execute("SELECT * FROM _inc").arrow()
    eager = con.execute("SELECT * FROM _inc").to_arrow_table()
    con.unregister("_inc")
    assert lazy.read_all().num_rows == 0, "a lazy reader is empty once its source is unregistered"
    assert eager.num_rows == 3, "a materialised table survives the unregister - use this one"


def test_a_declined_publish_is_reported_not_swallowed(published, monkeypatch):
    """Batching means ONE publish attempt for the whole pass, so a decline (another writer holds
    the repo) loses everything the pass computed - the caller must be able to see that."""
    import app.index.publish as publish_mod

    with snapanalysis.batched("1") as ok:          # the fixture's publisher returns a generation
        snapanalysis._persist("1", "srcA", _rows(2), "h")
    assert ok["saved"] is True

    # Never call monkeypatch.undo() here: the fixture patched read_cursor and replace_artifacts on
    # the SAME monkeypatch instance, so undoing restores the real ones and the next _persist
    # publishes a generation into the live data directory.
    monkeypatch.setattr(publish_mod, "replace_artifacts", lambda repo, tables: None)
    with snapanalysis.batched("1") as declined:
        snapanalysis._persist("1", "srcB", _rows(2), "h")
    assert declined["saved"] is False


def test_analyze_all_refuses_while_the_repo_is_indexing(monkeypatch):
    """The index pass holds the writer lock for its whole run, so starting here would compute
    every source and then have its single publish declined."""
    from app.api import snapshots

    monkeypatch.setattr(snapshots, "is_running", lambda repo: True)
    assert snapshots.analyze_all(repo="1")["started"] is False
    monkeypatch.setattr(snapshots, "is_running", lambda repo: False)
    monkeypatch.setattr(snapshots.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    assert snapshots.analyze_all(repo="1")["started"] is True
    snapshots._ANALYZING_ALL.discard("1")
