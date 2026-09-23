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


def test_import_limited_to_one_target():
    one = kopia.policy_import_command("1", "p.json", target=SRC)
    assert one[1:] == ["policy", "import", "--from-file", "p.json", SRC]
    assert kopia.policy_import_command("1", "p.json", global_=True)[-1] == "--global"
    with pytest.raises(ValueError):
        kopia.policy_import_command("1", "p.json", mirror=True, target=SRC)


def test_policy_write_rules_imports_one_policy_file(tmp_path, monkeypatch):
    seen = {}

    def fake_run(repo, cmd, action, summary):
        path = cmd[cmd.index("--from-file") + 1]
        with open(path, encoding="utf-8") as fh:
            seen["file"] = json.load(fh)
        seen.update(cmd=cmd, action=action, summary=summary, path=path)
        return {"returncode": 0, "stdout": "", "stderr": "", "command": cmd}

    monkeypatch.setattr(kopia, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(kopia, "_run_policy_write", fake_run)
    pol = {"files": {"ignore": ["z", "a"]}}
    kopia.policy_write_rules_execute("1", SRC, False, pol, add=1)
    assert seen["file"] == {SRC: pol}
    assert seen["cmd"][-2:] == [SRC, "--no-progress"]
    assert seen["action"] == "policy-set" and seen["summary"]["add"] == 1
    assert not os.path.exists(seen["path"])  # temp file removed
    kopia.policy_write_rules_execute("1", None, True, pol)
    assert "(global)" in seen["file"] and "--global" in seen["cmd"]


def test_new_builders_reject_a_non_kopia_repo():
    for call in (
        lambda: kopia.policy_set_command("bz", target=SRC, add_ignore=["x"]),
        lambda: kopia.policy_delete_command("bz", target=SRC),
        lambda: kopia.policy_import_command("bz", "b.json"),
    ):
        with pytest.raises(ValueError):
            call()


def test_read_export_args_pass_the_readonly_gate(tmp_path, monkeypatch):
    """_read_export used to pass a FULL command (with the
    'kopia' binary prefix) into kopia(), which re-prefixes it and then the RO gate refuses its
    own doubled command - so every backup, and therefore every fail-closed save, failed. The
    args _read_export sends must pass the REAL gate untouched."""
    seen = {}

    def gate_and_fake(repo, args, **kw):
        kopia.check_readonly(args)  # the real boundary check; raises on a doubled command
        seen["args"] = args
        to_file = args[args.index("--to-file") + 1]
        with open(to_file, "w", encoding="utf-8") as fh:
            json.dump(GLOBAL_EXPORT, fh)
        return kopia.KopiaResult(args=args, returncode=0, stdout="", stderr="", elapsed=0.0)

    monkeypatch.setattr(policybackup, "kopia", gate_and_fake)
    monkeypatch.setattr(policybackup, "DATA_DIR", str(tmp_path))
    text = policybackup._read_export("1")
    assert json.loads(text)[SRC]["files"]["ignore"] == ["/Temp/**"]
    assert seen["args"][0] == "policy" and "--overwrite" in seen["args"]


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
        calls["content"] = open(file_path, encoding="utf-8").read()
        calls["mirror"] = mirror
        return {"returncode": 0, "stdout": "ok", "stderr": "", "command": ["kopia"]}

    monkeypatch.setattr(policybackup, "policy_import_execute", fake_import)
    monkeypatch.setattr(policybackup, "invalidate_after_write", lambda repo, targets=None: None)
    backup = policybackup.create_backup("1", "manual")
    out = policybackup.restore_backup("1", backup["name"], mirror=False)
    assert out["returncode"] == 0
    # kopia is handed a temp file holding the backup's text, removed the moment it returns
    assert os.path.basename(calls["file"]).startswith("policy_restore_")
    assert calls["content"] == policybackup.read_backup("1", backup["name"])
    assert not os.path.exists(calls["file"])
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


def test_drive_root_policies_are_found_not_read_as_global():
    """the ancestor walk used to yield 'G:' while kopia spells the
    drive-root target 'G:\\' - drive-root-owned sources read as global-inherited (7 of 85 on
    repo 1). The walk must produce the drive root in kopia's own spelling."""
    uh = "Anshul@anshul-desktop"
    drive_root = f"{uh}:G:{SEP}"
    defined = {"(global)": {"files": {"ignore": ["g1"]}},
               drive_root: {"files": {"ignore": ["driveroot-rule"]}}}
    src = f"{uh}:G:{SEP}GitHub{SEP}Example-Repo"
    assert effective_ignore_origin("1", src, defined) == ("inherited", drive_root)
    # the bare-drive spelling is never a candidate: only the real target matches
    defined_bare = {"(global)": {"files": {"ignore": ["g1"]}}, f"{uh}:G:": {"files": {"ignore": ["x"]}}}
    assert effective_ignore_origin("1", src, defined_bare) == ("global", "(global)")


def test_posix_root_policy_is_found():
    defined = {"(global)": {"files": {"ignore": ["g1"]}},
               "root@nas:/": {"files": {"ignore": ["root-rule"]}}}
    assert effective_ignore_origin("1", "root@nas:/var/log", defined) == ("inherited", "root@nas:/")
    defined_mid = {"(global)": {"files": {"ignore": ["g1"]}},
                   "root@nas:/etc": {"files": {"ignore": ["etc-rule"]}}}
    assert effective_ignore_origin("1", "root@nas:/etc/nginx", defined_mid) == \
        ("inherited", "root@nas:/etc")


# ---- invalidate_after_write reaches every source the write touches ---------------------

def _patch_refresh(monkeypatch, defined):
    import app.core.sources as sources_mod
    import app.core.policy as pol
    import app.index.policypricing as pp
    calls = {"one": [], "full": 0, "defined": 0, "price": 0}

    def _fake_refresh_defined(repo, force=False):
        calls["defined"] += 1
        return {"ts": 0.0, "policies": defined}, True

    monkeypatch.setattr(pol, "refresh_defined_policies", _fake_refresh_defined)
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})
    monkeypatch.setattr(pol, "refresh_one_policy",
                        lambda repo, key: calls["one"].append(key))
    monkeypatch.setattr(pol, "refresh_policies",
                        lambda repo, force=False: calls.__setitem__("full", calls["full"] + 1))
    monkeypatch.setattr(pp, "invalidate", lambda repo=None: calls.__setitem__("price", calls["price"] + 1))
    return calls


