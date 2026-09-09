"""Dry-run execution (the run bar's backend half).

Invariants pinned here:
- a dry-run command list carrying --commit / --delete is REFUSED, never run (a builder
  regression must not turn the "Dry run" button into a real mutation);
- the summary parsers read kopia's real output strings (verified against 0.23.1);
- empty input refuses without spawning anything.
"""

import pytest

from app.core.kopia import (
    TIMEOUT_RC,
    _parse_delete_dry,
    _parse_remove_dry,
    _run_dry_commands,
    remove_files_dry_run,
    snapshot_delete_dry_run,
)


def test_dry_run_refuses_a_command_with_the_mutating_flag():
    with pytest.raises(PermissionError):
        _run_dry_commands("1", [["kopia", "snapshot", "fix", "remove-files", "--commit"]], "remove-files", "--commit")
    with pytest.raises(PermissionError):
        _run_dry_commands("1", [["kopia", "snapshot", "delete", "abc", "--delete"]], "snapshot-delete", "--delete")


def test_dry_run_refuses_empty_input():
    r = remove_files_dry_run("1")
    assert r["returncode"] != 0 and "error" in r
    r = snapshot_delete_dry_run("1", [])
    assert r["returncode"] != 0 and "error" in r


def test_remove_dry_summary_parses_real_output():
    text = "\n".join([
        "Processing snapshot g:\\REAPER Media",
        "  2026-09-07 23:04 unchanged (abc123)",
        "will remove file Project Files/Mix v14.RPP",
        "will remove file Project Files/Mix v13.RPP",
        "  2026-09-06 23:02 replaced manifest from old1 to new1",
        "    diff old1 new1",
        "  2026-09-05 23:03 replaced manifest from old2 to new2",
        "Fixed 2 snapshots, but snapshot manifests were not updated. Pass --commit to update snapshots.",
    ])
    s = _parse_remove_dry(text)
    assert s["entries"] == 2
    assert s["snapshots"] == 2
    assert "snapshot manifests were not updated" in s["final_line"]


def test_remove_dry_summary_on_empty_output():
    s = _parse_remove_dry("")
    assert s["entries"] == 0 and s["snapshots"] == 0 and s["final_line"] == ""


def test_delete_dry_summary_parses_real_output():
    text = "\n".join([
        "Would delete snapshot gkjbbbbb of g:\\Media at 2026-09-07 23:04 (pass --delete to confirm)",
        "Would delete snapshot gkjbbbbb of g:\\Media at 2026-09-06 23:02 (pass --delete to confirm)",
    ])
    s = _parse_delete_dry(text)
    assert s["snapshots"] == 2
    assert "2 snapshot(s)" in s["final_line"]


def test_run_dry_commands_reports_timeout_distinctly(monkeypatch):
    import subprocess

    def fake_run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="kopia", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = _run_dry_commands("1", [["kopia", "diff", "x"]], "remove-files", "--commit")
    assert r["returncode"] == TIMEOUT_RC
