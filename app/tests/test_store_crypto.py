"""Step 1 of: the keystore, encrypted sessions, RAM tiers and the idle close.

These are the load-bearing checks for encryption at rest. The ones that matter most are the
negative ones: a wrong password must be REFUSED (DuckDB's database encryption is authenticated,
which is the only reason a wrap file doubles as a password check), and a file written through a
configured connection must not contain the plaintext it was built from.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import duckdb
import pytest

from app.index import crypto, store, upgrade


class _FakeKeyring:
    """An in-memory stand-in for the OS keychain; tests never touch the real one."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, account):
        return self.store.get((service, account))

    def set_password(self, service, account, value):
        self.store[(service, account)] = value

    def delete_password(self, service, account):
        if (service, account) not in self.store:
            raise KeyError("no such entry")
        del self.store[(service, account)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


@pytest.fixture
def keystore(tmp_path, monkeypatch):
    """Point the keystore at a scratch dir and leave encryption ON for the test."""
    monkeypatch.setattr(crypto, "keystore_dir", lambda: str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "enabled", lambda: crypto.configured())
    for name, value in (("_KC_THREAD", None), ("_KC_FAILED", False), ("_REMEMBERED", None)):
        monkeypatch.setattr(crypto, name, value)
    crypto.lock()
    yield tmp_path
    crypto.lock()


# ---------------------------------------------------------------------------- key material

def test_setup_then_unlock_with_the_right_password(keystore):
    recovery = crypto.setup("correct horse", with_recovery=True)
    key = crypto.data_key()
    assert key and recovery
    crypto.lock()
    assert not crypto.unlocked()
    assert crypto.unlock("correct horse") is True
    assert crypto.data_key() == key      # the same data key, not a re-derived one


def test_a_wrong_password_is_refused_and_leaves_the_store_locked(keystore):
    crypto.setup("correct horse", with_recovery=False)
    crypto.lock()
    assert crypto.unlock("wrong horse") is False
    assert crypto.unlocked() is False
    assert crypto.data_key() is None


def test_the_recovery_key_opens_the_same_data_key(keystore):
    recovery = crypto.setup("pw", with_recovery=True)
    key = crypto.data_key()
    crypto.lock()
    assert crypto.unlock_with_recovery(recovery) is True
    assert crypto.data_key() == key


def test_a_mistyped_recovery_key_fails_instead_of_raising(keystore):
    crypto.setup("pw", with_recovery=True)
    crypto.lock()
    assert crypto.unlock_with_recovery("not even base64 !!") is False
    assert crypto.unlock_with_recovery(base64.b64encode(b"x" * 32).decode()) is False


def test_changing_the_password_rewraps_without_changing_the_data_key(keystore):
    crypto.setup("old pw", with_recovery=False)
    key = crypto.data_key()
    assert crypto.change_password("old pw", "new pw") is True
    crypto.lock()
    assert crypto.unlock("old pw") is False
    assert crypto.unlock("new pw") is True
    assert crypto.data_key() == key      # re-wrap only: nothing on disk needs re-encrypting


def test_changing_the_password_needs_the_old_one(keystore):
    crypto.setup("old pw", with_recovery=False)
    assert crypto.change_password("guess", "new pw") is False


def test_an_interrupted_password_change_still_opens_with_the_old_password(keystore, monkeypatch):
    """The wrap and the salt are two files; a crash between them used to leave NEITHER password
    working. The previous wrap + salt are kept until both writes land, so the old password still
    opens the keystore and the change is simply retried."""
    crypto.setup("old pw", with_recovery=False)
    key = crypto.data_key()
    real_write = crypto._write_meta
    calls = {"n": 0}

    def crash_on_the_second_write(meta):
        calls["n"] += 1
        if calls["n"] == 2:                    # the salt write, after the new wrap already landed
            raise OSError("simulated crash between the two writes")
        return real_write(meta)

    with monkeypatch.context() as m:
        m.setattr(crypto, "_write_meta", crash_on_the_second_write)
        with pytest.raises(OSError):
            crypto.change_password("old pw", "new pw")

    crypto.lock()
    assert crypto.unlock("old pw") is True      # the old password still opens the keystore
    assert crypto.data_key() == key
    assert crypto.change_password("old pw", "new pw") is True     # retried: the change completes
    crypto.lock()
    assert crypto.unlock("old pw") is False
    assert crypto.unlock("new pw") is True
    assert crypto.data_key() == key


def test_a_finished_password_change_leaves_no_previous_state(keystore):
    crypto.setup("old pw", with_recovery=False)
    assert crypto.change_password("old pw", "new pw") is True
    cfg = keystore / "cfg"
    assert not (cfg / "keystore-password.kdb.prev").exists()
    meta = json.loads((cfg / "keystore.json").read_text(encoding="utf-8"))
    assert "kdf_prev" not in meta


def test_unlock_says_why_it_failed(keystore):
    """A wrong password and a damaged keystore used to be the same ``False``; the UI needs to
    tell the user which one they are looking at."""
    crypto.setup("pw", with_recovery=False)
    crypto.lock()
    assert crypto.unlock_detailed("nope") == (False, "wrong-password")
    assert crypto.unlock_detailed("pw") == (True, "ok")
    wrap = keystore / "cfg" / "keystore-password.kdb"
    wrap.write_bytes(b"this is not a keystore anymore")
    crypto.lock()
    assert crypto.unlock_detailed("pw") == (False, "corrupt-keystore")


def test_the_recovery_key_sets_a_new_password(keystore):
    recovery = crypto.setup("pw", with_recovery=True)
    crypto.lock()
    ok, reason = crypto.reset_password_with_recovery(recovery, "brand new pw")
    assert (ok, reason) == (True, "ok")
    crypto.lock()
    assert crypto.unlock("pw") is False
    assert crypto.unlock("brand new pw") is True
    crypto.lock()
    assert crypto.unlock_with_recovery(recovery) is True     # the recovery key keeps working


def test_a_keychain_entry_is_scoped_to_the_install(keystore, fake_keyring):
    crypto.setup("pw", with_recovery=False)
    key = crypto.data_key()
    assert crypto.remember_on_this_machine(True) == "on"
    assert fake_keyring.store[(crypto._SERVICE, crypto._account())] == key
    crypto.lock()
    assert crypto.is_remembered() is True
    assert crypto.unlock_from_keychain() is True
    assert crypto.data_key() == key
    assert crypto.remember_on_this_machine(False) == "off"
    assert crypto.is_remembered() is False


def test_a_keychain_that_never_answers_blocks_nothing(keystore, fake_keyring, monkeypatch):
    # On a Linux desktop the keychain can open a password window and wait for a person: setup,
    # status and Quit hung on it, and every status check opened another window.
    import threading
    crypto.setup("pw", with_recovery=False)
    answer = threading.Event()
    calls = []

    def stuck_set(service, account, value):
        calls.append(account)
        answer.wait(10)
        raise RuntimeError("prompt dismissed")

    monkeypatch.setattr(fake_keyring, "set_password", stuck_set)
    assert crypto.remember_on_this_machine(True, wait=0.2) == "waiting"
    assert crypto.remember_state() == "waiting"
    assert crypto.remember_on_this_machine(True, wait=0.2) == "waiting"   # no second window
    assert len(calls) == 1
    answer.set()
    crypto._KC_THREAD.join(5)
    assert crypto.remember_state() == "unavailable" and crypto.is_remembered() is False


def test_a_legacy_keychain_entry_is_migrated_to_the_scoped_name(keystore, fake_keyring):
    # Installs that remembered the key before the entry was scoped still unlock, and the entry
    # moves to the scoped name so a second install on the machine cannot read it.
    crypto.setup("pw", with_recovery=False)
    key = crypto.data_key()
    fake_keyring.store[(crypto._SERVICE, crypto._ACCOUNT)] = key
    crypto.lock()
    assert crypto.unlock_from_keychain() is True
    assert fake_keyring.store[(crypto._SERVICE, crypto._account())] == key
    assert crypto.data_key() == key


def test_the_data_key_is_never_written_in_the_clear(keystore):
    crypto.setup("pw", with_recovery=True)
    key = crypto.data_key()
    raw = base64.b64decode(key)
    cfg = keystore / "cfg"
    seen = 0
    for name in os.listdir(cfg):
        blob = (cfg / name).read_bytes()
        assert key.encode() not in blob, f"{name} carries the data key as base64"
        assert raw not in blob, f"{name} carries the raw data key"
        seen += 1
    assert seen >= 2                     # meta + at least one wrap file really were inspected


def test_a_key_that_is_not_base64_is_refused_before_it_reaches_sql(keystore):
    # Keys are inlined into SQL because PRAGMA add_parquet_key refuses a bound parameter, so the
    # charset guard is the whole defence against a key breaking out of its quotes.
    with pytest.raises(ValueError):
        crypto._check_key("abc' OR '1'='1")


# ---------------------------------------------------------------------------- sessions

def test_apply_configures_limits_and_round_trips_encrypted_parquet(keystore, tmp_path):
    crypto.setup("pw", with_recovery=False)
    con = duckdb.connect()
    session = crypto.apply(con, str(tmp_path / "tmp"), "512MB", 4)
    assert session is not None           # encryption on: a session database was attached
    out = str(tmp_path / "t.parquet").replace("\\", "/")
    con.execute("CREATE TABLE t AS SELECT 1 AS a, 'topsecretpath' AS b")
    con.execute(f"COPY t TO '{out}' ({crypto.copy_options()})")
    assert b"topsecretpath" not in open(out, "rb").read()
    got = con.execute("SELECT b FROM read_parquet(%s)" % crypto.parquet_read(out)).fetchone()[0]
    assert got == "topsecretpath"
    con.close()
    session.cleanup()
    assert not os.path.exists(session.path)


def test_an_encrypted_file_cannot_be_read_without_the_key(keystore, tmp_path):
    crypto.setup("pw", with_recovery=False)
    con = duckdb.connect()
    session = crypto.apply(con, str(tmp_path / "tmp"), "512MB", 4)
    out = str(tmp_path / "t.parquet").replace("\\", "/")
    con.execute("CREATE TABLE t AS SELECT 'topsecretpath' AS b")
    con.execute(f"COPY t TO '{out}' ({crypto.copy_options()})")
    con.close()
    session.cleanup()
    plain = duckdb.connect()
    with pytest.raises(duckdb.Error):
        plain.execute(f"SELECT * FROM read_parquet('{out}')").fetchall()
    plain.close()


def test_a_files_encryption_state_is_readable_without_the_key(keystore, tmp_path):
    crypto.setup("pw", with_recovery=False)
    con = duckdb.connect()
    session = crypto.apply(con, str(tmp_path / "tmp"), "512MB", 4)
    plain = str(tmp_path / "plain.parquet").replace("\\", "/")
    enc = str(tmp_path / "enc.parquet").replace("\\", "/")
    con.execute("CREATE TABLE t AS SELECT 'x' AS a")
    con.execute("COPY t TO '%s' (FORMAT parquet)" % plain)
    con.execute(f"COPY t TO '{enc}' ({crypto.copy_options()})")
    con.close()
    session.cleanup()
    assert crypto.file_is_encrypted(plain) is False
    assert crypto.file_is_encrypted(enc) is True
    assert crypto.file_is_encrypted(str(tmp_path / "missing.parquet")) is False
    assert crypto.wrong_state([plain, enc], True) == [plain]
    assert crypto.wrong_state([plain, enc], False) == [enc]


def test_the_build_override_decides_the_write_state(keystore):
    # The setting and the data can disagree while a switch is pending; the build override is how
    # a build stays consistent with the manifest it is replacing.
    crypto.setup("pw", with_recovery=False)
    assert crypto.writes_encrypted() is True
    with crypto.writing_state(False):
        assert crypto.writes_encrypted() is False
        assert "encryption_config" not in crypto.copy_options()
        assert crypto.parquet_read_list(["x.parquet"]) == "['x.parquet']"
    assert crypto.writes_encrypted() is True


def test_a_cursor_sees_the_views_of_the_encrypted_session(keystore, tmp_path):
    """A DuckDB cursor is a fresh connection and does not inherit ``USE``; without the search
    path every encrypted read fails with "Table with name cid does not exist"."""
    crypto.setup("pw", with_recovery=False)
    con = duckdb.connect()
    session = crypto.apply(con, str(tmp_path / "tmp"), "512MB", 4)
    assert session is not None
    con.execute("CREATE VIEW cid AS SELECT 7 AS a")
    with pytest.raises(duckdb.CatalogException):
        con.cursor().execute("SELECT * FROM cid")       # the bug this path exists to prevent
    cur = con.cursor()
    cur.execute("SET search_path='%s'" % crypto.cursor_search_path(session))
    assert cur.execute("SELECT * FROM cid").fetchone() == (7,)
    assert crypto.cursor_search_path(None) is None
    con.close()
    session.cleanup()


def test_apply_is_a_plain_connection_when_encryption_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "enabled", lambda: False)
    con = duckdb.connect()
    session = crypto.apply(con, str(tmp_path / "tmp"), "512MB", 4)
    assert session.path is None and crypto.cursor_search_path(session) is None
    assert crypto.copy_options() == "FORMAT parquet, COMPRESSION zstd"
    assert "encryption_config" not in crypto.parquet_read("x.parquet")
    con.close()
    session.cleanup()


