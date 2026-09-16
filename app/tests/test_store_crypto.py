"""Step 1 of: the keystore, encrypted sessions, RAM tiers and the idle close.

These are the load-bearing checks for encryption at rest. The ones that matter most are the
negative ones: a wrong password must be REFUSED (DuckDB's database encryption is authenticated,
which is the only reason a wrap file doubles as a password check), and a file written through a
configured connection must not contain the plaintext it was built from.
"""

from __future__ import annotations

import base64
import os

import duckdb
import pytest

from app.index import crypto, store


@pytest.fixture
def keystore(tmp_path, monkeypatch):
    """Point the keystore at a scratch dir and leave encryption ON for the test."""
    monkeypatch.setattr(crypto, "keystore_dir", lambda: str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "enabled", lambda: crypto.configured())
    monkeypatch.setattr(crypto, "remember_on_this_machine", lambda on: True)
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


def test_apply_is_a_plain_connection_when_encryption_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "enabled", lambda: False)
    con = duckdb.connect()
    assert crypto.apply(con, str(tmp_path / "tmp"), "512MB", 4) is None
    assert crypto.copy_options() == "FORMAT parquet, COMPRESSION zstd"
    assert "encryption_config" not in crypto.parquet_read("x.parquet")
    con.close()


def test_sweep_removes_session_databases_a_crash_left_behind(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    (d / "session_1234_abcdef.db").write_bytes(b"x")
    (d / "keep.parquet").write_bytes(b"x")
    assert crypto.sweep_sessions(str(d)) == 1
    assert (d / "keep.parquet").exists()


def test_a_locked_store_refuses_to_open_or_write(keystore, monkeypatch):
    crypto.setup("pw", with_recovery=False)
    crypto.lock()
    assert crypto.ready() is False
    with pytest.raises(PermissionError):
        with store.scratch_connection("3"):
            pass


def test_ready_is_true_when_encryption_is_off(monkeypatch):
    monkeypatch.setattr(crypto, "enabled", lambda: False)
    assert crypto.ready() is True


# ---------------------------------------------------------------------------- RAM tiers

@pytest.mark.parametrize("total_gb, expect", [(8, ("512MB", 4)), (7.6, ("512MB", 4)),
                                              (16, ("1GB", 8)), (32, ("3GB", 16)),
                                              (64, ("3GB", 16))])
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


def test_threads_are_never_left_at_the_duckdb_default():
    # Every tier pins a thread count on purpose: the machine default tracks core count, and a
    # high count inflates hash-aggregate memory enough to OOM the small tier.
    assert all(isinstance(t, int) and t > 0 for _gb, _mem, t in store._TIERS)


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
