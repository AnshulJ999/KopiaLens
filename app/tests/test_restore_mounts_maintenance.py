"""Restore planning helpers, mount mapping cleanup, and the maintenance job command."""

from __future__ import annotations

import os

from app.api import restore
from app.core import jobs, mounts

SRC = "me@pc:G:\\Data"


def test_restore_source_id_is_snapshot_plus_inner_path():
    assert restore._source_id(SRC, "G:\\Data\\Work\\notes.txt", "snap1") == "snap1/Work/notes.txt"
    assert restore._source_id(SRC, "G:\\Data", "snap1") == "snap1"


def test_flat_restore_names_are_unique():
    assert restore._flat_names(["a.txt", "b.txt", "A.txt", "a.txt"]) == ["a.txt", "b.txt", "A (2).txt", "a (3).txt"]


def test_beside_the_original_never_reuses_a_name(tmp_path):
    orig = tmp_path / "notes.txt"
    orig.write_text("x")
    first = restore._beside(str(orig), "2026-09-01T10:00:00Z", os.sep)
    assert os.path.basename(first) == "notes (2026-09-01).txt"
    open(first, "w").close()
    assert os.path.basename(restore._beside(str(orig), "2026-09-01T10:00:00Z", os.sep)) == "notes (2026-09-01 2).txt"


def test_beside_uses_the_sources_separator():
    assert restore._beside("/home/me/notes.txt", "2026-09-01", "/") == "/home/me/notes (2026-09-01).txt"
    assert restore._beside("C:\\Users\\me\\notes.txt", "2026-09-01", "\\") == "C:\\Users\\me\\notes (2026-09-01).txt"


def test_original_location_must_be_a_path_on_this_computer(monkeypatch):
    monkeypatch.setattr(restore.os, "name", "nt")
    assert restore._writable_here("C:\\Users\\me\\a.txt") and restore._writable_here("\\\\nas\\share\\a.txt")
    assert not restore._writable_here("/home/me/a.txt")
    monkeypatch.setattr(restore.os, "name", "posix")
    assert restore._writable_here("/home/me/a.txt") and not restore._writable_here("C:\\Users\\me\\a.txt")


def test_version_restore_prefers_the_picked_snapshot(monkeypatch):
    rows = [{"snapshot_id": "new", "start_time": "2026-09-10", "size": 5, "content_id": "c1"},
            {"snapshot_id": "mid", "start_time": "2026-09-05", "size": 6, "content_id": "c2"},
            {"snapshot_id": "old", "start_time": "2026-09-01", "size": 5, "content_id": "c1"}]
    monkeypatch.setattr(restore, "file_versions", lambda repo, s, p: rows)
    monkeypatch.setattr(restore, "sources_index", lambda repo: {SRC: [{"id": "new"}, {"id": "old"}]})
    item = restore.RestoreItem(source=SRC, full_path="G:\\Data\\a.txt", content_id="c1", snapshot_id="old")
    r = restore.resolve_item("3", item)
    assert r["snapshot_id"] == "old" and r["source_id"] == "old/a.txt"


def test_unticked_ignore_permission_errors_is_passed_to_kopia(monkeypatch):
    from app.core import kopia
    monkeypatch.setattr(kopia, "_repo_args", lambda repo: [])
    base = {"restore_owners": True, "restore_permissions": True, "restore_times": True}
    assert "--no-ignore-permission-errors" not in kopia.restore_command_full("3", "s", "t", {**base, "ignore_permission_errors": True})
    assert "--no-ignore-permission-errors" in kopia.restore_command_full("3", "s", "t", {**base, "ignore_permission_errors": False})
    assert "--no-ignore-permission-errors" not in kopia.restore_command_full("3", "s", "t", base)


def test_compare_counts_new_identical_and_different(tmp_path, monkeypatch):
    same = tmp_path / "same.txt"
    same.write_bytes(b"12345")
    os.utime(same, (1_700_000_000, 1_700_000_000))
    (tmp_path / "diff.txt").write_bytes(b"1")
    (tmp_path / "extra.txt").write_bytes(b"1")
    listing = [("same.txt", 5, "2023-11-14T22:13:20Z"), ("diff.txt", 9, ""), ("new.txt", 3, "")]
    monkeypatch.setattr(restore, "_listing", lambda repo, obj, kind, size, mtime: iter(listing))
    out = restore._compare("3", {"obj": "k1", "type": "d", "size": 0, "mtime": ""}, str(tmp_path), delete_extra=True)
    assert (out["new"], out["identical"], out["different"], out["extra"]) == (1, 1, 1, 1)
    assert out["extra_paths"] == [str(tmp_path / "extra.txt")]


def test_only_local_kopia_mappings_are_removed(monkeypatch):
    monkeypatch.setattr(mounts, "local_mappings", lambda: {"Z:": "\\\\127.0.0.1@53000\\DavWWWRoot"})
    ran = []
    monkeypatch.setattr(mounts.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    assert mounts.drop_mapping("N:") is False          # a real network share is never touched
    assert mounts.drop_mapping("C:\\mnt\\x") is False  # not a drive letter
    assert mounts.drop_mapping("z:") is True
    assert ran == [["net", "use", "Z:", "/delete", "/y"]]


def test_maintenance_job_builds_safety_and_mode(monkeypatch):
    monkeypatch.setattr(jobs, "maintenance_run_command", lambda repo: ["kopia", "maintenance", "run", "--full"])
    monkeypatch.setattr(jobs, "_repo_args", lambda repo: [])
    cmds, meta = jobs._build_commands("maintenance", "3", {"full": True, "safety": "none"})
    assert cmds == [["maintenance", "run", "--full", "--safety=none"]]
    assert meta["safety"] == "none"
    cmds, meta = jobs._build_commands("maintenance", "3", {"full": False})
    assert cmds == [["maintenance", "run"]] and meta["safety"] == "full"


def test_finished_job_from_before_a_restart_still_opens(tmp_path, monkeypatch):
    log = tmp_path / "job.log"
    log.write_text("=== job abc restore ===\nerror restoring: boom\n", encoding="utf-8")
    rec = {"job_id": "abc", "kind": "restore", "kind_label": "Restoring", "repo": "3", "label": "x",
           "dry": False, "mutating": False, "state": "failed", "rc": 1, "error": "exit 1", "created": 1.0,
           "started": 1.0, "finished": 2.0, "elapsed": 1.0, "total": 1, "done": 1, "errors": 1,
           "log_path": str(log), "detail": {}}
    monkeypatch.setattr(jobs, "read_job_history", lambda limit=200: [rec])
    s = jobs.history_status("abc")
    assert s["state"] == "failed" and s["from_history"] is True
    assert s["lines"][-1].endswith("error restoring: boom") and s["cursor"] == 2
    assert jobs.history_status("nope") is None
