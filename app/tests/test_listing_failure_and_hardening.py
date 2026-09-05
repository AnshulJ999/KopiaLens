"""A failed repository listing must never look like an empty repository, plus the
smaller hardening items shipped alongside it.

KL35 was the serious one: `kopia snapshot list` returning an error was read as "this repo has no
snapshots", the builder then read every source as removed and deleted its rows, and the pass
reported success. Two independent protections are asserted here - the checked listing boundary,
and the builder's own refusal to treat an empty list as a mass removal.
"""

from __future__ import annotations

import json
import subprocess

import duckdb
import pytest

from app import main as m
from app.core import auth, nightly, sources
from app.core import kopia as kopia_mod
from app.index import builder


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


@pytest.fixture(autouse=True)
def clean_listing_cache():
    sources._CACHE.clear()
    sources._LAST_ERROR.clear()
    sources._REFRESHING.clear()
    yield
    sources._CACHE.clear()
    sources._LAST_ERROR.clear()
    sources._REFRESHING.clear()


# --------------------------------------------------------------------------------------------
# The listing boundary
# --------------------------------------------------------------------------------------------

def test_a_failed_listing_raises_instead_of_returning_empty(monkeypatch):
    monkeypatch.setattr(sources, "kopia",
                        lambda *a, **k: _Result(returncode=1, stderr="repository unavailable"))
    with pytest.raises(sources.ListingFailed) as exc:
        sources.all_snapshots("1")
    assert "repository unavailable" in str(exc.value)


def test_a_genuinely_empty_repository_is_still_empty(monkeypatch):
    """The distinction the whole fix rests on: exit 0 with no output is a real empty repo."""
    monkeypatch.setattr(sources, "kopia", lambda *a, **k: _Result(stdout="", returncode=0))
    assert sources.all_snapshots("1") == []


def _snap(source_path: str, snap_id: str) -> dict:
    return {"id": snap_id, "source": {"userName": "u", "host": "h", "path": source_path},
            "rootEntry": {"obj": "k" + snap_id}, "startTime": "2026-09-01T00:00:00Z"}