def test_each_connection_spills_into_its_own_temp_folder(tmp_path, monkeypatch):
    """DuckDB names spill files the same in every connection, so two sharing a folder overwrite
    each other's ("Corrupt temporary file")."""
    monkeypatch.setattr(crypto, "enabled", lambda: False)
    base = str(tmp_path / "tmp")
    cons = [duckdb.connect(), duckdb.connect()]
    sessions = [crypto.apply(c, base, "512MB", 2) for c in cons]
    dirs = [c.execute("SELECT current_setting('temp_directory')").fetchone()[0] for c in cons]
    assert dirs[0] != dirs[1]
    for c, s in zip(cons, sessions):
        c.close()
        s.cleanup()
    assert os.listdir(base) == []


def test_sweep_removes_session_databases_a_crash_left_behind(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    (d / "session_1234_abcdef.db").write_bytes(b"x")
    (d / "keep.parquet").write_bytes(b"x")
    assert crypto.sweep_sessions(str(d)) == 1
    assert (d / "keep.parquet").exists()


def test_sweep_all_walks_the_read_and_staging_temp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_owner_alive", lambda fn: False)   # pids 1/2 are alive on Linux
    read = tmp_path / "duck_tmp_repo1"
    read.mkdir()
    (read / "session_1_aa.db").write_bytes(b"x")
    staging = tmp_path / "repo3" / "_staging" / "tmp"
    staging.mkdir(parents=True)
    (staging / "session_2_bb.db").write_bytes(b"x")
    assert crypto.sweep_all() == 2
    assert not (read / "session_1_aa.db").exists()


def test_a_locked_encrypted_store_refuses_to_open_or_write(keystore, monkeypatch, tmp_path):
    """Locked + encrypted data: nothing may read it, and nothing may write as if it were plain."""
    crypto.setup("pw", with_recovery=False)
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    root = store.repo_dir("3")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"generation": 1, "encrypted": True, "artifacts": {},
                   "schema_version": upgrade.MANIFEST_VERSION}, fh)
    crypto.lock()
    assert crypto.ready() is False
    with pytest.raises(PermissionError):
        with store.scratch_connection("3"):
            pass
    with pytest.raises(PermissionError):
        store.open_generation("3")