def test_invalidate_after_write_reaches_every_source_the_write_touches(monkeypatch):
    """a write reaches the source itself, every source
    whose rules are owned by the written target, and - for a Global write - every source that
    inherits global. A Global-only write used to refresh NOTHING (the effective view then mixed
    fresh defined rules with up-to-24h-stale effective ones)."""
    import app.core.sources as sources_mod
    import app.core.policy as pol
    uh = "u@h"
    written_target = f"{uh}:G:{SEP}GitHub"
    defined = {"(global)": {"files": {"ignore": ["g1"]}},
               written_target: {"files": {"ignore": ["gh"]}}}
    # three sources: the written one, one inheriting from it, one with its own list elsewhere
    monkeypatch.setattr(sources_mod, "sources_index", lambda repo: {
        written_target: [], f"{written_target}{SEP}PS": [], f"{uh}:C:{SEP}": []})
    calls = _patch_refresh(monkeypatch, defined)
    pol.invalidate_after_write("1", targets=[written_target])
    assert calls["one"] == [written_target, f"{written_target}{SEP}PS"]


def test_invalidate_after_global_write_refreshes_global_inheritors(monkeypatch):
    import app.core.sources as sources_mod
    import app.core.policy as pol
    uh = "u@h"
    defined = {"(global)": {"files": {"ignore": ["g1"]}},
               f"{uh}:C:{SEP}": {"files": {"ignore": ["own"]}}}
    # D:\ defines nothing -> inherits global -> must refresh; C:\ owns its list -> must not
    monkeypatch.setattr(sources_mod, "sources_index", lambda repo: {
        f"{uh}:C:{SEP}": [], f"{uh}:D:{SEP}": []})
    calls = _patch_refresh(monkeypatch, defined)
    pol.invalidate_after_write("1", targets=["(global)"])
    assert calls["one"] == [f"{uh}:D:{SEP}"]
    assert calls["defined"] == 1


def test_invalidate_without_targets_is_the_full_mirror_refresh(monkeypatch):
    import app.core.sources as sources_mod
    import app.core.policy as pol
    monkeypatch.setattr(sources_mod, "sources_index", lambda repo: {"u@h:C:\\".replace(chr(92), SEP): []})
    calls = _patch_refresh(monkeypatch, {"(global)": {}})
    pol.invalidate_after_write("1", targets=None)
    assert calls["full"] == 1 and calls["one"] == []


# ---- the ignore-only fast path ----------------------------------------------------

def _patch_fastpath(monkeypatch, defined, cached_policies):
    """Patch the module for fast-path tests: the export is 'fresh', the effective-policy cache
    is in memory, and show_policy COUNTS (the fast path must never call it)."""
    import app.core.sources as sources_mod
    import app.core.policy as pol
    import app.index.policypricing as pp
    calls = {"show": 0, "one": 0, "price": 0}
    monkeypatch.setattr(sources_mod, "sources_index",
                        lambda repo: {k: [] for k in cached_policies})
    monkeypatch.setattr(pol, "refresh_defined_policies",
                        lambda repo, force=False: ({"ts": 0.0, "policies": defined}, True))
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})
    monkeypatch.setattr(pol, "load_policies", lambda repo: {"ts": 0.0, "policies": dict(cached_policies)})
    written = {}
    monkeypatch.setattr(pol, "_write_policies", lambda repo, pols: written.update(pols))
    monkeypatch.setattr(pol, "show_policy", lambda repo, target: calls.__setitem__("show", calls["show"] + 1) or {})
    monkeypatch.setattr(pol, "refresh_one_policy", lambda repo, key: calls.__setitem__("one", calls["one"] + 1))
    monkeypatch.setattr(pp, "invalidate", lambda repo=None: calls.__setitem__("price", calls["price"] + 1))
    return calls, written


