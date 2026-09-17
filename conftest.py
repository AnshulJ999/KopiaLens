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
"""

from __future__ import annotations

import faulthandler
import os
import tempfile

import pytest

# Before `config` is imported by anything. A test must never reach the real walk caches or
# refcount pickles - they are retired multi-GB files that no test has any business reading.
_CACHE_SANDBOX = tempfile.mkdtemp(prefix="kopialens-test-cache-")
os.environ["KOPIALENS_CACHE_DIR"] = _CACHE_SANDBOX

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
    """Remove the per-run cache sandbox; it is this file's own litter."""
    import shutil

    shutil.rmtree(_CACHE_SANDBOX, ignore_errors=True)
