"""Dry-run execution (the run bar's backend half).

Invariants pinned here:
- a dry-run command list carrying --commit / --delete is REFUSED, never run (a builder
  regression must not turn the "Dry run" button into a real mutation);
- the summary parsers read kopia's real output strings (verified against 0.23.1);
- empty input refuses without spawning anything.
"""

import subprocess

import pytest

from app.core.kopia import (
    TIMEOUT_RC,
    _parse_delete_dry,
    _parse_delete_line,
    _parse_remove_dry,
    _parse_remove_line,
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
    def fake_run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="kopia", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = _run_dry_commands("1", [["kopia", "diff", "x"]], "remove-files", "--commit")
    assert r["returncode"] == TIMEOUT_RC


def test_a_missing_kopia_fails_the_run_instead_of_reporting_success(monkeypatch):
    """The reviewed trap: the log-write guard used to swallow the subprocess failure too, so a
    missing kopia came back as rc 0 with empty output and the UI showed 'dry run checked'."""
    def fake_run(*_a, **_k):
        raise FileNotFoundError("kopia: not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = _run_dry_commands("1", [["kopia", "snapshot", "fix", "remove-files", "x"]],
                          "remove-files", "--commit")
    assert r["returncode"] != 0
    assert "error" in r and "kopia" in r["error"].lower()


def test_the_output_cap_does_not_stop_execution(monkeypatch):
    """The second reviewed trap: the 256 KB cap used to break the COMMAND loop, so later
    batches never ran while rc stayed 0 and the summary undercounted."""
    calls = []

    def fake_run(cmd, **_k):
        calls.append(cmd)

        class _R:
            returncode = 0
            stdout = "x" * (300 * 1024)  # one batch alone exceeds the cap
            stderr = ""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = _run_dry_commands("1", [["kopia", "a"], ["kopia", "b"]], "remove-files", "--commit",
                          parse_line=_parse_remove_line)
    assert len(calls) == 2          # both batches ran despite the cap
    assert r["truncated"] is True   # the response trims, the run does not stop
    assert r["returncode"] == 0


def test_the_line_parsers_accumulate_across_batches():
    acc: dict = {}
    for text in (["will remove file a.txt", "will remove file b.txt"],
                 ["Fixed 2 snapshots, but snapshot manifests were not updated."]):
        for ln in text:
            _parse_remove_line(ln, acc)
    assert acc["entries"] == 2 and acc["snapshots"] == 2
    acc = {}
    _parse_delete_line("Would delete snapshot g1 of g:\\x (pass --delete to confirm)", acc)
    _parse_delete_line("Would delete snapshot g2 of g:\\x (pass --delete to confirm)", acc)
    assert acc["snapshots"] == 2 and "2 snapshot(s)" in acc["final_line"]


def test_a_multi_batch_remove_summary_does_not_lose_the_fixed_count():
    """every batch prints its own 'Fixed N snapshots' line, so a later batch's 0 used to
    overwrite the total and read the whole multi-batch run as 0."""
    acc: dict = {}
    _parse_remove_line("Fixed 12 snapshots, but snapshot manifests were not updated.", acc)
    _parse_remove_line("Fixed 0 snapshots, but snapshot manifests were not updated.", acc)
    _parse_remove_line("will remove file late.txt", acc)
    assert acc["snapshots"] == 12 and acc["entries"] == 1