def test_a_failed_refresh_keeps_serving_the_last_good_listing(monkeypatch):
    calls = {"n": 0}

    def fake(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Result(stdout=json.dumps([_snap("E:\\data", "s1")]), returncode=0)
        return _Result(returncode=1, stderr="drive not ready")

    monkeypatch.setattr(sources, "kopia", fake)
    monkeypatch.setattr(sources, "load_settings", lambda: {"listing_backend": "subprocess"})

    first = sources.sources_index("1")
    assert list(first) == ["u@h:E:\\data"]

    again = sources.sources_index("1", force=True)   # the forced rebuild fails
    assert list(again) == ["u@h:E:\\data"], "a failed refresh must not empty the cached listing"
    assert "drive not ready" in sources.last_listing_error("1")


def test_the_error_clears_once_a_listing_succeeds(monkeypatch):
    monkeypatch.setattr(sources, "load_settings", lambda: {"listing_backend": "subprocess"})
    monkeypatch.setattr(sources, "kopia", lambda *a, **k: _Result(returncode=1, stderr="boom"))
    with pytest.raises(sources.ListingFailed):
        sources.sources_index("1")
    assert sources.last_listing_error("1")

    monkeypatch.setattr(sources, "kopia",
                        lambda *a, **k: _Result(stdout=json.dumps([_snap("E:\\data", "s1")])))
    sources.sources_index("1")
    assert sources.last_listing_error("1") == ""


def test_a_background_refresh_failure_does_not_strand_the_refreshing_flag(monkeypatch):
    """If the flag were left set, this repo would never refresh again for the life of the
    process - a silent freeze rather than a visible failure."""
    monkeypatch.setattr(sources, "load_settings", lambda: {"listing_backend": "subprocess"})
    monkeypatch.setattr(sources, "kopia", lambda *a, **k: _Result(returncode=1, stderr="nope"))
    sources._REFRESHING.add("1")
    sources._refresh_in_background("1")
    assert "1" not in sources._REFRESHING


# --------------------------------------------------------------------------------------------
# The builder's own guard
# --------------------------------------------------------------------------------------------

def _seeded_index(path):
    """A file-backed index, because ``_run_build`` closes its connection in a finally - the rows
    have to be checked from a fresh connection afterwards."""
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE files(repo VARCHAR, source VARCHAR, snapshot_id VARCHAR)")
    con.execute("CREATE TABLE paths(repo VARCHAR, source VARCHAR, full_path VARCHAR)")
    con.execute("CREATE TABLE indexed_snapshots(repo VARCHAR, snapshot_id VARCHAR, "
                "source VARCHAR, start_time TIMESTAMP, root_obj VARCHAR, file_count BIGINT)")
    con.execute("CREATE TABLE first_seen_meta(repo VARCHAR, source VARCHAR)")
    con.execute("INSERT INTO indexed_snapshots VALUES ('1','s1','u@h:E:\\data',NULL,'k1',5)")
    con.execute("INSERT INTO paths VALUES ('1','u@h:E:\\data','E:\\data\\a.txt')")
    con.execute("INSERT INTO files VALUES ('1','u@h:E:\\data','s1')")
    return con


def test_an_empty_listing_against_a_populated_index_is_refused(monkeypatch, tmp_path):
    """Even if a future change let an empty list reach the builder, the rows survive."""
    path = tmp_path / "idx.duckdb"
    _seeded_index(path).close()
    monkeypatch.setattr(builder, "sources_index", lambda repo: {})
    monkeypatch.setattr(builder, "write_cursor", lambda repo: duckdb.connect(str(path)))
    monkeypatch.setattr(builder, "cache_db", lambda repo: None)
    builder._JOBS["1"] = {"building": True, "error": None, "indexed": 0, "total": 0}

    builder._run_build("1", None, False)

    job = builder._JOBS["1"]
    assert job["error"], "a refused build must report an error, not finish quietly"
    assert "refusing" in job["error"]
    check = duckdb.connect(str(path))
    assert check.execute("SELECT count(*) FROM paths").fetchone()[0] == 1
    assert check.execute("SELECT count(*) FROM indexed_snapshots").fetchone()[0] == 1
    check.close()


def test_an_empty_listing_against_an_empty_index_is_allowed(monkeypatch, tmp_path):
    """A brand new install has neither sources nor rows; that must not be an error."""
    path = tmp_path / "idx.duckdb"
    con = _seeded_index(path)
    con.execute("DELETE FROM indexed_snapshots")
    con.execute("DELETE FROM paths")
    con.close()
    monkeypatch.setattr(builder, "sources_index", lambda repo: {})
    monkeypatch.setattr(builder, "write_cursor", lambda repo: duckdb.connect(str(path)))
    monkeypatch.setattr(builder, "cache_db", lambda repo: None)
    monkeypatch.setattr(builder, "materialize_physical", lambda *a, **k: None)
    builder._JOBS["1"] = {"building": True, "error": None, "indexed": 0, "total": 0}

    builder._run_build("1", None, False)
    assert not builder._JOBS["1"]["error"]


# --------------------------------------------------------------------------------------------
# The failure has to reach the screen, and survive a restart
# --------------------------------------------------------------------------------------------

@pytest.fixture
def state(monkeypatch, tmp_path):
    f = tmp_path / "bg_worker_state.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(m, "_LAST_CYCLE_FILE", str(f))
    return f


def test_a_failure_reason_is_persisted_and_cleared_on_the_next_success(state):
    m.mark_nightly_result("1", False, "kopia snapshot list failed (exit 1): repository unavailable")
    assert "repository unavailable" in m.nightly_error("1")["reason"]
    assert m.nightly_success_ts("1") == 0.0, "a failure must not advance the freshness stamp"

    m.mark_nightly_result("1", True)
    assert m.nightly_error("1") == {}


def test_a_failure_with_no_reason_still_records_something(state):
    m.mark_nightly_result("1", False)
    assert m.nightly_error("1")["reason"]


def test_the_worker_passes_its_failure_reason_to_the_callback(monkeypatch):
    """The reason only reaches the state file if the callback is handed it."""
    got: list[tuple] = []
    monkeypatch.setattr(nightly, "_reserve", lambda repo: True)
    monkeypatch.setattr(nightly, "run_index_pass",
                        lambda repo, full, _owned: {"ok": False, "reason": "storage timeout"})
    monkeypatch.setattr(nightly, "REPOS", {"1": {"kind": "kopia"}})

    started = nightly.start_index_pass_async("1", on_done=lambda *a: got.append(a))
    assert started["started"] is True
    for _ in range(200):
        if got:
            break
        import time as _t
        _t.sleep(0.01)
    assert got and got[0] == ("1", False, "storage timeout")


# --------------------------------------------------------------------------------------------
# Worker log history
# --------------------------------------------------------------------------------------------

def test_the_worker_log_rolls_instead_of_being_wiped(tmp_path):
    """It used to be opened with mode "w" every run, so only the most recent nightly existed and
    "has this stage been healthy for a few nights?" was unanswerable after the fact."""
    logp = tmp_path / "index_worker_repo1.log"
    logp.write_text("x" * 2000, encoding="utf-8")
    nightly._roll_worker_log(str(logp), max_bytes=1000, keep=3)
    assert not logp.exists()
    assert (tmp_path / "index_worker_repo1.log.1").read_text(encoding="utf-8") == "x" * 2000


def test_a_small_log_is_left_alone(tmp_path):
    logp = tmp_path / "index_worker_repo1.log"
    logp.write_text("small", encoding="utf-8")
    nightly._roll_worker_log(str(logp), max_bytes=1000, keep=3)
    assert logp.read_text(encoding="utf-8") == "small"


def test_only_the_configured_number_of_generations_is_kept(tmp_path):
    logp = tmp_path / "index_worker_repo1.log"
    for i in range(1, 4):
        (tmp_path / f"index_worker_repo1.log.{i}").write_text(f"gen{i}", encoding="utf-8")
    logp.write_text("y" * 2000, encoding="utf-8")
    nightly._roll_worker_log(str(logp), max_bytes=1000, keep=3)
    assert (tmp_path / "index_worker_repo1.log.1").read_text(encoding="utf-8") == "y" * 2000
    assert (tmp_path / "index_worker_repo1.log.2").read_text(encoding="utf-8") == "gen1"
    assert (tmp_path / "index_worker_repo1.log.3").read_text(encoding="utf-8") == "gen2"
    assert not (tmp_path / "index_worker_repo1.log.4").exists()


def test_the_worker_log_is_opened_for_append(tmp_path):
    src = (nightly.__file__)
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert 'open(logp, "w"' not in text, "truncating the worker log discards every past run"
    assert 'open(logp, "a"' in text


# --------------------------------------------------------------------------------------------
# A hung kopia call
# --------------------------------------------------------------------------------------------

def test_a_timed_out_kopia_call_comes_back_as_a_failed_result(monkeypatch):
    """A failed RESULT, not an exception: every caller already checks the return code, and a new
    exception type would slip past all of them."""
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="kopia", timeout=1)

    monkeypatch.setattr(kopia_mod.subprocess, "run", boom)
    monkeypatch.setattr(kopia_mod, "_repo_args", lambda repo: [])
    res = kopia_mod.kopia("1", ["snapshot", "list"], timeout=1)
    assert res.returncode == kopia_mod.TIMEOUT_RC
    assert "timed out" in res.stderr


