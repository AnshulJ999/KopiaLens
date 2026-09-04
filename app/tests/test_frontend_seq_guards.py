"""Frontend sequence-guard invariants, pinned at source level.

There is no JS test runner here, so these read `app.js` the way `test_repo_kind_guard.py` reads
`core/kopia.py`: the point is to catch the NEXT loader that forgets a guard, not to re-test the
ones already fixed.

The bug these exist for: starting a new remove plan bumped its own counter but not the
reclaim-detail counter, so an in-flight reclaim calc still matched its guard and wrote figures for
the OLD file set into the NEW plan's panel - on the one screen whose job is saying what a delete
will free. It self-corrected below the 200k auto-compute threshold (a replacement calc bumped the
counter) and persisted above it, which is why it survived the first KL2 pass.
"""

from __future__ import annotations

import os
import re

import pytest

import config

APP_JS = os.path.join(config.HERE, "frontend", "js", "app.js")


def _body(name: str) -> str:
    """The source of one method, from its signature to the next same-indent closing brace."""
    src = open(APP_JS, encoding="utf-8").read()
    start = src.index(name)
    end = src.index("\n    },", start)
    return src[start:end]


@pytest.fixture(scope="module")
def fetch_plan() -> str:
    return _body("async _fetchRemovePlan()")


def test_a_new_plan_invalidates_an_in_flight_reclaim(fetch_plan):
    """THE test. Without this the stale reclaim lands in the new panel."""
    assert "++this._removeDetailSeq" in fetch_plan, (
        "_fetchRemovePlan must invalidate _removeDetailSeq: a reclaim calc already running is "
        "computing for the previous file set, and its own guard cannot detect a plan change.")


def test_the_invalidated_reclaim_does_not_leave_a_spinner(fetch_plan):
    """Invalidating means the old calc returns early and never clears its own flag."""
    assert "detailLoading = false" in fetch_plan, (
        "clearing _removeDetailSeq strands detailLoading=true unless it is reset here")


def test_the_plan_fetch_still_guards_itself(fetch_plan):
    assert "++this._removePlanSeq" in fetch_plan


def test_reclaim_still_checks_its_own_counter():
    body = _body("async removeComputeReclaim()")
    assert "++this._removeDetailSeq" in body
    assert re.search(r"seq !== this\._removeDetailSeq", body), (
        "removeComputeReclaim must still drop its result when superseded")
