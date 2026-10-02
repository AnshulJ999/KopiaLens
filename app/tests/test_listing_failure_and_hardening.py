"""A failed repository listing must never look like an empty repository, plus the
smaller hardening items shipped alongside it.

KL35 was the serious one: `kopia snapshot list` returning an error was read as "this repo has no
snapshots", the builder then read every source as removed and deleted its rows, and the pass
reported success. Two independent protections are asserted here - the checked listing boundary,
and the builder's own refusal to treat an empty list as a mass removal.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import duckdb
import pytest

from app import main as m
from app.core import auth, nightly, sources
from app.core import kopia as kopia_mod


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


def test_the_worker_run_pin_holds_one_listing_for_the_whole_run(monkeypatch):
    """The nightly stages are minutes apart and the TTL is 45s, so `kopia snapshot list` ran
    once per stage - 8x per nightly for the same data. The worker pins the first listing for
    the process lifetime (the worker exits when the run ends); force=True still rebuilds."""
    calls = {"n": 0}

    def fake(*_a, **_k):
        calls["n"] += 1
        return _Result(stdout=json.dumps([_snap("E:\\data", "s1")]), returncode=0)

    monkeypatch.setattr(sources, "kopia", fake)
    monkeypatch.setattr(sources, "load_settings", lambda: {"listing_backend": "subprocess"})
    monkeypatch.setattr(sources, "_PINNED", True)

    sources.sources_index("1")
    sources._CACHE["1"]["built"] = time.time() - 3600  # far past the 45s TTL
    sources.sources_index("1")
    assert calls["n"] == 1, "a pinned run must not re-list between stages"
    sources.sources_index("1", force=True)
    assert calls["n"] == 2, "an explicit force still rebuilds under the pin"


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


class _FakeProc:
    """Just enough of Popen for _run_worker: it has already exited."""

    def __init__(self, out: str, rc: int):
        self._out, self.returncode, self.pid = out, rc, 4242

    def communicate(self, timeout=None):
        return self._out, ""

    def poll(self):
        return self.returncode


def _run_worker_with(monkeypatch, tmp_path, out: str, rc: int) -> dict:
    monkeypatch.setattr(nightly, "worker_log_path", lambda repo: str(tmp_path / "w.log"))
    monkeypatch.setattr(nightly, "worker_timeout", lambda: 60)
    monkeypatch.setattr(nightly.subprocess, "Popen", lambda *a, **k: _FakeProc(out, rc))
    return nightly._run_worker("1", False)


def test_the_workers_own_reason_survives_its_non_zero_exit(monkeypatch, tmp_path):
    """the worker prints WHY it failed and then exits 1. Reading the exit
    code first discarded the explanation and reported "worker exit 1" - the exact message the
    freshness strip exists to replace."""
    out = json.dumps({"ok": False, "repo": "1", "reason": "repository unavailable"})
    got = _run_worker_with(monkeypatch, tmp_path, out, rc=1)
    assert got["ok"] is False
    assert got["reason"] == "repository unavailable"


def test_a_completed_run_that_failed_a_stage_builds_its_reason_from_errors(monkeypatch, tmp_path):
    """The worker's OTHER failure shape: it finishes, reports ok=false with per-stage `errors`, and
    still exits 1 - with NO `reason` field at all (a `reason` only exists on a crash). The reason
    must be built from those errors, not replaced by the "worker exit 1" placeholder. The shape
    here is the one `worker.run()` actually emits, not an invented one."""
    out = json.dumps({"ok": False, "repo": "1", "full": False,
                      "stages": {"index": 12.0, "churn": 3.1},
                      "errors": {"churn": "OOM in the 365d window"},
                      "skipped": [], "elapsed": 14.2})
    got = _run_worker_with(monkeypatch, tmp_path, out, rc=1)
    assert got["ok"] is False
    assert got["reason"] == "churn: OOM in the 365d window"
    assert got["errors"]["churn"] == "OOM in the 365d window"


def test_a_parsed_failure_with_neither_reason_nor_errors_falls_back(monkeypatch, tmp_path):
    out = json.dumps({"ok": False, "repo": "1"})
    got = _run_worker_with(monkeypatch, tmp_path, out, rc=1)
    assert got["reason"] == "worker exit 1"


def test_the_worker_emits_no_reason_on_a_stage_failure(monkeypatch, tmp_path):
    """Producer half of the two-shapes rule: a stage failure emits `errors` and NO `reason`. Pinned
    separately so neither half of the contract can drift - this feature was tested twice against
    invented shapes before this pair existed."""
    from app.index import worker
    monkeypatch.setattr(worker, "progress_path", lambda repo: str(tmp_path / "p.json"))

    def _boom(repo, full=False):
        raise RuntimeError("walk failed")

    monkeypatch.setattr(worker, "index_stage", _boom)
    summary = worker.run("1", full=False)
    assert summary["ok"] is False
    assert "reason" not in summary
    assert summary["errors"]["index"] == "walk failed"


def test_an_unparsable_failure_still_falls_back_to_the_exit_code(monkeypatch, tmp_path):
    got = _run_worker_with(monkeypatch, tmp_path, "this is not json", rc=1)
    assert got["ok"] is False
    assert "worker exit 1" in got["reason"]


def test_a_successful_run_returns_its_summary_unchanged(monkeypatch, tmp_path):
    out = json.dumps({"ok": True, "repo": "1", "elapsed": 12.5})
    got = _run_worker_with(monkeypatch, tmp_path, out, rc=0)
    assert got == {"ok": True, "repo": "1", "elapsed": 12.5}


def test_the_worker_passes_its_failure_reason_to_the_callback(monkeypatch):
    """The reason only reaches the state file if the callback is handed it."""
    got: list[tuple] = []
    monkeypatch.setattr(nightly, "_reserve", lambda repo: True)
    monkeypatch.setattr(nightly, "run_index_pass",
                        lambda repo, full, _owned, trigger="manual": {"ok": False, "reason": "storage timeout"})
    monkeypatch.setattr(nightly, "REPOS", {"1": {"kind": "kopia"}})

    started = nightly.start_index_pass_async("1", on_done=lambda *a: got.append(a))
    assert started["started"] is True
    for _ in range(200):
        if got:
            break
        time.sleep(0.01)
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


def test_the_streaming_reader_kills_a_child_that_goes_silent(monkeypatch):
    """iterating stdout BLOCKS, so a deadline checked inside the read loop
    never runs against the failure it exists for. The watchdog has to kill the child instead.
    Uses a real subprocess that produces no output at all."""
    import sys as _sys
    monkeypatch.setattr(kopia_mod, "_base_command",
                        lambda repo, args: [_sys.executable, "-c", "import time; time.sleep(30)"])
    monkeypatch.setattr(kopia_mod, "check_readonly", lambda args: None)
    t0 = time.time()
    with pytest.raises(RuntimeError) as exc:
        list(kopia_mod.kopia_lines("1", ["content", "list"], timeout=1.5))
    elapsed = time.time() - t0
    assert "timed out" in str(exc.value)
    assert elapsed < 15, f"the watchdog did not interrupt the silent child (took {elapsed:.0f}s)"


def test_the_streaming_reader_still_returns_normal_output(monkeypatch):
    import sys as _sys
    monkeypatch.setattr(
        kopia_mod, "_base_command",
        lambda repo, args: [_sys.executable, "-c", "print('a'); print('b')"])
    monkeypatch.setattr(kopia_mod, "check_readonly", lambda args: None)
    assert list(kopia_mod.kopia_lines("1", ["content", "list"], timeout=30)) == ["a", "b"]


def test_the_streaming_reader_raises_on_a_non_zero_exit(monkeypatch):
    """A partial read must never be mistaken for a complete one - that is the whole reason
    load_sizemap can trust what it gets."""
    import sys as _sys
    monkeypatch.setattr(
        kopia_mod, "_base_command",
        lambda repo, args: [_sys.executable, "-c",
                            "import sys; print('one'); sys.stderr.write('boom'); sys.exit(2)"])
    monkeypatch.setattr(kopia_mod, "check_readonly", lambda args: None)
    with pytest.raises(RuntimeError) as exc:
        list(kopia_mod.kopia_lines("1", ["content", "list"], timeout=30))
    assert "exited 2" in str(exc.value)
    assert "boom" in str(exc.value), "stderr must reach the error message"


def test_a_partial_sizemap_never_replaces_a_good_one(monkeypatch, tmp_path):
    """load_sizemap's half of the same rule: a stream that dies part-way would otherwise make
    every physical/freeable figure read low, silently and permanently."""
    from app.core import sizemap as sm
    monkeypatch.setattr(sm, "CACHEDIR", str(tmp_path))
    good = {"c1": 100, "c2": 200}
    with open(tmp_path / "sizemap_repo1.pkl", "wb") as fh:
        import pickle
        pickle.dump(good, fh)

    def _dies(*_a, **_k):
        yield "c1 x 10+100"
        raise RuntimeError("kopia exited 1: storage unavailable")

    monkeypatch.setattr(sm, "kopia_lines", _dies)
    assert sm.load_sizemap("1", refresh=True) == good
    with open(tmp_path / "sizemap_repo1.pkl", "rb") as fh:
        import pickle
        assert pickle.load(fh) == good, "the partial map overwrote the good one"


def test_a_refresh_overrides_the_worker_run_memo(monkeypatch, tmp_path):
    """The run-scoped memo must not mask refresh=True: metrics.py's mid-run recovery reloads the
    sizemap once when content ids are missing, and the memo answering first would silently keep
    the old map (every physical figure for the new content reads low) - the exact bug the memo
    review caught."""
    from app.core import sizemap as sm
    monkeypatch.setattr(sm, "CACHEDIR", str(tmp_path))
    monkeypatch.setattr(sm, "MEMO_ENABLED", True)
    monkeypatch.setattr(sm, "_MEMO", {})   # isolate the module-level memo from any earlier test
    old = {"c1": 100}
    with open(tmp_path / "sizemap_repo1.pkl", "wb") as fh:
        import pickle
        pickle.dump(old, fh)

    assert sm.load_sizemap("1") == old            # memo populated with the old map
    assert sm._MEMO["1"] == old

    def _fresh(*_a, **_k):
        yield "c1 x 10+100"
        yield "c2 x 20+200"

    monkeypatch.setattr(sm, "kopia_lines", _fresh)
    new = {"c1": 100, "c2": 200}
    assert sm.load_sizemap("1", refresh=True) == new   # refresh bypasses the memo entirely
    assert sm._MEMO["1"] == new                        # and re-sets it for the rest of the run
    assert sm.load_sizemap("1") == new                 # later calls see the refreshed map


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


def test_no_account_means_no_valid_session():
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


def test_the_bind_default_follows_the_launch_profile(monkeypatch):
    """No profile declared = a server start = all interfaces (the container convention: the user's
    port mapping decides exposure). The tray declares its child desktop, which binds loopback."""
    monkeypatch.setattr(auth, "_env", lambda name: "")
    monkeypatch.setattr(auth, "raw_kopialens_settings", lambda: {})
    assert auth.bind_host() == "0.0.0.0"
    monkeypatch.setattr(auth, "_env",
                        lambda name: "desktop" if name == "KOPIALENS_PROFILE" else "")
    assert auth.bind_host() == "127.0.0.1"


def test_env_and_explicit_settings_beat_the_profile(monkeypatch):
    monkeypatch.setattr(auth, "_env", lambda name: {"KOPIALENS_PROFILE": "desktop",
                                                    "KOPIALENS_BIND_HOST": "192.168.1.5"}.get(name, ""))
    assert auth.bind_host() == "192.168.1.5"   # env wins over the profile
    monkeypatch.setattr(auth, "_env",
                        lambda name: "desktop" if name == "KOPIALENS_PROFILE" else "")
    monkeypatch.setattr(auth, "raw_kopialens_settings", lambda: {"bind_host": "0.0.0.0"})
    assert auth.bind_host() == "0.0.0.0"       # an explicit file value wins over the profile


def test_the_allowed_hosts_env_overrides_settings(monkeypatch):
    monkeypatch.setattr(auth, "_env", lambda name: "a.example.net, b.example.net"
                        if name == "KOPIALENS_ALLOWED_HOSTS" else "")
    assert auth.allowed_hosts() == ["a.example.net", "b.example.net"]


def test_the_login_page_is_reachable_without_a_session():
    assert auth.exempt("/login")
    assert auth.exempt("/health")
    assert auth.exempt("/api/auth/login")
    assert auth.exempt("/api/auth/logout")
    assert not auth.exempt("/api/search")
    assert not auth.exempt("/")
    # EXACT matches only: a prefix rule here would leave password management open to callers
    # with no session.
    assert not auth.exempt("/api/auth/set-password")
    assert not auth.exempt("/api/auth/remove-password")


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
    assert "%KL_RC%" in text and "KL_FAILED" in text
    assert "summary" in text
    assert "maintenance run --full" in text


def test_the_bat_never_uses_delayed_expansion():
    """With enabledelayedexpansion on, cmd.exe treats everything BETWEEN two `!` characters as a
    variable reference and deletes the whole span - quotes and flags included, not just the `!`.
    It corrupts filenames, --source paths and the kopia config path alike, and --filename matching
    by basename means a corrupted name can hit a DIFFERENT real file. Reproduced 2026-09-06."""
    text = _bat()
    assert "enabledelayedexpansion" not in text
    assert "!KL_" not in text
    assert "!ERRORLEVEL!" not in text


@pytest.mark.skipif(os.name != "nt", reason="runs the .bat through cmd.exe")
def test_the_bat_keeps_bang_filenames_intact_when_run(tmp_path):
    """End-to-end on the real cmd.exe: the kopia command is swapped for an echo of `!`-bearing
    names, the generated script is executed verbatim, and the text must survive intact while the
    counters still work. This is the reproduction of the delayed-expansion corruption."""
    import subprocess as sp

    from app.core.kopia import remove_files_bat_text

    text = remove_files_bat_text(
        [["echo", "removed Backup!.txt and Wow!! Two + fake error line"]], "run.log")
    bat = tmp_path / "t.bat"
    bat.write_text(text, encoding="ascii", errors="replace", newline="\r\n")
    res = sp.run(["cmd", "/c", str(bat)], input="\r\n", capture_output=True, text=True,
                 errors="replace", timeout=60)
    out = res.stdout
    assert "Backup!.txt and Wow!! Two" in out           # the !-span survived, quotes included
    assert "1 removal line(s)" in out                  # counters still resolve
    assert "1 problem line(s)" in out                  # the error branch ran
    assert "failed: 0" in out                          # batch exit code read correctly


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
