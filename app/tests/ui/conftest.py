"""Browser-suite fixtures: one fixture repository per run, throwaway KopiaLens servers against it,
and pages that fail the test on any browser console error.

Run by name only: ``pytest Kopia/KopiaLens/app/tests/ui`` (the default suite skips this folder).
"""

from __future__ import annotations

import faulthandler
import re

import pytest

from . import _harness as h

UI_TEST_TIMEOUT = 240     # a flow can include a first index; Playwright bounds each wait itself


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


def _server(fixture_repo, kopia, name: str):
    srv = h.Server(fixture_repo["root"], fixture_repo["env"], re.sub(r"\W+", "-", name)[:40]).start()
    try:
        yield srv
    finally:
        srv.stop()
        _disconnect_created(srv, kopia)


@pytest.fixture
def fresh_server(fixture_repo, kopia, request):
    """A new server with an empty data folder: no account, no repository."""
    yield from _server(fixture_repo, kopia, request.node.name)


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
    gen = _server(fixture_repo, kopia, request.node.name)
    srv = next(gen)
    arrange_indexed(srv, fixture_repo)
    yield srv
    gen.close()


@pytest.fixture
def open_page(browser):
    """``open_page(server, signed_in=True)`` -> a page in a new browser context. Any console
    error or uncaught exception fails the test, except those matching ``page.kl_allow``."""
    opened = []

    def _open(server: h.Server, signed_in: bool = True, width: int = 1400):
        ctx = browser.new_context(viewport={"width": width, "height": 1000}, accept_downloads=True)
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
    problems = []
    for ctx, page in opened:
        problems += [e for e in page.kl_errors if not any(re.search(a, e) for a in page.kl_allow)]
        ctx.close()
    assert not problems, f"browser console errors: {problems}"