def test_asking_for_encryption_while_locked_is_refused(keystore):
    # Silently downgrading to plaintext here is how an unencrypted file lands inside an
    # encrypted generation, so the request fails instead.
    crypto.setup("pw", with_recovery=False)
    crypto.lock()
    with pytest.raises(PermissionError):
        crypto.copy_options(encrypted=True)
    with pytest.raises(PermissionError):
        crypto.parquet_read_list(["x.parquet"], encrypted=True)
    assert "encryption_config" not in crypto.parquet_read("x.parquet", encrypted=False)


def test_ready_is_true_when_encryption_is_off(monkeypatch):
    monkeypatch.setattr(crypto, "enabled", lambda: False)
    assert crypto.ready() is True


# ---------------------------------------------------------------------------- RAM tiers

@pytest.fixture
def _fixed_machine(monkeypatch):
    """The tier tests set RAM themselves: no container limit and enough cores on any test machine."""
    from app.core import host
    monkeypatch.setattr(host, "_cgroup_memory_limit", lambda: 0)
    monkeypatch.setattr(store.os, "cpu_count", lambda: 64)


@pytest.mark.parametrize("total_gb, expect", [(4, ("512MB", 2)), (6, ("768MB", 3)), (7.6, ("972MB", 3)),
                                              (8, ("1GB", 4)), (12, ("1536MB", 6)), (16, ("2GB", 8)),
                                              (24, ("3GB", 12)), (64, ("3GB", 12)), (0, ("512MB", 2))])
