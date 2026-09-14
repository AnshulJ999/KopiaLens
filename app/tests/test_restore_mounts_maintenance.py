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
    first = restore._beside(str(orig), "2026-09-01T10:00:00Z")
    assert os.path.basename(first) == "notes (2026-09-01).txt"
    open(first, "w").close()
    assert os.path.basename(restore._beside(str(orig), "2026-09-01T10:00:00Z")) == "notes (2026-09-01 2).txt"


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
