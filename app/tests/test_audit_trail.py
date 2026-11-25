"""Audit trail: append-only, best-effort, one JSON object per line, optional actor field."""

from __future__ import annotations

import json

import pytest

from app.core import audit


@pytest.fixture(autouse=True)
def redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_path", lambda: str(tmp_path / "audit.jsonl"))


def test_a_record_round_trips_with_every_field():
    audit.audit("executed", "remove-files", repo="1", summary={"filenames": 2}, rc=0,
                duration_s=1.25, log_file="x.log", actor="anshul")
    rec = audit.read_tail(10)[0]
    assert rec["event"] == "executed"
    assert rec["action"] == "remove-files"
    assert rec["repo"] == "1"
    assert rec["summary"] == {"filenames": 2}
    assert rec["rc"] == 0
    assert rec["duration_s"] == 1.2
    assert rec["log_file"] == "x.log"
    assert rec["actor"] == "anshul"     # in the schema from the start (multi-user future-proofing)
    assert rec["ts"]


def test_optional_fields_default_to_none_but_exist():
    audit.audit("generated", "remove-plan")
    rec = audit.read_tail(10)[0]
    assert rec["repo"] is None
    assert rec["summary"] == {}
    # The actor defaults to the current identity (multi-user groundwork): every recorded action
    # says who did it, which is the part that cannot be backfilled later.
    from app.core import auth
    assert rec["actor"] == auth.current_actor()


def test_read_tail_returns_oldest_first_and_respects_the_limit():
    for i in range(7):
        audit.audit("executed", f"act{i}")
    got = audit.read_tail(3)
    assert [r["action"] for r in got] == ["act4", "act5", "act6"]


def test_a_corrupt_line_does_not_hide_the_rest(tmp_path):
    p = tmp_path / "audit.jsonl"
    audit.audit("executed", "good1")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{this is not json\n")
    audit.audit("executed", "good2")
    got = audit.read_tail(10)
    assert [r["action"] for r in got] == ["good1", "{this is not json", "good2"]
    assert got[1]["event"] == "corrupt"      # flagged, never silently dropped


def test_a_write_failure_never_raises_into_the_caller(monkeypatch):
    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(audit, "_path", _boom)
    audit.audit("executed", "remove-files")   # must not raise


def test_size_bytes_tracks_the_file(tmp_path):
    assert audit.size_bytes() == 0
    audit.audit("executed", "remove-files")
    assert audit.size_bytes() > 0
    recs = audit.read_tail(5)
    assert json.loads(open(tmp_path / "audit.jsonl", encoding="utf-8").readline())["action"] == "remove-files"
    assert recs[0]["action"] == "remove-files"
