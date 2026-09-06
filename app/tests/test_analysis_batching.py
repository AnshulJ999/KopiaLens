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

    def arrow(self):
        return self._incoming

    def close(self):
        pass


@pytest.fixture
def published(monkeypatch):
    calls = []
    monkeypatch.setattr(snapanalysis, "read_cursor", lambda repo: _Cur([]))
    import app.index.publish as publish

    monkeypatch.setattr(publish, "replace_artifacts",
                        lambda repo, tables: calls.append((repo, tables["sanalysis"].num_rows)))
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
