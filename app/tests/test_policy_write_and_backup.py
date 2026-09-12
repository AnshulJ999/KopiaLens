"""Policy write path: command builders, the fail-closed backup, and the save/restore flow.

The backup rule is the point of the whole file (/ design section 4): a policy write
without a VERIFIED pre-write backup is refused. An export that kopia truncated must read as "no
backup", never as an empty one.
"""

from __future__ import annotations

import json
import os

import pytest

from app.core import kopia, policybackup
from app.core.policy import effective_ignore_origin, global_ignores
from app.core.policybackup import BackupError

SEP = chr(92)
SRC = f"Anshul@anshul-desktop:C:{SEP}Src"
GLOBAL_EXPORT = {"(global)": {"files": {"ignore": ["*.log", "node_modules"]}},
                 SRC: {"files": {"ignore": ["/Temp/**"]}}}


# ---- command builders ------------------------------------------------------------------

def test_policy_set_command_shape():
    cmd = kopia.policy_set_command("1", target=SRC, add_ignore=["/a/**"], remove_ignore=["b"])
    assert cmd[:5] == ["kopia", "policy", "set", SRC, "--remove-ignore"]
    assert cmd[5:7] == ["b", "--add-ignore"]
    assert cmd[7] == "/a/**"
    assert "--no-progress" not in cmd  # the executor appends the quiet flag, not the builder


def test_policy_set_command_global():
    cmd = kopia.policy_set_command("1", global_=True, add_ignore=["x"])
    assert cmd[:5] == ["kopia", "policy", "set", "--global", "--add-ignore"]


def test_policy_set_command_validates():
    with pytest.raises(ValueError):
        kopia.policy_set_command("1", target=SRC)  # no changes
    with pytest.raises(ValueError):
        kopia.policy_set_command("1", target=SRC, global_=True, add_ignore=["x"])
    with pytest.raises(ValueError):
        kopia.policy_set_command("1", add_ignore=["x"])  # no target at all


def test_policy_delete_and_import_commands():
    assert kopia.policy_delete_command("1", target=SRC)[1:4] == ["policy", "delete", SRC]
    assert "--global" in kopia.policy_delete_command("1", global_=True)
    imp = kopia.policy_import_command("1", "C:\\backup.json")
    assert imp[1:] == ["policy", "import", "--from-file", "C:\\backup.json"]
    assert "--delete-other-policies" not in imp
    assert kopia.policy_import_command("1", "b.json", mirror=True)[-1] == "--delete-other-policies"
    with pytest.raises(ValueError):
        kopia.policy_delete_command("1")  # no target
    with pytest.raises(ValueError):
        kopia.policy_import_command("1", "")


def test_new_builders_reject_a_non_kopia_repo():
    for call in (
        lambda: kopia.policy_set_command("bz", target=SRC, add_ignore=["x"]),
        lambda: kopia.policy_delete_command("bz", target=SRC),
        lambda: kopia.policy_import_command("bz", "b.json"),
        lambda: kopia.policy_export_command("bz", "e.json"),
    ):
        with pytest.raises(ValueError):
            call()


# ---- backups ---------------------------------------------------------------------------

def _fake_kopia_export(monkeypatch, payload=None, rc=0):
    """Replace policybackup.kopia with a writer that dumps `payload` to the --to-file path."""
    def fake_export(repo, args):
        to_file = args[args.index("--to-file") + 1]
        if rc == 0:
            with open(to_file, "w", encoding="utf-8") as fh:
                json.dump(payload or GLOBAL_EXPORT, fh)
        return kopia.KopiaResult(args=args, returncode=rc, stdout="", stderr="boom" if rc else "",
                                 elapsed=0.0)

    monkeypatch.setattr(policybackup, "kopia", fake_export)


def test_create_backup_exports_and_verifies(tmp_path, monkeypatch):
    _fake_kopia_export(monkeypatch)
    monkeypatch.setattr(policybackup, "DATA_DIR", str(tmp_path))
    info = policybackup.create_backup("1", "pre-write")
    assert info["verified"] is True
    with open(info["path"], encoding="utf-8") as fh:
        assert json.load(fh)[SRC]["files"]["ignore"] == ["/Temp/**"]
    assert os.path.basename(info["path"]).endswith("_pre-write.json")


def test_failed_export_is_a_backup_error_not_an_empty_backup(tmp_path, monkeypatch):
    _fake_kopia_export(monkeypatch, rc=1)
    monkeypatch.setattr(policybackup, "DATA_DIR", str(tmp_path))
    with pytest.raises(BackupError):
        policybackup.create_backup("1", "pre-write")
    assert policybackup.list_backups("1") == []


