""" The delete buttons must send the preview's ticket, or the server refuses the job.

No JS runner here, so this pins the plan-form job starts at source level (the same shape as
test_frontend_seq_guards.py). The ticket is what makes a mutating delete run exactly the previewed
list - it was built on the server but never sent from the UI, which made the whole safeguard inert.
"""

from __future__ import annotations

import os

import config

JS = os.path.join(config.HERE, "frontend", "js")


def _body(filename: str, name: str) -> str:
    src = open(os.path.join(JS, filename), encoding="utf-8").read()
    start = src.index(name)
    end = src.index("\n    },", start)
    return src[start:end]


def test_the_remove_plan_delete_sends_the_ticket():
    body = _body("detail.js", "async removeRun()")
    assert "plan_token: this.removePlan.plan_token" in body, (
        "a mutating plan delete must carry the preview's ticket - the server refuses without one")
    assert "this._fetchRemovePlan()" in body, (
        "an expired ticket refuses server-side, so the panel must reload the fresh preview")


def test_the_folder_flow_delete_step_sends_the_ticket():
    body = _body("deletepanel.js", "flowDeleteSpec()")
    assert "plan_token" in body, (
        "the exclude-and-delete flow's remove step is a plan-form delete and needs the ticket")


def test_the_dry_run_sends_the_ticket_when_it_has_one():
    body = _body("detail.js", "removePlanDry()")
    assert "plan_token" in body
