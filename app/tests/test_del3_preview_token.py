""" A delete runs exactly the previewed list: the token, the moved-index note, and the
fallback when a preview has expired."""

from __future__ import annotations

from app.api import actions, jobs

SEP = chr(92)


def _remember(items=("o1",), generation=7):
    return actions._remember_plan({"items": list(items), "flag": "--object-id", "source": [f"u@h:C:{SEP}"],
                                   "by": "object-id", "generation": generation})


def test_the_job_runs_the_previewed_list_without_recomputing(monkeypatch):
    token = _remember()
    monkeypatch.setattr(actions, "index_generation", lambda repo: 7)
    monkeypatch.setattr(actions, "_remove_targets",
                        lambda rp: pytest_fail("the job must not re-derive a previewed plan"))
    out = jobs._resolve_params("remove_files", "3", {"form": "plan", "plan_token": token, "prefix": "x"})
    assert out["items"] == ["o1"] and out["resolved"] == "preview"
    assert "index_changed" not in out and "note" not in out


def test_a_moved_index_notes_it_and_still_runs_the_approved_list(monkeypatch):
    token = _remember(generation=7)
    monkeypatch.setattr(actions, "index_generation", lambda repo: 9)      # a reindex landed
    out = jobs._resolve_params("remove_files", "3", {"form": "plan", "plan_token": token})
    assert out["items"] == ["o1"] and out["index_changed"] is True
    assert "changed since your preview" in out["note"]


def test_a_missing_preview_falls_back_to_the_filter_and_says_so(monkeypatch):
    monkeypatch.setattr(actions, "_remove_targets", lambda rp: {
        "names": [], "object_ids": ["o2"], "pairs": [], "files": [], "sources": [], "capped": False})
    out = jobs._resolve_params("remove_files", "3",
                               {"form": "plan", "plan_token": "gone", "prefix": "x"})
    assert out["items"] == ["o2"] and out["resolved"] == "expired" and "expired" in out["note"]


def test_the_note_reaches_the_status_payload():
    from app.core import jobs as core_jobs

    job = {"job_id": "j1", "kind": "remove_files", "detail": {"note": "hello"},
           "dry_summary": None, "finished": None, "started": 0.0, "created": 0.0}
    assert core_jobs._serialize(job)["note"] == "hello"
    assert "note" not in core_jobs._serialize({**job, "detail": {}})


def pytest_fail(message):
    raise AssertionError(message)
