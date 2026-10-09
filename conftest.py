"""Test-run guardrails. Loaded before any test module, so the env work below lands before
`config` resolves its module-level paths.

Two things went wrong on 2026-09-17 and both are fenced off here:

  * A test inherited the developer's real cache folder. `test_empty_repo_publishes_an_openable
    _generation` builds an index for a repo with NO sources, which leaves the tree empty, which
    triggers `_sync_tree`'s walk-cache upgrade seed - and that read the live 2.0 GB
    `data/cache/repo1.db`, wrote a 1.9 GB scratch database and ran for minutes. The repository
    under test was empty; the gigabytes came from the machine.
  * Nothing bounded a runaway test, so it consumed RAM and disk until someone killed the run.

The cache redirect is the fix; the memory cap and the watchdog are the net under it.

The data folder is sandboxed too (2026-09-23): the shell's KOPIALENS_DATA_DIR points at the live
install, and a test run wrote into it. Every run gets a throwaway data folder seeded with a copy of
settings.json and the real repo records with their kopia connections pointed at nothing (no test
may reach a real repository), and refuses to start if config still resolves to the real folder.
"""

from __future__ import annotations

import faulthandler
import json
import os
import shutil
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _real_config_dir() -> str:
    """Where config.py would find repos.json/settings.json for this shell (read-only use)."""
    env = os.environ.get("KOPIALENS_CONFIG_DIR") or os.environ.get("KOPIALENS_DATA_DIR")
    if env:
        return env
    if os.path.exists(os.path.join(_HERE, "repos.json")):
        return _HERE
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "KopiaLens")


# Before `config` is imported by anything. A test must never reach the real walk caches or
# refcount pickles - they are retired multi-GB files that no test has any business reading.
_CACHE_SANDBOX = tempfile.mkdtemp(prefix="kopialens-test-cache-")
os.environ["KOPIALENS_CACHE_DIR"] = _CACHE_SANDBOX

# No test may read or write the real OS keyring (Windows Credential Manager, macOS Keychain, the
# Secret Service): unpatched keyring calls fail, and the code under test takes its no-keyring path.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

# A test must never write into the live data folder; every write lands in the sandbox.
_DATA_SANDBOX = tempfile.mkdtemp(prefix="kopialens-test-data-")
_REAL_CONFIG_DIR = _real_config_dir()
_settings_src = os.path.join(_REAL_CONFIG_DIR, "settings.json")
if os.path.isfile(_settings_src):
    shutil.copy2(_settings_src, os.path.join(_DATA_SANDBOX, "settings.json"))


_STANDIN_STORE = {"version": 1, "data_dir": None, "cache_dir": None, "repos": [
    {"id": "1", "name": "Test repo 1", "kind": "kopia", "config_file": None},
    {"id": "2", "name": "Test repo 2", "kind": "kopia", "config_file": None},
    {"id": "3", "name": "Test repo 3", "kind": "kopia", "config_file": None},
    {"id": "bz", "name": "Backblaze Personal Backup", "kind": "backblaze", "config_file": None},
]}


def _sandbox_repo_records() -> None:
    """The real repo ids and names, but every kopia connection points at a config file that does
    not exist: an unmocked kopia call in a test fails at once instead of reaching a real
    repository. A test that truly needs kopia builds its own throwaway repo."""
    src = os.path.join(_REAL_CONFIG_DIR, "repos.json")
    try:
        with open(src, encoding="utf-8") as fh:
            store = json.load(fh)
    except (OSError, ValueError):
        store = _STANDIN_STORE   # a fresh checkout (CI, a Linux container) has no records of its own
    dead =os.path.join(_DATA_SANDBOX, "no-such-kopia-config")
    for rec in store.get("repos") or []:
        if rec.get("kind", "kopia") == "kopia":
            rec["config_file"] = os.path.join(dead, f"repo{rec.get('id')}.config")
    with open(os.path.join(_DATA_SANDBOX, "repos.json"), "w", encoding="utf-8") as fh:
        json.dump(store, fh)


