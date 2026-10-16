"""Browser-suite fixtures: one fixture repository per run, throwaway KopiaLens servers against it,
and pages that fail the test on any browser console error.

Run by name only: ``pytest Kopia/KopiaLens/app/tests/ui`` (the default suite skips this folder).
"""

from __future__ import annotations

import faulthandler
import os
import re
from pathlib import Path

import pytest

from . import _harness as h

UI_TEST_TIMEOUT = 240     # a flow can include a first index; Playwright bounds each wait itself
UI_DIR = Path(__file__).parent


@pytest.hookimpl(tryfirst=True)     # before xdist reads the group marks
def pytest_collection_modifyitems(config, items):
    """Retry a failed flow once (the summary lists it as rerun, with its trace). Under xdist,
    tests on the shared ``app_server`` stay on one worker in file order (the Free sweep reads
    what the earlier ones left behind); every other file runs on a worker of its own."""
    for item in items:
        if UI_DIR not in item.path.parents:
            continue
        group = "app_server" if "app_server" in item.fixturenames else item.path.name
        item.add_marker(pytest.mark.xdist_group(group))
        if not item.get_closest_marker("xfail"):
            item.add_marker(pytest.mark.flaky(reruns=1))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    rep = (yield).get_result()
    if rep.when == "call":
        item.kl_failed = rep.failed or (rep.skipped and hasattr(rep, "wasxfail"))


_TRACES: list[str] = []


def pytest_runtest_logreport(report):
    """Runs where the reports arrive (the xdist controller too); the trace paths ride on them."""
    if report.when == "teardown":
        _TRACES.extend(v for k, v in report.user_properties if k == "trace")


def pytest_terminal_summary(terminalreporter):
    if _TRACES:
        terminalreporter.section("browser traces of failed flows (playwright show-trace <file>)")
        for path in sorted(set(_TRACES)):
            terminalreporter.line(path)