def test_a_timeout_is_a_listing_failure_not_an_empty_repository(monkeypatch):
    """The two fixes have to compose: a hung kopia must not become "no snapshots" either."""
    monkeypatch.setattr(sources, "kopia",
                        lambda *a, **k: _Result(returncode=kopia_mod.TIMEOUT_RC,
                                                stderr="kopia timed out after 3600s"))
    with pytest.raises(sources.ListingFailed):
        sources.all_snapshots("1")


# --------------------------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------------------------

def test_a_password_round_trips_and_a_wrong_one_does_not(monkeypatch):
    stored = auth.hash_password("correct horse battery")
    assert auth.verify_password("correct horse battery", stored)
    assert not auth.verify_password("wrong", stored)
    assert "correct horse battery" not in stored, "the password itself must never be stored"


def test_a_malformed_stored_hash_is_simply_not_a_match():
    assert not auth.verify_password("anything", "")
    assert not auth.verify_password("anything", "garbage")


def test_changing_the_password_invalidates_existing_sessions(monkeypatch):
    first, second = auth.hash_password("first"), auth.hash_password("second")
    monkeypatch.setattr(auth, "password_hash", lambda: first)
    token = auth.session_token()
    assert auth.valid_session(token)
    monkeypatch.setattr(auth, "password_hash", lambda: second)
    assert not auth.valid_session(token)


