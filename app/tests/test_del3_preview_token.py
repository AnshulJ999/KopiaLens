"""A mutating delete runs exactly the previewed list: the token, the moved-index note, the
refusal when the preview is gone, and the dry-run fallback that may still re-plan."""

from __future__ import annotations

import pytest

from app.api import actions, jobs
from app.core import jobs as core_jobs

SEP = chr(92)


def _remember(items=("o1",), generation=7, repo="3"):
    return actions._remember_plan({"items": list(items), "flag": "--object-id",
                                   "source": [f"u@h:C:{SEP}"], "by": "object-id",
                                   "repo": repo, "generation": generation})


def test_the_job_runs_the_previewed_list_without_recomputing(monkeypatch):
    token = _remember()
    monkeypatch.setattr(actions, "index_generation", lambda repo: 7)
    monkeypatch.setattr(actions, "_remove_targets",
                        lambda rp: pytest.fail("the job must not re-derive a previewed plan"))
    out = jobs._resolve_params("remove_files", "3", {"form": "plan", "plan_token": token, "prefix": "x"})
    assert out["items"] == ["o1"] and out["resolved"] == "preview"
    assert "index_changed" not in out and "note" not in out


def test_a_moved_index_notes_it_and_still_runs_the_approved_list(monkeypatch):
    token = _remember(generation=7)
    monkeypatch.setattr(actions, "index_generation", lambda repo: 9)      # a reindex landed
    out = jobs._resolve_params("remove_files", "3", {"form": "plan", "plan_token": token})
    assert out["items"] == ["o1"] and out["index_changed"] is True
    assert "changed since your preview" in out["note"]


def test_an_expired_preview_refuses_a_mutating_delete(monkeypatch):
    """Option (a): the approved list is gone, so the delete refuses - never a silent re-plan."""
    monkeypatch.setattr(actions, "_remove_targets",
                        lambda rp: pytest.fail("an expired ticket must not re-derive a delete"))
    with pytest.raises(core_jobs.JobRefused):
        jobs._resolve_params("remove_files", "3", {"form": "plan", "plan_token": "gone", "prefix": "x"})


def test_a_mutating_plan_without_a_ticket_refuses(monkeypatch):
    monkeypatch.setattr(actions, "_remove_targets",
                        lambda rp: pytest.fail("a ticket-less delete must not re-derive"))
    with pytest.raises(core_jobs.JobRefused):
        jobs._resolve_params("remove_files", "3", {"form": "plan", "prefix": "x"})


def test_a_dry_run_still_falls_back_to_the_filter(monkeypatch):
    """Dry runs mutate nothing, so an expired ticket costs nothing - the old fallback stands."""
    monkeypatch.setattr(actions, "_remove_targets", lambda rp: {
        "names": [], "object_ids": ["o2"], "pairs": [], "files": [], "sources": [], "capped": False})
    out = jobs._resolve_params("remove_files", "3",
                               {"form": "plan", "plan_token": "gone", "prefix": "x", "dry": True})
    assert out["items"] == ["o2"] and out["resolved"] == "filter"


def test_a_ticket_from_another_repo_refuses(monkeypatch):
    token = _remember(repo="1")
    monkeypatch.setattr(actions, "_remove_targets",
                        lambda rp: pytest.fail("another repo's ticket must not delete here"))
    with pytest.raises(core_jobs.JobRefused):
        jobs._resolve_params("remove_files", "2", {"form": "plan", "plan_token": token, "prefix": "x"})


def test_the_note_reaches_the_status_payload():
    job = {"job_id": "j1", "kind": "remove_files", "detail": {"note": "hello"},
           "dry_summary": None, "finished": None, "started": 0.0, "created": 0.0}
    assert core_jobs._serialize(job)["note"] == "hello"
    assert "note" not in core_jobs._serialize({**job, "detail": {}})
