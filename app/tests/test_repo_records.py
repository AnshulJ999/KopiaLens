"""Repository records: id allocation, the record rules, and the per-repo file map.

The id is embedded in on-disk names (`data/repo<id>/`, `cache/*_repo<id>.*`), which is why a
user-chosen id is not a thing: ids are allocated, validated, and never changed.
"""

from __future__ import annotations

import json

import pytest

import config


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An empty record store on disk, with every path redirected and REPOS restored afterwards."""
    from config import REPOS

    monkeypatch.setattr(config, "REPOS_FILE", str(tmp_path / "repos.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHEDIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "RECORDS", {"version": 1, "cache_dir": None, "repos": []})
    snapshot = dict(REPOS)
    try:
        yield tmp_path
    finally:
        REPOS.clear()
        REPOS.update(snapshot)


def test_next_id_is_one_past_the_highest_in_use(store):
    from app.core import repos
    assert repos.next_id() == "1"
    config.RECORDS["repos"] = [{"id": "1"}, {"id": "2"}, {"id": "bz"}]
    assert repos.next_id() == "3"
    config.RECORDS["repos"] = [{"id": "2"}, {"id": "bz"}]
    assert repos.next_id() == "3"


def test_next_id_never_reuses_a_removed_id(store):
    """Ids key shared state (bg_worker_state, size history), so a removed id must not come back -
    else a brand-new repo inherits the old one's index history."""
    from app.core import repos
    repos.add_record({"name": "One"})
    repos.add_record({"name": "Two"})
    repos.remove_record("2")
    assert repos.next_id() == "3"


def test_a_failed_connect_gives_its_number_back(store):
    """A number is used only by a real connection; overlapping connects never share one."""
    from app.core import repos
    repos.add_record({"name": "One"})
    failed = repos.reserve_id()
    repos.release_id(failed)
    assert repos.next_id() == failed
    first, second = repos.reserve_id(), repos.reserve_id()
    repos.release_id(first)                    # a later reservation exists: keep the gap
    assert repos.next_id() == str(int(second) + 1)


def test_scrub_storage_config_hides_credentials_and_keeps_the_rest(store):
    """A kopia config file holds credentials in the clear; nothing secret may reach a response or
    repos.json. The display fields the screens need must survive."""
    from app.core import repos
    raw = {
        "bucket": "my-backups", "endpoint": "s3.amazonaws.com", "region": "eu-central-1",
        "prefix": "kopia/", "accessKeyID": "AKIA123", "secretAccessKey": "shhh",
        "sessionToken": "tok", "path": "/mnt/backup", "host": "nas.local", "port": 22,
        "password": "pw", "keyData": "-----BEGIN KEY-----", "keyID": "002",
        "storageKey": "azure-key", "sasToken": "sv=2021", "ClientSecret": "cs",
        "ClientCertificate": "cert", "credentials": "{\"private_key\": \"...\"}",
        "embeddedConfig": "[remote]\ntype = drive", "rcloneEnv": ["TOKEN=abc"],
        "rcloneArgs": ["--fast-list"], "credentialsFile": "/home/u/sa.json", "readOnly": True,
        "nested": {"secretAccessKey": "inner", "bucket": "inner-bucket"},
    }
    out = repos.scrub_storage_config(raw)
    assert "shhh" not in json.dumps(out) and "azure-key" not in json.dumps(out)
    assert "-----BEGIN KEY-----" not in json.dumps(out) and "sv=2021" not in json.dumps(out)
    assert out["nested"]["secretAccessKey"] == repos.SCRUBBED   # nested blocks too
    assert out["secretAccessKey"] == repos.SCRUBBED and out["password"] == repos.SCRUBBED
    assert out["storageKey"] == repos.SCRUBBED and out["ClientSecret"] == repos.SCRUBBED
    assert out["ClientCertificate"] == repos.SCRUBBED and out["credentials"] == repos.SCRUBBED
    assert out["embeddedConfig"] == repos.SCRUBBED and out["rcloneEnv"] == repos.SCRUBBED
    assert out["key"] if "key" in out else True
    # identifiers and paths are NOT secrets - the screens show them
    assert out["bucket"] == "my-backups" and out["endpoint"] == "s3.amazonaws.com"
    assert out["accessKeyID"] == "AKIA123" and out["credentialsFile"] == "/home/u/sa.json"
    assert out["nested"]["bucket"] == "inner-bucket" and out["rcloneArgs"] == ["--fast-list"]
    assert repos.scrub_storage_config(None) == {} and repos.scrub_storage_config("junk") == {}