def test_truncated_export_fails_verification(tmp_path, monkeypatch):
    def fake_export(repo, args):
        to_file = args[args.index("--to-file") + 1]
        with open(to_file, "w", encoding="utf-8") as fh:
            fh.write('{"(global)": {"files": {"igno')  # kopia died mid-write
        return kopia.KopiaResult(args=args, returncode=0, stdout="", stderr="", elapsed=0.0)

    monkeypatch.setattr(policybackup, "kopia", fake_export)
    monkeypatch.setattr(policybackup, "DATA_DIR", str(tmp_path))
    with pytest.raises(BackupError):
        policybackup.create_backup("1", "manual")


def test_prune_keeps_the_cap_per_class(tmp_path, monkeypatch):
    monkeypatch.setattr(policybackup, "DATA_DIR", str(tmp_path))
    d = policybackup.backup_dir("1")
    os.makedirs(d, exist_ok=True)
    for i in range(28):  # 28 distinct, chronologically-sortable pre-write names
        name = f"202601{(i // 10) + 1:02d}_0000{i % 10:02d}_pre-write.json"
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write("{}")
    policybackup._prune("1")
    left = [e for e in policybackup.list_backups("1") if e["trigger"] == "pre-write"]
    assert len(left) == policybackup._KEEP["pre-write"]


def test_backup_names_are_validated_against_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(policybackup, "DATA_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        policybackup.read_backup("1", "..\\settings.json")
    with pytest.raises(FileNotFoundError):
        policybackup.read_backup("1", "not-a-backup.json")


def test_nightly_backup_skips_unchanged_content(tmp_path, monkeypatch):
    _fake_kopia_export(monkeypatch)
    monkeypatch.setattr(policybackup, "DATA_DIR", str(tmp_path))
    first = policybackup.nightly_backup("1")
    assert first["changed"] is True
    second = policybackup.nightly_backup("1")
    assert second["changed"] is False          # same export content -> no new file
    assert len([e for e in policybackup.list_backups("1") if e["trigger"] == "nightly"]) == 1


# ---- restore + the inheritance resolution ----------------------------------------------

def test_restore_makes_a_pre_restore_backup_first(tmp_path, monkeypatch):
    _fake_kopia_export(monkeypatch)
    monkeypatch.setattr(policybackup, "DATA_DIR", str(tmp_path))
    calls = {}

    def fake_import(repo, file_path, mirror=False):
        calls["file"] = file_path
        calls["mirror"] = mirror
        return {"returncode": 0, "stdout": "ok", "stderr": "", "command": ["kopia"]}

    monkeypatch.setattr(policybackup, "policy_import_execute", fake_import)
    monkeypatch.setattr(policybackup, "invalidate_after_write", lambda repo, targets=None: None)
    backup = policybackup.create_backup("1", "manual")
    out = policybackup.restore_backup("1", backup["name"], mirror=False)
    assert out["returncode"] == 0
    assert calls["file"].endswith(backup["name"])
    assert out["pre_restore_backup"]["name"].endswith("_pre-restore.json")
    assert calls["mirror"] is False
    with pytest.raises(FileNotFoundError):
        policybackup.restore_backup("1", "20260101_000000_manual.json")  # unknown name


def test_effective_ignore_origin_own_beats_global():
    defined = {"(global)": {"files": {"ignore": ["g1"]}},
               SRC: {"files": {"ignore": ["own1"]}}}
    assert effective_ignore_origin("1", SRC, defined) == ("own", SRC)
    assert global_ignores("1", defined) == ["g1"]


def test_effective_ignore_origin_walks_ancestors_then_global():
    parent = f"Anshul@anshul-desktop:C:{SEP}Data"
    deep = f"{parent}{SEP}Backups"
    defined = {"(global)": {"files": {"ignore": ["g1"]}},
               parent: {"files": {"ignore": ["p1"]}}}
    # a nested policy inherits from its path ancestors before the global policy
    origin, owner = effective_ignore_origin("1", deep, defined)
    assert origin == "inherited" and owner == parent
    # no ancestor rules -> global applies
    child = f"Anshul@anshul-desktop:C:{SEP}Other"
    assert effective_ignore_origin("1", child, defined) == ("global", "(global)")
    # no rules anywhere -> none
    defined["(global)"] = {}
    assert effective_ignore_origin("1", child, defined) == ("none", None)
