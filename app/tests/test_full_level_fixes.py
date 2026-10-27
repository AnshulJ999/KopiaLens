"""The Full encryption level end to end: every reader of a covered file must decrypt it.

The 2026-09-24 review found the Full level writing files encrypted that half the app still read
as plain JSON - the scheduler forgot every run and re-indexed nonstop, the size history wiped
itself, Stop could not find the worker. These tests run the app's own writer/reader pairs at the
Full level, plus the smaller fixes from the same review.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from app.core import applog, filecrypto, nightly, repoconnect
from app.index import crypto


@pytest.fixture
def full(tmp_path, monkeypatch):
    """Scratch data folder + keystore, Full level, unlocked, log dirs redirected."""
    data = tmp_path / "data"
    (data / "cache").mkdir(parents=True)
    monkeypatch.setattr("config.DATA_DIR", str(data))
    monkeypatch.setattr(nightly, "DATA_DIR", str(data))
    monkeypatch.setattr(crypto, "keystore_dir", lambda: str(data))
    monkeypatch.setattr("app.settings.raw_kopialens_settings",
                        lambda: {"encryption_level": "full"})
    for name, sub in (("LOG_DIR", ""), ("APP_LOG_DIR", "app"), ("INDEX_LOG_DIR", "indexing"),
                      ("JOB_LOG_DIR", "jobs")):
        path = os.path.join(str(data), "logs", sub)
        os.makedirs(path, exist_ok=True)
        monkeypatch.setattr(applog, name, path)
    crypto.lock()
    crypto.setup("pw", with_recovery=False)
    assert filecrypto.available()
    yield data
    crypto.lock()


def test_the_scheduler_state_survives(full, monkeypatch):
    from app import main as appmain
    monkeypatch.setattr(appmain, "_LAST_CYCLE_FILE", str(full / "bg_worker_state.json"))
    appmain._write_state({"ts": 1.0, "nightly": {"1": 5.0}, "attempts": {"1": {"slot": 9.0}}})
    assert filecrypto.is_encrypted_file(str(full / "bg_worker_state.json"))
    assert appmain._attempt_record("1") == {"slot": 9.0}      # {} made every slot due again


def test_the_size_history_keeps_every_repo(full, monkeypatch):
    from app.api import stats
    monkeypatch.setattr(stats, "_HISTORY_FILE", str(full / "repo_size_history.json"))
    monkeypatch.setattr(stats, "_HISTORY_SAMPLE_MIN", 0)
    stats._record_growth("1", 100)
    stats._record_growth("2", 200)
    assert set(filecrypto.read_json(str(full / "repo_size_history.json"))) == {"1", "2"}


def test_worker_progress_splits_into_a_plain_status_and_an_encrypted_detail(full):
    from app.index import worker
    src = "Anshul@host:C:\\Secret\\Folder"
    worker.write_progress("3", {"repo": "3", "stage": "index", "pid": 4242, "updated": 1.0,
                                "current_source": src, "targets": [src], "error": None})
    status_raw = open(worker.progress_path("3"), "rb").read()
    assert b"Secret" not in status_raw and json.loads(status_raw)["pid"] == 4242   # tray + Stop
    assert filecrypto.is_encrypted_file(worker.detail_path("3"))
    merged = nightly.worker_progress("3")
    assert merged["pid"] == 4242 and merged["current_source"] == src
    assert nightly.stop_index_pass("3")["reason"] != "nothing is indexing this repo"
    worker.clear_progress("3")
    assert not os.path.exists(worker.progress_path("3"))
    assert not os.path.exists(worker.detail_path("3"))


def test_the_worker_result_reads_back(full):
    from app.index import worker
    worker.write_result("3", {"ok": True, "pid": 7})
    assert filecrypto.is_encrypted_file(worker.result_path("3"))
    assert nightly._read_result(worker.result_path("3")) == {"ok": True, "pid": 7}


def test_plain_operational_files_are_never_covered(full):
    for name in ("data_version.json", "index_progress_repo1.json", "index_progress_repobz.json",
                 "server.json", "desktop-instance.json"):
        (full / name).write_text("{}", encoding="utf-8")
    (full / "index_progress_detail_repo1.json").write_text("{}", encoding="utf-8")
    covered = {os.path.basename(p) for p in filecrypto.covered_paths()}
    assert "data_version.json" not in covered
    assert "index_progress_repo1.json" not in covered and "index_progress_repobz.json" not in covered
    assert "server.json" not in covered and "desktop-instance.json" not in covered   # the desktop app has no key
    assert "index_progress_detail_repo1.json" in covered


def test_checkin_and_index_copy_status_read_back(full, monkeypatch):
    from app.core import checkin
    from app.index import versions
    monkeypatch.setattr(checkin, "STATE_FILE", str(full / "checkin.json"))
    monkeypatch.setattr(versions, "DATA_DIR", str(full))
    iid = checkin.install_id()
    assert filecrypto.is_encrypted_file(str(full / "checkin.json"))
    assert checkin.install_id() == iid                         # read as {} gave a new id each time
    versions._write_status("1", {"ok": True})
    assert filecrypto.is_encrypted_file(str(full / "index_backup_status.json"))
    assert versions._read_status() == {"1": {"ok": True}}


def test_files_read_before_unlock_stay_plain_at_full(full):
    """Sign-in and the table layouts are read before the index is unlocked: an encrypted
    users.json read as "no accounts" and quietly switched sign-in off."""
    for name in ("users.json", "ui_state.json", "settings.json", "repos.json", "keystore.json"):
        (full / name).write_text("{}", encoding="utf-8")
    covered = {os.path.basename(p) for p in filecrypto.covered_paths()}
    assert not covered & {"users.json", "ui_state.json", "settings.json", "repos.json", "keystore.json"}


def test_a_past_jobs_log_decrypts(full, monkeypatch):
    from app.core import jobs
    monkeypatch.setattr(jobs, "_history_path", lambda: str(full / "jobs_history.jsonl"))
    logp = str(full / "logs" / "jobs" / "job.log")
    filecrypto.append_line(logp, "removed 3 files")
    filecrypto.append_line(str(full / "jobs_history.jsonl"),
                           json.dumps({"job_id": "j1", "log_path": logp, "created": 1.0}))
    lines = jobs.history_status("j1")["lines"]
    assert any("removed 3 files" in ln for ln in lines)
    assert not any("kle:" in ln for ln in lines)


def test_disconnect_cleans_encrypted_state_files(full):
    from app.core import repos
    path = str(full / "reindex_pending.json")
    filecrypto.write_json(path, {"1": 1.0, "2": 2.0})
    assert repos._drop_key(path, "1") is True
    assert filecrypto.read_json(path) == {"2": 2.0}


def test_an_append_waits_for_a_file_being_rewritten(full):
    path = str(full / "audit.jsonl")
    done = threading.Event()
    with filecrypto._WRITE_LOCK:
        t = threading.Thread(target=lambda: (filecrypto.append_line(path, "late"), done.set()))
        t.start()
        time.sleep(0.2)
        assert not done.is_set()          # held off until the rewrite releases the file
    t.join(5)
    assert filecrypto.read_lines(path) == ["late"]


def test_the_switch_leaves_a_running_workers_log_alone(full, monkeypatch):
    log = full / "logs" / "indexing" / "index_worker_repo3.log"
    log.write_text("plain line\n", encoding="utf-8")
    monkeypatch.setattr(nightly, "is_running", lambda repo: repo == "3")
    out = filecrypto.switch_files(True)
    assert out["skipped"] >= 1 and log.read_text(encoding="utf-8") == "plain line\n"


def test_the_level_follows_the_settings_file(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr("app.settings.SETTINGS_FILE", str(settings))
    settings.write_text(json.dumps({"kopialens": {"encryption_level": "index"}}), encoding="utf-8")
    assert crypto.level() == "index"
    settings.write_text(json.dumps({"kopialens": {"encryption_level": "full", "x": 1}}),
                        encoding="utf-8")
    assert crypto.level() == "full"


def test_lock_forgets_the_file_key(full):
    assert filecrypto.subkey() is not None
    crypto.lock()
    assert filecrypto._CACHE is None and filecrypto.subkey() is None


def test_the_startup_sweep_keeps_a_live_processs_session(tmp_path):
    live = tmp_path / f"session_{os.getppid()}_aa.db"
    dead = tmp_path / "session_999999_bb.db"
    live_dir = tmp_path / f"c{os.getppid()}_aa"
    dead_dir = tmp_path / "c999999_bb"
    live.write_bytes(b"x")
    dead.write_bytes(b"x")
    live_dir.mkdir()
    dead_dir.mkdir()
    (dead_dir / "duckdb_temp_storage_S64K-0.tmp").write_bytes(b"x")
    assert crypto.sweep_sessions(str(tmp_path)) == 2
    assert live.exists() and not dead.exists()
    assert live_dir.exists() and not dead_dir.exists()


def test_a_disconnected_repos_queued_rebuild_is_dropped(monkeypatch):
    from app.index import upgrade
    started = []
    monkeypatch.setattr(upgrade, "_start_pass", lambda repo, trigger: started.append(repo)
                        or {"started": True})
    monkeypatch.setitem(upgrade._REBUILD_QUEUE, "gone9", 0.0)
    upgrade.drain_queue()
    assert "gone9" not in upgrade._REBUILD_QUEUE and started == []


# ---------------------------------------------------------------------------- repo connections

def test_storage_secrets_never_reach_argv():
    fields = {"bucket": "b", "access_key": "AKID", "secret_access_key": "s3cr3t"}
    args = repoconnect.build_connect_args("s3", fields, "c.config", None)
    env, stdin = repoconnect.secret_inputs("s3", fields)
    assert "s3cr3t" not in " ".join(args) and env == {"AWS_SECRET_ACCESS_KEY": "s3cr3t"}
    assert stdin is None
    args = repoconnect.build_connect_args("token", {"token": "tok123"}, "c.config", None)
    env, stdin = repoconnect.secret_inputs("token", {"token": "tok123"})
    assert "tok123" not in " ".join(args) and "--token-stdin" in args and stdin == "tok123"


def test_sftp_with_the_system_ssh_needs_no_key_or_known_hosts():
    base = {"host": "h", "username": "u", "path": "/r"}
    assert repoconnect.validate_fields("sftp", base)                      # password/key needed
    assert repoconnect.validate_fields("sftp", {**base, "external": True}) == []
    sftp = next(p for p in repoconnect.provider_list() if p["key"] == "sftp")
    assert ["sftp_password", "keyfile", "key_data"] in sftp["requires_any"]
    assert sftp["requires_any_unless"] == "external"


def test_a_server_connection_unlocks_with_its_url_and_fingerprint(tmp_path, monkeypatch):
    from app.core import repos
    cfg = tmp_path / "server.config"
    cfg.write_text(json.dumps({"apiServer": {"url": "https://k:51515",
                                             "serverCertFingerprint": "ab12"}}), encoding="utf-8")
    assert repoconnect.describe_config(str(cfg))["config"]["url"] == "https://k:51515"
    monkeypatch.setattr(repos, "all_records", lambda: [{"id": "5", "config_file": str(cfg)}])
    seen = []
    monkeypatch.setattr(repoconnect, "_run", lambda args, pw, timeout=600.0, **kw: seen.append(args)
                        or {"returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(repoconnect, "probe", lambda repo=None: {"ok": True})
    repoconnect.unlock("5", "pw")
    assert seen[0][seen[0].index("--url") + 1] == "https://k:51515"
    assert seen[0][seen[0].index("--server-cert-fingerprint") + 1] == "ab12"