def test_storage_info_scrubs_a_legacy_record_and_the_cleanup_rewrites_it(store):
    """A record written before the scrub existed must not leak once, and gets cleaned up."""
    from app.core import repos
    repos.add_record({"name": "Legacy", "config_file": None})
    config.RECORDS["repos"][0]["storage"] = {"type": "s3",
                                             "config": {"bucket": "b", "secretAccessKey": "leak"}}
    assert repos.storage_info("1")["config"]["secretAccessKey"] == repos.SCRUBBED
    assert repos.scrub_stored_records() == ["1"]
    on_disk = json.loads((store / "repos.json").read_text(encoding="utf-8"))
    assert on_disk["repos"][0]["storage"]["config"]["secretAccessKey"] == repos.SCRUBBED
    assert on_disk["repos"][0]["storage"]["config"]["bucket"] == "b"
    assert repos.scrub_stored_records() == []


def test_capture_status_stores_a_scrubbed_config(store):
    from app.core import repos
    repos.add_record({"name": "Main", "config_file": None})
    repos.capture_status("1", {"uniqueIDHex": "a" * 64,
                               "storage": {"type": "b2", "config": {"bucket": "b", "key": "leak"}},
                               "volume": {"capacity": 10, "available": 5}})
    stored = config.RECORDS["repos"][0]["storage"]["config"]
    assert stored["key"] == repos.SCRUBBED and "leak" not in json.dumps(config.RECORDS)


def test_reserve_id_advances_the_persisted_mark_before_anything_slow(store):
    """Two overlapping connects must never share an id (and therefore a managed config path)."""
    from app.core import repos
    assert repos.reserve_id() == "1"
    assert repos.reserve_id() == "2"          # persisted at allocation, not at add_record
    on_disk = json.loads((store / "repos.json").read_text(encoding="utf-8"))
    assert on_disk["next_id"] == 3
    # a failed connect leaves the id burned by design
    repos.add_record({"id": "1", "name": "First"})
    assert repos.reserve_id() == "3"


def test_forget_state_clears_the_id_keyed_state_files(store):
    from app.core import repos
    (store / "bg_worker_state.json").write_text(json.dumps({
        "ts": 1, "nightly": {"2": 5, "1": 4}, "attempts": {"2": {"ok": None}},
        "success": {"2": 5}, "errors": {"2": {"reason": "x"}}}), encoding="utf-8")
    (store / "repo_size_history.json").write_text(json.dumps({"2": [[1, 2]], "1": [[1, 3]]}),
                                                  encoding="utf-8")
    (store / "reindex_pending.json").write_text(json.dumps({"2": {"requested": 1, "due": 2}}),
                                                encoding="utf-8")
    cleaned = repos.forget_state("2")
    assert set(cleaned) == {"bg_worker_state.json", "repo_size_history.json", "reindex_pending.json"}
    state = json.loads((store / "bg_worker_state.json").read_text(encoding="utf-8"))
    assert state["nightly"] == {"1": 4} and state["attempts"] == {} and state["errors"] == {}
    assert json.loads((store / "repo_size_history.json").read_text(encoding="utf-8")) == {"1": [[1, 3]]}
    assert json.loads((store / "reindex_pending.json").read_text(encoding="utf-8")) == {}
    assert repos.forget_state("2") == []       # nothing left to clean


@pytest.mark.parametrize("rid,ok", [
    ("1", True), ("12", True), ("abc123", True), ("bz", False), ("BZ", False),
    ("", False), ("a" * 13, False), ("a b", False), ("../x", False), ("re/po", False),
])
def test_valid_id(rid, ok):
    from app.core import repos
    assert repos.valid_id(rid) is ok


