"""The freshness strip's bz row.

The badge shows REBUILD age (last_reindex_ts - how old the data actually is); verification age
rides last_check_ts for the tooltip, and a change waiting on the min-gap is `pending`. The kopia
rows keep last-success semantics - a bz row must never read the verification max as last_index.

Run with pytest or directly (`python app/tests/test_freshness_bz.py`).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from app.api import dashboard as d  # noqa: E402


@pytest.fixture
def bz_row(monkeypatch):
    def _build(info, success=999.0):
        monkeypatch.setattr(d, "REPOS", {"bz": {"name": "Backblaze Personal Backup", "kind": "backblaze"}})
        monkeypatch.setattr(d, "nightly_state", lambda k: {})
        # nightly_* / last_listing_error resolve lazily from app.main / app.core.sources at call time
        from app import main as m
        from app.core import sources
        monkeypatch.setattr(m, "nightly_ts", lambda k: 0.0)
        monkeypatch.setattr(m, "nightly_success_ts", lambda k: success)
        monkeypatch.setattr(m, "nightly_error", lambda k: {})
        monkeypatch.setattr(sources, "last_listing_error", lambda k: None)
        from app.backblaze import indexer
        monkeypatch.setattr(indexer, "freshness", lambda: info)
        return d.index_freshness()["repos"][0]
    return _build


def test_badge_shows_rebuild_age_not_the_verification_max(bz_row):
    # nightly success (999) is NEWER than the rebuild (100): the old mapping would have taken
    # the max and shown a fake "just verified" - the badge must read the rebuild only.
    row = bz_row({"last_check_ts": 500.0, "last_reindex_ts": 100.0, "pending": False})
    assert row["last_index"] == 100.0
    assert row["last_check_ts"] == 500.0
    assert row["pending"] is False


def test_pending_row_still_reports_rebuild_age(bz_row):
    row = bz_row({"last_check_ts": 900.0, "last_reindex_ts": 100.0, "pending": True})
    assert row["last_index"] == 100.0
    assert row["pending"] is True


def test_unbuilt_bz_reads_never(bz_row):
    row = bz_row({"last_check_ts": None, "last_reindex_ts": None, "pending": False})
    assert row["last_index"] is None
    assert row["last_check_ts"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