def _traces_dir(tmp_path_factory) -> Path:
    """Inside this run's pytest temp folder (pytest keeps the last three runs)."""
    base = tmp_path_factory.getbasetemp()
    root = base.parent if base.name.startswith("popen-gw") else base
    path = root / "traces"
    path.mkdir(exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def _watchdog():
    """The suite's hard stop (kills the whole run), with a budget that fits a browser flow."""
    faulthandler.dump_traceback_later(UI_TEST_TIMEOUT, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture(scope="session")
def kopia() -> str:
    from app.core import kopiabin
    exe = kopiabin.resolve()
    if not exe:
        pytest.skip("kopia not found")
    return exe


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory, kopia) -> dict:
    root = tmp_path_factory.mktemp("kl-ui")
    return {"root": root, **h.build_fixture_repo(root, kopia)}


@pytest.fixture(scope="session")
def kopia_server(fixture_repo, kopia) -> dict:
    """A kopia repository server over the fixture repository."""
    srv = h.start_kopia_server(fixture_repo["root"], kopia, fixture_repo["env"], fixture_repo["config"])
    yield srv
    srv["proc"].terminate()
    try:
        srv["proc"].wait(timeout=20)
    except Exception:  # noqa: BLE001
        srv["proc"].kill()
    srv["log"].close()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browsers not installed
            pytest.skip(f"Playwright Chromium is not available: {exc}")
        yield chromium
        chromium.close()


def _disconnect_created(server: h.Server, kopia: str) -> None:
    """kopia keeps a created connection's password in Windows Credential Manager; disconnecting
    removes it, so a test run leaves nothing behind outside its temp folder."""
    for config in (server.data / "configs").glob("*.config"):
        try:
            h.kopia_run(kopia, server.env, config, "repository", "disconnect")
        except (RuntimeError, OSError):
            pass


def _server(fixture_repo, kopia, name: str, run: int = 1):
    """``run`` is the attempt number: a retried test gets a data folder of its own."""
    folder = re.sub(r"\W+", "-", name)[:36] + f"-r{run}"
    srv = h.Server(fixture_repo["root"], fixture_repo["env"], folder).start()
    try:
        yield srv
    finally:
        srv.stop()
        _disconnect_created(srv, kopia)


@pytest.fixture
def fresh_server(fixture_repo, kopia, request):
    """A new server with an empty data folder: no account, no repository."""
    yield from _server(fixture_repo, kopia, request.node.name, getattr(request.node, "execution_count", 1))


def arrange_indexed(srv: h.Server, fixture_repo: dict) -> str:
    """What onboarding leaves behind: an account, Index encryption on the same password (not
    remembered on this machine), the fixture repository attached and indexed."""
    srv.post("/api/account/create", {"username": h.USERNAME, "password": h.PASSWORD})
    srv.sign_in(h.USERNAME, h.PASSWORD)
    srv.post("/api/encryption/setup", {"password": h.PASSWORD, "level": "index"})
    rid = srv.post("/api/setup/attach", {"config_file": fixture_repo["config"],
                                         "name": "Fixture"})["repo"]["id"]
    srv.post("/api/onboarding/finish")
    srv.post("/api/onboarding/start-indexing", {"repos": [rid]})
    srv.wait_indexed(rid)
    h.assert_contained(srv, fixture_repo["root"])
    return rid


@pytest.fixture(scope="session")
def app_server(fixture_repo, kopia):
    """One onboarded, indexed server shared by the read-mostly flows."""
    gen = _server(fixture_repo, kopia, "indexed")
    srv = next(gen)
    arrange_indexed(srv, fixture_repo)
    yield srv
    gen.close()


@pytest.fixture
def indexed_server(fixture_repo, kopia, request):
    """A server of its own in the same onboarded state, for flows that restart it."""
    gen = _server(fixture_repo, kopia, request.node.name, getattr(request.node, "execution_count", 1))
    srv = next(gen)
    arrange_indexed(srv, fixture_repo)
    yield srv
    gen.close()


@pytest.fixture
def open_page(browser, request, tmp_path_factory):
    """``open_page(server, signed_in=True)`` -> a page in a new browser context. Any console
    error or uncaught exception fails the test, except those matching ``page.kl_allow``. A failed
    test keeps each context's trace (requests, console, page snapshots per step)."""
    opened = []

    def _open(server: h.Server, signed_in: bool = True, width: int = 1400):
        ctx = browser.new_context(viewport={"width": width, "height": 1000}, accept_downloads=True)
        ctx.tracing.start(screenshots=True, snapshots=True)
        page = ctx.new_page()
        page.kl_errors, page.kl_allow = [], []
        page.on("console", lambda m: m.type == "error" and page.kl_errors.append(m.text))
        page.on("pageerror", lambda e: page.kl_errors.append(str(e)))
        page.set_default_timeout(15_000)
        opened.append((ctx, page))
        if signed_in:
            h.sign_in(page, server)
        return page

    yield _open
    from playwright.sync_api import Error as PlaywrightError
    problems = []
    keep_all = os.environ.get("KOPIALENS_UI_TRACES") == "all"   # for timing a passing flow
    for i, (ctx, page) in enumerate(opened):
        mine = [e for e in page.kl_errors if not any(re.search(a, e) for a in page.kl_allow)]
        problems += mine
        keep = keep_all or getattr(request.node, "kl_failed", False) or bool(mine)
        run = getattr(request.node, "execution_count", 1)
        path = _traces_dir(tmp_path_factory) / f"{request.node.name[:60]}-run{run}-{i}.zip"
        try:
            ctx.tracing.stop(path=str(path) if keep else None)
            if keep:
                request.node.user_properties.append(("trace", str(path)))
        except PlaywrightError:   # the test closed this context itself: nothing left to save
            pass
        ctx.close()
    assert not problems, f"browser console errors: {problems}"