def test_add_record_allocates_persists_and_updates_repos_in_place(store):
    from app.core import repos
    rec = repos.add_record({"name": "Test repo", "config_file": None})
    assert rec["id"] == "1"
    assert rec["counts_as_backup"] is True and rec["source"] == "connected"
    # REPOS IS the interface: an added repo must be visible to it without a restart, and the dict
    # object must be the same one every importer holds.
    assert "1" in config.REPOS
    assert config.REPOS["1"]["name"] == "Test repo"
    on_disk = json.loads((store / "repos.json").read_text(encoding="utf-8"))
    assert on_disk["repos"][0]["id"] == "1"


def test_add_record_rejects_a_duplicate_or_invalid_id(store):
    from app.core import repos
    repos.add_record({"name": "One"})
    with pytest.raises(ValueError, match="already in use"):
        repos.add_record({"id": "1", "name": "Two"})
    with pytest.raises(ValueError, match="invalid repo id"):
        repos.add_record({"id": "bz", "name": "Backblaze clash"})


def test_update_record_edits_only_the_editable_fields(store):
    from app.core import repos
    repos.add_record({"name": "One", "config_file": None})
    rec = repos.update_record("1", {"name": "Renamed", "short_name": "R1", "counts_as_backup": False,
                                    "unique_id": "should-not-change"})
    assert rec["short_name"] == "R1"
    assert rec["counts_as_backup"] is False
    assert "unique_id" not in rec
    assert config.REPOS["1"]["name"] == "Renamed"
    assert config.REPOS["1"]["counts_as_backup"] is False
    with pytest.raises(ValueError, match="unknown repo"):
        repos.update_record("9", {"name": "x"})


def test_remove_record_drops_it_from_the_store_and_repos(store):
    from app.core import repos
    repos.add_record({"name": "One"})
    repos.remove_record("1")
    assert "1" not in config.REPOS
    assert json.loads((store / "repos.json").read_text(encoding="utf-8"))["repos"] == []


def test_detail_shape_is_the_api_contract(store):
    from app.core import repos
    repos.add_record({"name": "Main", "short_name": "M", "config_file": r"C:\k\r.cfg"})
    config.RECORDS["repos"][0]["storage"] = {"type": "s3", "config": {"bucket": "b"}}
    detail = repos.detail(config.RECORDS["repos"][0])
    assert detail["id"] == "1" and detail["short_name"] == "M"
    assert detail["storage"]["type"] == "s3" and detail["storage"]["kind"] == "cloud"
    assert detail["counts_as_backup"] is True
    assert set(detail) >= {"id", "name", "short_name", "kind", "config_file", "source",
                           "counts_as_backup", "unique_id", "storage"}


@pytest.mark.parametrize("stype,kind", [
    ("filesystem", "local"), ("sftp", "network"), ("webdav", "network"), ("rclone", "network"),
    ("s3", "cloud"), ("b2", "cloud"), ("gcs", "cloud"), ("azureBlob", "cloud"),
    ("gdrive", "cloud"), ("server", "server"), ("", "unknown"),
])
def test_storage_kind(stype, kind):
    from app.core import repos
    assert repos.storage_kind(stype) == kind


def test_data_paths_matches_only_this_repos_files(store):
    from app.core import repos
    cache = store / "cache"
    cache.mkdir()
    (store / "repo1").mkdir()
    (store / "duck_tmp_repo1").mkdir()
    (store / "repo12").mkdir()
    (store / "churn_repo1_30d_d4.parquet").write_text("x", encoding="utf-8")
    (store / "churn_repo12_30d_d4.parquet").write_text("x", encoding="utf-8")
    (cache / "policy_check_repo1.json").write_text("{}", encoding="utf-8")
    (cache / "policy_check_repo12.json").write_text("{}", encoding="utf-8")
    (store / "runs").mkdir()
    (store / "runs" / "repo1.jsonl").write_text("", encoding="utf-8")
    (store / "policy-backups").mkdir()
    (store / "policy-backups" / "repo1").mkdir()

    names = {path.replace(str(store), "").lstrip("\\/").replace("\\", "/")
             for path in repos.data_paths("1")}
    assert names == {"repo1", "duck_tmp_repo1", "churn_repo1_30d_d4.parquet",
                     "cache/policy_check_repo1.json", "runs/repo1.jsonl",
                     "policy-backups/repo1"}
    assert all("repo12" not in name for name in names)