@pytest.mark.usefixtures("_fixed_machine")
def test_ram_tiers_follow_total_memory(total_gb, expect, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: type("m", (), {"total": int(total_gb * 1024 ** 3)})())
    monkeypatch.delenv("KOPIALENS_DUCKDB_MEMORY", raising=False)
    monkeypatch.setattr(store, "load_settings", None, raising=False)
    monkeypatch.setattr("app.settings.load_settings",
                        lambda: {"kopialens": {"duckdb_memory": "", "duckdb_threads": 0}})
    assert store.resolve_limits() == expect


def test_explicit_settings_beat_the_detected_tier(monkeypatch):
    monkeypatch.setattr("app.settings.load_settings",
                        lambda: {"kopialens": {"duckdb_memory": "7GB", "duckdb_threads": 2}})
    assert store.resolve_limits() == ("7GB", 2)


@pytest.mark.usefixtures("_fixed_machine")
def test_auto_threads_use_at_most_half_the_processors(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("m", (), {"total": 64 * 1024 ** 3})())
    monkeypatch.setattr(store.os, "cpu_count", lambda: 6)
    assert store.auto_limits() == ("3GB", 3)
    monkeypatch.setattr(store.os, "cpu_count", lambda: 1)
    assert store.auto_limits() == ("3GB", 1)


