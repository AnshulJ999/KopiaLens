"""Connecting to a repository of any type kopia supports, and attaching existing connections
( backend).

Facts pinned here come from kopia's own source (cli/storage_*.go) and from a live check against
kopia 0.23.1: `connect from-config --file <same file>` re-persists a password; a prompt with no tty
fails in milliseconds; the repository password goes in KOPIA_PASSWORD, never in argv.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

import config


@pytest.fixture
def store(tmp_path, monkeypatch):
    from config import REPOS

    monkeypatch.setattr(config, "REPOS_FILE", str(tmp_path / "repos.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHEDIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "RECORDS", {"version": 1, "cache_dir": None, "repos": []})
    monkeypatch.setattr("app.core.audit._path", lambda: str(tmp_path / "audit.jsonl"))
    # status capture is a real kopia spawn - the flows under test must not need one
    from app.core import repos as repo_store
    monkeypatch.setattr(repo_store, "capture_status", lambda repo, status=None: {})
    snapshot = dict(REPOS)
    try:
        yield tmp_path
    finally:
        REPOS.clear()
        REPOS.update(snapshot)


@pytest.fixture
def fixed_binary(monkeypatch):
    from app.core import kopiabin
    monkeypatch.setattr(kopiabin, "resolve", lambda force=False: "kopia-fake")


def test_provider_list_covers_every_kopia_storage_type():
    from app.core import repoconnect
    keys = {p["key"] for p in repoconnect.provider_list()}
    assert keys == {"filesystem", "sftp", "s3", "b2", "gcs", "azure", "webdav", "rclone",
                    "gdrive", "server", "token"}
    by_key = {p["key"]: p for p in repoconnect.provider_list()}
    assert by_key["token"]["password"] is False      # the token carries the password
    assert all(p["password"] is True for k, p in by_key.items() if k != "token")
    assert all(f["key"] for p in by_key.values() for f in p["fields"])
    # a server connection and a token cannot host a NEW repository - only the 9 storage types can
    assert {k for k, p in by_key.items() if not p["can_create"]} == {"server", "token"}


def test_validate_fields_reports_what_is_missing():
    from app.core import repoconnect
    assert repoconnect.validate_fields("filesystem", {"path": r"E:\Kopia"}) == []
    missing = repoconnect.validate_fields("filesystem", {})
    assert missing and "Folder" in missing[0]
    # SFTP needs credentials and host verification, one of each group
    sftp = {"path": "/repo", "host": "nas", "username": "u"}
    assert any("Password" in m for m in repoconnect.validate_fields("sftp", sftp))
    sftp.update({"keyfile": "/home/u/.ssh/id_ed25519", "known_hosts": "/home/u/.ssh/known_hosts"})
    assert repoconnect.validate_fields("sftp", sftp) == []
    # Azure accepts a key, a SAS token, or a service principal
    az = {"container": "c", "storage_account": "a"}
    assert any("Account key" in m for m in repoconnect.validate_fields("azure", az))
    az["tenant_id"] = "t"
    assert repoconnect.validate_fields("azure", az) == []


def test_connect_args_never_carry_the_password(fixed_binary):
    from app.core import repoconnect
    args = repoconnect.build_connect_args(
        "filesystem", {"path": r"E:\Kopia", "name": None}, r"C:\data\configs\repo1.config", "s3cret!")
    assert args == ["kopia-fake", "--config-file", r"C:\data\configs\repo1.config", "repository",
                    "connect", "filesystem", "--path", r"E:\Kopia", "--no-check-for-updates"]
    assert "s3cret!" not in args


def test_connect_args_bool_fields_and_defaults(fixed_binary):
    from app.core import repoconnect
    base = {"path": "/repo"}
    assert "--flat" in repoconnect.build_connect_args("filesystem", {**base, "flat": True}, "c", "p")
    assert "--flat" not in repoconnect.build_connect_args("filesystem", {**base, "flat": False}, "c", "p")
    # rclone's --atomic-writes DEFAULTS TO TRUE: unchecking it must send the negated form, not the flag
    args = repoconnect.build_connect_args("rclone", {"remote_path": "d:k", "atomic_writes": False}, "c", "p")
    assert "--no-atomic-writes" in args and "--atomic-writes" not in args
    args = repoconnect.build_connect_args("rclone", {"remote_path": "d:k", "atomic_writes": True}, "c", "p")
    assert "--atomic-writes" not in args and "--no-atomic-writes" not in args


def test_create_args_carry_the_verb_and_the_format_options(fixed_binary):
    from app.core import repoconnect
    args = repoconnect.build_create_args(
        "filesystem", {"path": "E:/NewRepo"}, "/data/configs/repo5.config",
        {"block_hash": "BLAKE2B-256-128", "format_version": 2, "ecc_overhead_percent": 0})
    assert args[:6] == ["kopia-fake", "--config-file", "/data/configs/repo5.config",
                        "repository", "create", "filesystem"]
    assert args[6:8] == ["--path", "E:/NewRepo"]
    assert "--block-hash" in args and "--format-version" in args
    assert "--ecc-overhead-percent" not in args        # 0 means "leave it at kopia's default"
    # a server connection and a token connect to an EXISTING repository only
    with pytest.raises(ValueError, match="connect to one instead"):
        repoconnect.build_create_args("server", {"url": "https://x:51515"}, "c", None)
    with pytest.raises(ValueError, match="connect to one instead"):
        repoconnect.build_create_args("token", {"token": "t"}, "c", None)


def test_create_records_a_created_repo(store, fixed_binary, monkeypatch):
    from app.core import repoconnect
    seen = {}

    def fake_run(args, password, timeout=600.0):
        seen["args"] = args
        open(args[2], "w", encoding="utf-8").write("{}")
        return {"returncode": 0, "stdout": "Initializing repository with:", "stderr": "",
                "command": args}

    monkeypatch.setattr(repoconnect, "_run", fake_run)
    rec = repoconnect.create("filesystem", {"path": "E:/NewRepo"}, "pw", "Fresh")
    assert rec["source"] == "created" and rec["id"] == "1"
    assert "create" in seen["args"] and "connect" not in seen["args"]
    assert os.path.isfile(rec["config_file"])


def test_config_location_defaults_by_profile_and_can_be_overridden(store, monkeypatch):
    """Where a CREATED connection keeps its kopia config: kopia's own folder on a desktop install
    (so KopiaUI and the kopia CLI see it too), KopiaLens's data folder on a server/Docker install
    (a container's data folder is the mounted volume; kopia's folder inside the container is not).
    Env beats the setting beats the profile - the same order as the bind default."""
    from app import settings as settings_mod
    from app.core import repoconnect

    monkeypatch.delenv("KOPIALENS_KOPIA_CONFIG_LOCATION", raising=False)
    monkeypatch.delenv("KOPIALENS_PROFILE", raising=False)
    (store / "settings.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(store / "settings.json"))
    assert repoconnect.config_location() == "data"          # no tray in the test = server

    monkeypatch.setenv("KOPIALENS_PROFILE", "desktop")
    assert repoconnect.config_location() == "kopia"

    (store / "settings.json").write_text(json.dumps({"kopialens": {"kopia_config_location": "data"}}),
                                         encoding="utf-8")
    assert repoconnect.config_location() == "data"          # explicit choice beats the profile

    monkeypatch.setenv("KOPIALENS_KOPIA_CONFIG_LOCATION", "kopia")
    assert repoconnect.config_location() == "kopia"         # env beats everything


def test_managed_config_paths_and_ownership(store, monkeypatch):
    from app import settings as settings_mod
    from app.core import repoconnect

    monkeypatch.setattr(repoconnect, "kopia_config_dir", lambda: str(store / "kopia"))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(store / "settings.json"))
    monkeypatch.delenv("KOPIALENS_KOPIA_CONFIG_LOCATION", raising=False)

    monkeypatch.setenv("KOPIALENS_PROFILE", "desktop")
    path = repoconnect.managed_config_path("7")
    assert os.path.basename(path) == "repository-kopialens-7.config"   # KopiaUI's own convention
    assert os.path.dirname(path) == str(store / "kopia")
    assert repoconnect.is_managed_config(path) is True
    # a config KopiaUI made itself is NOT ours to delete
    theirs = store / "kopia" / "repository-1754765417479.config"
    assert repoconnect.is_managed_config(str(theirs)) is False
    # an unrelated path is never ours either
    assert repoconnect.is_managed_config(str(store / "notes.config")) is False
    assert repoconnect.is_managed_config(None) is False

    monkeypatch.setenv("KOPIALENS_KOPIA_CONFIG_LOCATION", "data")
    path = repoconnect.managed_config_path("7")
    assert path == os.path.join(str(store), "configs", "repo7.config")
    assert repoconnect.is_managed_config(path) is True


def test_detect_configs_scans_both_folders(store, monkeypatch):
    from app.core import repoconnect
    kopia_dir = store / "kopia"
    kopia_dir.mkdir()
    data_dir = store / "configs"
    data_dir.mkdir()
    (kopia_dir / "repository.config").write_text(json.dumps({"storage": {"type": "filesystem"}}),
                                                 encoding="utf-8")
    ours = data_dir / "repo7.config"
    ours.write_text(json.dumps({"storage": {"type": "b2"}}), encoding="utf-8")
    monkeypatch.setattr(repoconnect, "kopia_config_dir", lambda: str(kopia_dir))

    found = repoconnect.detect_configs()
    by_name = {f["filename"]: f for f in found}
    assert set(by_name) == {"repository.config", "repo7.config"}
    assert by_name["repository.config"]["folder"] == "kopia"
    assert by_name["repository.config"]["managed"] is False
    assert by_name["repo7.config"]["folder"] == "data" and by_name["repo7.config"]["managed"] is True


def test_create_in_the_kopia_config_folder(store, fixed_binary, monkeypatch):
    """The default-created connection lands in kopia's folder with KopiaUI's naming, and a remove
    with delete_config takes exactly that file."""
    from app import settings as settings_mod
    from app.core import repoconnect

    kopia_dir = store / "kopia"
    kopia_dir.mkdir()
    monkeypatch.setattr(repoconnect, "kopia_config_dir", lambda: str(kopia_dir))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(store / "settings.json"))
    monkeypatch.setenv("KOPIALENS_PROFILE", "desktop")
    seen = {}

    def fake_run(args, password, timeout=600.0):
        seen["args"] = args
        open(args[2], "w", encoding="utf-8").write("{}")
        return {"returncode": 0, "stdout": "", "stderr": "", "command": args}

    monkeypatch.setattr(repoconnect, "_run", fake_run)
    rec = repoconnect.create("filesystem", {"path": "/repo"}, "pw")
    assert rec["config_file"] == str(kopia_dir / "repository-kopialens-1.config")
    assert os.path.isfile(rec["config_file"])
    repoconnect.remove("1", delete_config=True)
    assert not os.path.exists(rec["config_file"])


def test_no_flow_leaks_a_storage_credential(store, fixed_binary, monkeypatch):
    """The whole point of scrubbing on read: a config file's credentials must not reach
    detect-configs, attach, probe, repos.json or /api/repos - while the display fields stay."""
    from app.core import repoconnect, repos
    cfg = store / "repository-cloud.config"
    cfg.write_text(json.dumps({
        "storage": {"type": "s3", "config": {
            "bucket": "my-backups", "endpoint": "s3.amazonaws.com", "prefix": "kopia/",
            "accessKeyID": "AKIA123", "secretAccessKey": "TOPSECRET", "sessionToken": "TOK"}}}),
        encoding="utf-8")
    monkeypatch.setattr(repoconnect, "kopia_config_dir", lambda: str(store))

    facts = repoconnect.describe_config(str(cfg))
    assert facts["config"]["secretAccessKey"] == repos.SCRUBBED
    assert facts["config"]["sessionToken"] == repos.SCRUBBED
    assert facts["config"]["bucket"] == "my-backups" and facts["config"]["accessKeyID"] == "AKIA123"

    listed = {f["filename"]: f for f in repoconnect.detect_configs()}
    assert "TOPSECRET" not in json.dumps(listed)

    rec = repoconnect.attach(str(cfg), "Cloud")   # capture_status is a no-op in this suite
    assert "TOPSECRET" not in json.dumps(rec)
    assert rec["storage"]["config"]["bucket"] == "my-backups"
    # a live status reading is scrubbed before it is stored, too
    repos.capture_status(rec["id"], {"storage": {"type": "s3", "config": {"bucket": "my-backups",
                                                                         "secretAccessKey": "TOPSECRET"}}})
    assert "TOPSECRET" not in json.dumps(config.RECORDS)
    assert "TOPSECRET" not in json.dumps(repos.detail(config.repo_record(rec["id"])))

    monkeypatch.setattr(repoconnect, "_run", lambda args, password, timeout=600.0: {
        "returncode": 0, "stdout": json.dumps({"uniqueIDHex": "a" * 64, "storage": {
            "type": "s3", "config": {"bucket": "my-backups", "secretAccessKey": "TOPSECRET"}}}),
        "stderr": "", "command": args})
    out = repoconnect.probe(repo=rec["id"])
    assert "TOPSECRET" not in json.dumps(out) and out["config"]["bucket"] == "my-backups"


def test_connect_records_a_managed_config(store, fixed_binary, monkeypatch):
    from app.core import repoconnect
    seen = {}

    def fake_run(args, password, timeout=600.0):
        seen["args"], seen["password"] = args, password
        open(args[2], "w", encoding="utf-8").write("{}")   # kopia wrote the config
        return {"returncode": 0, "stdout": "Connected to repository.", "stderr": "", "command": args}

    monkeypatch.setattr(repoconnect, "_run", fake_run)
    rec = repoconnect.connect("filesystem", {"path": r"E:\Kopia"}, "pw", "Main backup", "Main")
    assert rec["id"] == "1" and rec["source"] == "connected"
    assert rec["config_file"] == os.path.join(str(store), "configs", "repo1.config")
    assert os.path.isfile(rec["config_file"])
    assert rec["counts_as_backup"] is True
    assert seen["password"] == "pw"
    assert "1" in config.REPOS


def test_connect_failure_leaves_nothing_behind(store, fixed_binary, monkeypatch):
    from app.core import repoconnect

    def fake_run(args, password, timeout=600.0):
        open(args[2], "w", encoding="utf-8").write("{}")
        return {"returncode": 1, "stdout": "", "stderr": "error connecting to repository: no such bucket",
                "command": args}

    monkeypatch.setattr(repoconnect, "_run", fake_run)
    with pytest.raises(ValueError, match="no such bucket"):
        repoconnect.connect("filesystem", {"path": r"E:\Kopia"}, "pw")
    assert config.RECORDS["repos"] == []
    assert not os.path.exists(os.path.join(str(store), "configs", "repo1.config"))


def test_connect_refuses_a_missing_password_or_field(store, fixed_binary):
    from app.core import repoconnect
    with pytest.raises(ValueError, match="Folder"):
        repoconnect.connect("filesystem", {}, "pw")
    with pytest.raises(ValueError, match="password is required"):
        repoconnect.connect("filesystem", {"path": r"E:\Kopia"}, "")


def test_describe_config_reads_both_connection_shapes(store):
    from app.core import repoconnect
    storage_cfg = store / "repository-1.config"
    storage_cfg.write_text(json.dumps({"storage": {"type": "s3", "config": {"bucket": "b"}}}),
                           encoding="utf-8")
    assert repoconnect.describe_config(str(storage_cfg))["kind"] == "cloud"
    server_cfg = store / "repository-2.config"
    server_cfg.write_text(json.dumps({"apiServer": {"baseURL": "https://kopia.local:51515"}}),
                          encoding="utf-8")
    facts = repoconnect.describe_config(str(server_cfg))
    assert facts["type"] == "server" and facts["config"]["url"] == "https://kopia.local:51515"
    assert repoconnect.describe_config(str(store / "nope.config"))["type"] == ""


def test_detect_configs_marks_what_is_already_in_the_store(store, monkeypatch):
    from app.core import repoconnect
    cfgdir = store / "kopia"
    cfgdir.mkdir()
    (cfgdir / "repository.config").write_text(json.dumps({"storage": {"type": "filesystem"}}), encoding="utf-8")
    (cfgdir / "repository-99.config").write_text(json.dumps({"storage": {"type": "b2"}}), encoding="utf-8")
    (cfgdir / "not-a-config.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(repoconnect, "kopia_config_dir", lambda: str(cfgdir))
    found = repoconnect.detect_configs()
    assert [f["filename"] for f in found] == ["repository-99.config", "repository.config"]
    assert all(f["in_store"] is False for f in found)
    from app.core import repos
    repos.add_record({"name": "Default", "config_file": None})   # uses kopia's default connection
    found = repoconnect.detect_configs()
    default = next(f for f in found if f["is_default"])
    assert default["in_store"] is True and default["repo_id"] == "1"


def test_probe_parses_status_and_falls_back_to_the_config_for_a_server(store, fixed_binary, monkeypatch):
    from app.core import repoconnect
    status = {"uniqueIDHex": "a" * 64, "storage": {"type": "s3", "config": {"bucket": "b"}},
              "volume": {"capacity": 100, "available": 40}}
    monkeypatch.setattr(repoconnect, "_run",
                        lambda args, password, timeout=600.0: {"returncode": 0, "stdout": json.dumps(status),
                                                               "stderr": "", "command": args})
    out = repoconnect.probe(config_file=None)
    assert out["ok"] and out["unique_id"] == "a" * 64 and out["kind"] == "cloud"
    assert out["capacity"] == 100 and out["available"] == 40

    # A repository-server connection reports no storage through `repository status`
    server_cfg = store / "srv.config"
    server_cfg.write_text(json.dumps({"apiServer": {"baseURL": "https://kopia.local:51515"}}), encoding="utf-8")
    monkeypatch.setattr(repoconnect, "_run",
                        lambda args, password, timeout=600.0: {"returncode": 0, "stdout": "{}",
                                                               "stderr": "", "command": args})
    out = repoconnect.probe(config_file=str(server_cfg))
    assert out["ok"] and out["type"] == "server" and out["kind"] == "server"


def test_probe_reports_a_failure_instead_of_raising(store, fixed_binary, monkeypatch):
    from app.core import repoconnect
    monkeypatch.setattr(repoconnect, "_run",
                        lambda args, password, timeout=600.0: {"returncode": 1, "stdout": "",
                                                               "stderr": "ERROR: cannot open repository",
                                                               "command": args})
    out = repoconnect.probe(config_file=None)
    assert out["ok"] is False and out["error"] == "ERROR: cannot open repository"


def test_attach_points_at_the_existing_config_and_refuses_a_copy_of_one(store, fixed_binary, monkeypatch):
    from app.core import repoconnect
    cfg = store / "repository-1.config"
    cfg.write_text(json.dumps({"storage": {"type": "filesystem"}}), encoding="utf-8")
    rec = repoconnect.attach(str(cfg), "KopiaUI repo", "KU")
    assert rec["source"] == "attached" and rec["config_file"] == str(cfg)
    assert os.path.isfile(str(cfg))          # never moved or copied
    with pytest.raises(ValueError, match="already attached"):
        repoconnect.attach(str(cfg))
    # the default connection is a single slot too
    rec2 = repoconnect.attach(None, "Default")
    assert rec2["config_file"] is None
    with pytest.raises(ValueError, match="already attached"):
        repoconnect.attach(None)


def test_attach_rejects_a_file_that_is_not_a_kopia_config(store, fixed_binary):
    from app.core import repoconnect
    junk = store / "notes.txt"
    junk.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="not a kopia connection config"):
        repoconnect.attach(str(junk))


def test_unlock_uses_from_config_for_storage_and_the_url_for_a_server(store, fixed_binary, monkeypatch):
    from app.core import repoconnect
    cfg = store / "repository-1.config"
    cfg.write_text(json.dumps({"storage": {"type": "filesystem"}}), encoding="utf-8")
    srv = store / "repository-2.config"
    srv.write_text(json.dumps({"apiServer": {"baseURL": "https://kopia.local:51515"}}), encoding="utf-8")
    repoconnect.attach(str(cfg), "A")
    repoconnect.attach(str(srv), "B")
    monkeypatch.setattr(repoconnect, "probe", lambda config_file=None, repo=None: {"ok": True})
    calls = []

    def fake_run(args, password, timeout=600.0):
        calls.append((args, password))
        return {"returncode": 0, "stdout": "Connected to repository.", "stderr": "", "command": args}

    monkeypatch.setattr(repoconnect, "_run", fake_run)
    assert repoconnect.unlock("1", "pw")["ok"] is True
    assert "from-config" in calls[0][0] and str(cfg) in calls[0][0]
    assert calls[0][1] == "pw"
    repoconnect.unlock("2", "pw")
    assert "server" in calls[1][0] and "https://kopia.local:51515" in calls[1][0]
    with pytest.raises(ValueError, match="Enter the repository password"):
        repoconnect.unlock("1", "  ")


def test_remove_deletes_only_this_repos_files(store, fixed_binary, monkeypatch):
    from app.core import repoconnect, repos
    cfg = store / "repository-1.config"
    cfg.write_text(json.dumps({"storage": {"type": "filesystem", "config": {"path": "/repo"}}}),
                   encoding="utf-8")
    repoconnect.attach(str(cfg), "A")
    (store / "repo1").mkdir()
    (store / "repo1" / "manifest.json").write_text("{}", encoding="utf-8")
    (store / "repo12").mkdir()
    (store / "cache").mkdir(exist_ok=True)
    (store / "cache" / "churn_repo1_30d_d4.parquet").write_text("x", encoding="utf-8")

    preview = repoconnect.remove_preview("1")
    names = {os.path.basename(p) for p in preview["paths"]}
    assert names == {"repo1", "churn_repo1_30d_d4.parquet"}
    assert preview["bytes"] > 0 and preview["managed_config"] is False

    out = repoconnect.remove("1")
    assert out["ok"] and not out["failed"]
    assert not (store / "repo1").exists()
    assert (store / "repo12").exists()          # a different repo's folder is untouched
    assert cfg.exists()                          # an attached config is never deleted
    assert repos.all_records() == []


def test_remove_can_delete_a_config_kopialens_created(store, fixed_binary, monkeypatch):
    from app.core import repoconnect
    monkeypatch.setattr(repoconnect, "_run", lambda args, password, timeout=600.0: {
        "returncode": 0, "stdout": "", "stderr": "", "command": args})
    rec = repoconnect.connect("filesystem", {"path": "/repo"}, "pw")
    cfg = rec["config_file"]
    assert repoconnect.remove_preview("1")["managed_config"] is True
    repoconnect.remove("1", delete_config=True)
    assert not os.path.exists(cfg)


def test_run_puts_the_password_in_the_environment_only(store, fixed_binary, monkeypatch):
    """A real spawn, of a program that prints KOPIA_PASSWORD: argv must never carry it."""
    from app.core import repoconnect
    monkeypatch.delenv("KOPIA_PASSWORD", raising=False)
    probe_cmd = [sys.executable, "-c", "import os,sys; sys.stdout.write(os.environ.get('KOPIA_PASSWORD') or '')"]
    out = repoconnect._run(probe_cmd, "hunter2")
    assert out["returncode"] == 0 and out["stdout"] == "hunter2"
    assert "hunter2" not in out["command"]
    out = repoconnect._run(probe_cmd, None)
    assert out["stdout"] == ""