_sandbox_repo_records()
# kopia's own default connection too: a bare `kopia` call must not find the real repository.config
os.environ["KOPIA_CONFIG_PATH"] = os.path.join(_DATA_SANDBOX, "no-such-kopia-config", "default.config")
os.environ["KOPIALENS_DATA_DIR"] = _DATA_SANDBOX
os.environ["KOPIALENS_CONFIG_DIR"] = _DATA_SANDBOX
os.environ["KOPIALENS_REPOS_FILE"] = os.path.join(_DATA_SANDBOX, "repos.json")
os.environ["KOPIALENS_DEV_ADDONS"] = ""   # empty beats a developer's .env.dev: no dev add-ons in tests


def pytest_sessionstart(session):
    """Refuse the whole run if config still points outside the sandbox: continuing would write
    into a real install."""
    import config

    sandbox = os.path.realpath(_DATA_SANDBOX)
    for label, path in (("DATA_DIR", config.DATA_DIR), ("CONFIG_DIR", config.CONFIG_DIR),
                        ("REPOS_FILE", config.REPOS_FILE)):
        if not os.path.realpath(path).startswith(sandbox):
            pytest.exit(f"config.{label} resolves outside the test sandbox ({path}); refusing to "
                        f"run tests against a real data folder", returncode=2)

# Ceiling for every connection the app opens during tests (`store.resolve_limits` reads this).
# Test fixtures are tiny; anything that wants more than this is misbehaving and should fail
# loudly rather than eat the machine.
os.environ["KOPIALENS_DUCKDB_MEMORY"] = "512MB"

# A single test that runs longer than this is a bug, not a slow machine. The traceback names the
# exact line, which is what turns "the suite is slow" into a one-line diagnosis.
TEST_TIMEOUT = 60


@pytest.fixture(autouse=True)
def _bound_duckdb(request, monkeypatch):
    """Cap threads as well as memory. Threads are not env-readable, and DuckDB otherwise takes
    one per core (24 here) per connection, which inflates hash-aggregate memory for no gain on
    fixture-sized data.

    `test_store_crypto` is exempt: it asserts on ``resolve_limits`` itself, and patching the
    function under test would only ever assert this fixture back. Its own connections are
    KDF-sized and need no ceiling.
    """
    if request.node.path.name == "test_store_crypto.py":
        return
    from app.index import store

    monkeypatch.setattr(store, "resolve_limits", lambda: ("512MB", 4))


@pytest.fixture(autouse=True)
def _cap_raw_duckdb(monkeypatch, tmp_path):
    """Cap connections tests open THEMSELVES, not just the ones the store opens.

    Most test files call `duckdb.connect()` directly, which takes the machine defaults - 50 GiB
    and one thread per core. Capping only `store.resolve_limits` leaves those uncovered, so the
    ceiling is applied at the `connect` boundary where every path meets.
    """
    import duckdb

    real = duckdb.connect
    spill = str(tmp_path / "duck_tmp").replace("'", "''")

    def capped(*args, **kwargs):
        con = real(*args, **kwargs)
        for stmt in ("SET memory_limit='512MB'", "SET threads=4",
                     "SET temp_directory='%s'" % spill):
            try:
                con.execute(stmt)
            except Exception:  # noqa: BLE001 - a read-only or odd connection still runs its test
                pass
        return con

    monkeypatch.setattr(duckdb, "connect", capped)


@pytest.fixture(autouse=True)
def _watchdog():
    """Hard stop for a runaway test. **This kills the whole pytest process, by design.**

    `exit=True` means `_exit(1)`, so one overrun ends the run with no results rather than failing
    a single test. That is deliberate and should not be "fixed" into a timer thread that fails
    only the current test: the case this exists for is a query blocked inside DuckDB's C code,
    which no Python-level timer can interrupt. A traceback plus a dead process beats a hung suite.
    """
    faulthandler.dump_traceback_later(TEST_TIMEOUT, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


def pytest_sessionfinish(session, exitstatus):
    """Remove the per-run sandboxes; they are this file's own litter."""
    shutil.rmtree(_CACHE_SANDBOX, ignore_errors=True)
    shutil.rmtree(_DATA_SANDBOX, ignore_errors=True)