@pytest.mark.usefixtures("_fixed_machine")
def test_auto_limits_ignore_the_override(monkeypatch):
    # Settings shows "Automatic (<tier>)", which must not echo the user's own override back.
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("m", (), {"total": 16 * 1024 ** 3})())
    monkeypatch.setattr("app.settings.load_settings",
                        lambda: {"kopialens": {"duckdb_memory": "7GB", "duckdb_threads": 2}})
    assert store.auto_limits() == ("2GB", 8)
    assert store.resolve_limits() == ("7GB", 2)


@pytest.mark.usefixtures("_fixed_machine")
def test_the_index_worker_caps_threads_but_an_explicit_setting_wins(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("m", (), {"total": 64 * 1024 ** 3})())
    monkeypatch.delenv("KOPIALENS_DUCKDB_MEMORY", raising=False)
    monkeypatch.setattr(store, "_in_worker", True)
    settings = {"kopialens": {"duckdb_memory": "", "duckdb_threads": 0}}
    monkeypatch.setattr("app.settings.load_settings", lambda: settings)
    assert store.resolve_limits() == ("3GB", store.WORKER_THREADS)
    settings["kopialens"]["duckdb_threads"] = 12
    assert store.resolve_limits() == ("3GB", 12)





# ---------------------------------------------------------------------------- idle close

def test_close_idle_waits_for_the_window(monkeypatch):
    monkeypatch.setattr(store, "idle_seconds", lambda: 1800)
    store.touch()
    assert store.close_idle() is False


def test_close_idle_never_closes_a_generation_being_read(monkeypatch):
    class _Gen:
        _refs = 1

        def close(self):
            raise AssertionError("closed a generation with a live reader")

    monkeypatch.setattr(store, "idle_seconds", lambda: 1)
    monkeypatch.setattr(store, "_LAST_TOUCH", 0.0)
    monkeypatch.setitem(store._GENS, "test", _Gen())
    try:
        assert store.close_idle() is False
    finally:
        store._GENS.pop("test", None)


def test_close_idle_closes_an_idle_generation(monkeypatch):
    closed = []

    class _Gen:
        _refs = 0

        def close(self):
            closed.append(True)

    monkeypatch.setattr(store, "idle_seconds", lambda: 1)
    monkeypatch.setattr(store, "_LAST_TOUCH", 0.0)
    monkeypatch.setitem(store._GENS, "test", _Gen())
    try:
        assert store.close_idle() is True
        assert closed == [True]
        assert "test" not in store._GENS
    finally:
        store._GENS.pop("test", None)


def test_idle_close_can_be_disabled(monkeypatch):
    monkeypatch.setattr(store, "idle_seconds", lambda: 0)
    monkeypatch.setattr(store, "_LAST_TOUCH", 0.0)
    assert store.close_idle() is False


def test_polled_endpoints_do_not_count_as_user_activity():
    # The idle close is defeated entirely if an open browser tab's status polling counts, so the
    # middleware skips exactly the polled set. Pin that the set still exists and is non-empty.
    from app.main import _POLL_PATHS
    assert "/api/index/status" in _POLL_PATHS and "/api/jobs/list" in _POLL_PATHS