def test_ignore_only_write_patches_the_cache_with_zero_subprocesses(monkeypatch):
    """a Global add-ignore write used to fire one `policy show` per inheriting source
    inside the save request. The fast path patches files.ignore from the export it already took."""
    import app.core.policy as pol
    uh = "u@h"
    defined = {"(global)": {"files": {"ignore": ["g1", "NEW"]}},
               f"{uh}:C:{SEP}": {"files": {"ignore": ["own"]}}}
    cached = {
        f"{uh}:D:{SEP}": {"files": {"ignore": ["g1"]}, "retention": {"keepLatest": 7}},
        f"{uh}:C:{SEP}": {"files": {"ignore": ["own"]}},
    }
    calls, written = _patch_fastpath(monkeypatch, defined, cached)
    pol.invalidate_after_write("1", targets=["(global)"], ignore_only=True)
    assert calls["show"] == 0 and calls["one"] == 0
    # D:\ inherits global -> ignore patched to the new global list, other fields untouched
    assert written[f"{uh}:D:{SEP}"]["files"]["ignore"] == ["g1", "NEW"]
    assert written[f"{uh}:D:{SEP}"]["retention"] == {"keepLatest": 7}
    # C:\ owns its list -> untouched (its origin is not the written target)
    assert written[f"{uh}:C:{SEP}"]["files"]["ignore"] == ["own"]


def test_ignore_only_fast_path_matches_a_full_refresh(monkeypatch):
    """The fast path's answer must equal what the per-source refresh would have fetched."""
    import app.core.policy as pol
    uh = "u@h"
    owner = f"{uh}:G:{SEP}GitHub"
    inheritor = f"{owner}{SEP}PS"
    defined = {owner: {"files": {"ignore": ["gh", "NEW"]}}, "(global)": {}}
    cached = {owner: {"files": {"ignore": ["gh"]}}, inheritor: {"files": {"ignore": ["gh"]}}}
    calls, written = _patch_fastpath(monkeypatch, defined, cached)
    pol.invalidate_after_write("1", targets=[owner], ignore_only=True)
    assert calls["show"] == 0
    # the written target itself: own rules from the export; the inheritor: the owner's rules
    assert written[owner]["files"]["ignore"] == ["gh", "NEW"]
    assert written[inheritor]["files"]["ignore"] == ["gh", "NEW"]


def test_ignore_only_falls_back_when_a_source_is_not_cached(monkeypatch):
    """A source the cache never saw must be fetched properly, not patched from nothing."""
    import app.core.policy as pol
    uh = "u@h"
    defined = {"(global)": {"files": {"ignore": ["g1"]}}}
    cached = {f"{uh}:D:{SEP}": {"files": {"ignore": ["g1"]}}}
    calls, _written = _patch_fastpath(monkeypatch, defined, cached)
    monkeypatch.setattr("app.core.sources.sources_index",
                        lambda repo: {f"{uh}:D:{SEP}": [], f"{uh}:E:{SEP}": []})
    pol.invalidate_after_write("1", targets=["(global)"], ignore_only=True)
    assert calls["one"] >= 1  # the fallback refreshed the uncached source


def test_ignore_only_falls_back_on_delete_policy(monkeypatch):
    """delete_policy changes every field for inheritors - never eligible for the fast path."""
    import app.core.policy as pol
    uh = "u@h"
    defined = {"(global)": {"files": {"ignore": ["g1"]}}}
    calls, _written = _patch_fastpath(monkeypatch, defined, {f"{uh}:D:{SEP}": {}})
    pol.invalidate_after_write("1", targets=["(global)"], ignore_only=False)
    assert calls["one"] == 1 and calls["show"] == 0  # refresh_one_policy, not the patch


def test_ignore_only_falls_back_when_the_export_fails(monkeypatch):
    """A failed export keeps serving stale definitions; the fast path must not trust it."""
    import app.core.policy as pol
    import app.index.policypricing as pp
    calls = {"one": 0}
    monkeypatch.setattr("app.core.sources.sources_index", lambda repo: {})
    monkeypatch.setattr(pol, "refresh_defined_policies",
                        lambda repo, force=False: ({"ts": 0.0, "policies": {}}, False))
    monkeypatch.setattr(pol, "refresh_one_policy", lambda repo, key: calls.__setitem__("one", calls["one"] + 1))
    monkeypatch.setattr(pp, "invalidate", lambda repo=None: None)
    pol.invalidate_after_write("1", targets=["(global)"], ignore_only=True)
    assert calls["one"] == 0  # nothing was reached; the stale export was not patched from