def test_no_password_means_no_valid_session(monkeypatch):
    monkeypatch.setattr(auth, "password_hash", lambda: "")
    assert not auth.valid_session("")
    assert not auth.valid_session("anything")


@pytest.mark.parametrize("host", ["localhost", "localhost:7123", "127.0.0.1:7123",
                                  "192.168.1.50:7123", "[::1]:7123", ""])
def test_localhost_and_ip_literals_are_always_accepted(monkeypatch, host):
    """DNS rebinding needs a DOMAIN name pointed at a private address; an IP literal in the Host
    header cannot be that, so blocking those would only break LAN access for no gain."""
    monkeypatch.setattr(auth, "_kl", lambda: {"allowed_hosts": []})
    assert auth.host_allowed(host)


def test_an_unlisted_domain_name_is_rejected(monkeypatch):
    monkeypatch.setattr(auth, "_kl", lambda: {"allowed_hosts": ["kopia.example.net"]})
    assert auth.host_allowed("kopia.example.net:7123")
    assert not auth.host_allowed("evil.example.com")


def test_the_bind_host_defaults_to_loopback(monkeypatch):
    monkeypatch.setattr(auth, "_kl", lambda: {})
    assert auth.bind_host("127.0.0.1") == "127.0.0.1"
    monkeypatch.setattr(auth, "_kl", lambda: {"bind_host": "0.0.0.0"})
    assert auth.bind_host("127.0.0.1") == "0.0.0.0"


def test_the_login_page_is_reachable_without_a_session():
    assert auth.exempt("/login")
    assert auth.exempt("/health")
    assert auth.exempt("/api/auth/login")
    assert not auth.exempt("/api/search")
    assert not auth.exempt("/")


# --------------------------------------------------------------------------------------------
# The exported remove-files .bat
# --------------------------------------------------------------------------------------------

def _bat() -> str:
    from app.core.kopia import remove_files_bat_text
    return remove_files_bat_text(
        [["kopia", "snapshot", "fix", "remove-files", "--object-id", "abc", "--commit"]],
        "run.log")


def test_the_bat_calls_find_and_findstr_by_full_path():
    """Bare `find` picks up GNU find from Git for Windows / Cygwin / GnuWin32 when one of those
    is ahead of System32 on PATH, and GNU find with these arguments starts recursively scanning
    the drive - the script appears to hang. Found by running a generated .bat, not by review."""
    text = _bat()
    assert "%SystemRoot%\\System32\\find.exe" in text
    assert "%SystemRoot%\\System32\\findstr.exe" in text
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("find "), f"bare find is PATH-dependent: {stripped}"
        assert not stripped.startswith("findstr "), f"bare findstr is PATH-dependent: {stripped}"


def test_the_bat_captures_stderr():
    """remove-files writes every per-snapshot line to stderr and nothing to stdout, so without
    2>&1 the log and the on-screen summary are both empty."""
    assert "2>&1" in _bat()


def test_the_bat_writes_its_log_next_to_itself():
    assert 'set "LOG=%~dp0run.log"' in _bat()


def test_the_bat_reports_a_failing_batch_and_ends_with_a_summary():
    text = _bat()
    assert "!KL_RC!" in text and "KL_FAILED" in text
    assert "summary" in text
    assert "maintenance run --full" in text


# --------------------------------------------------------------------------------------------
# CSV export cursor
# --------------------------------------------------------------------------------------------

def test_a_cancelled_csv_export_closes_its_cursor():
    """A cancelled download abandons the generator part-way; without try/finally the DuckDB read
    cursor stayed open until the generator happened to be collected."""
    from app.api import search as search_api

    class FakeCursor:
        def __init__(self):
            self.closed = False
            self.rows = [[f"r{i}"] * len(search_api._EXPORT_COLS) for i in range(20000)]

        def fetchmany(self, n):
            batch, self.rows = self.rows[:n], self.rows[n:]
            return batch

        def close(self):
            self.closed = True

    cur = FakeCursor()
    gen = search_api._csv_rows(cur)
    next(gen)                 # header only, then the client goes away
    gen.close()
    assert cur.closed
