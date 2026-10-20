"""KopiaLens runs a repository's backups (core/engine.py), driven through the browser on a
repository of its own: take over, back up now (checked through kopia itself), the task in Tasks,
stepping back when another kopia server starts on the same repository, and handing back."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

from . import _harness as h
from .conftest import arrange_indexed


@pytest.fixture(scope="module")
def engine_repo(tmp_path_factory, kopia) -> dict:
    root = tmp_path_factory.mktemp("kl-ui-engine")
    return {"root": root, **h.build_fixture_repo(root, kopia)}


@pytest.fixture
def engine_server(engine_repo, kopia):
    srv = h.Server(engine_repo["root"], engine_repo["env"], "engine").start()
    try:
        rid = arrange_indexed(srv, engine_repo)
        srv.rid = rid
        yield srv
    finally:
        srv.stop()


def _snapshot_count(kopia: str, repo: dict) -> int:
    out = h.kopia_run(kopia, repo["env"], Path(repo["config"]), "snapshot", "list", "--all", "--json")
    return len(json.loads(out))


def _state(srv: h.Server) -> str:
    return next(r for r in srv.get("/api/engine/status")["repos"] if r["repo"] == srv.rid)["state"]


def _wait(check, what: str, srv: h.Server, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"{what}\n{srv.tail()}"
        time.sleep(1)


def _rival(kopia: str, repo: dict) -> subprocess.Popen:
    """Another engine on the same repository, through a connection file of its own."""
    cfg = Path(repo["root"]) / "rival.config"
    env = {**repo["env"], "KOPIA_PASSWORD": h.REPO_PASSWORD}
    if not cfg.exists():
        h.kopia_run(kopia, env, cfg, "repository", "connect", "filesystem", "--path", repo["repo"],
                    *h.NO_CRED_MANAGER, "--override-hostname", h.HOST, "--override-username", h.USER)
    return h.contain(subprocess.Popen([kopia, "--config-file", str(cfg), "server", "start", "--insecure",
                                       "--random-password", "--no-grpc", "--address=127.0.0.1:0"],
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))


def test_kopialens_runs_backups_and_steps_back_for_another_engine(engine_server, open_page, kopia, engine_repo):
    page = open_page(engine_server)
    chip = page.get_by_test_id("backups-chip")
    expect(chip).to_be_visible(timeout=20_000)

    chip.click()
    page.get_by_role("button", name="Who runs backups…").first.click()
    dialog = page.get_by_test_id("engine-dialog")
    expect(dialog).to_contain_text("Connected as")
    dialog.get_by_text("KopiaLens runs kopia's schedules").click()
    dialog.get_by_role("button", name="Save").click()
    page.locator("button[x-text='confirmBox.confirmLabel']").click()
    _wait(lambda: _state(engine_server) == "running", "KopiaLens's kopia server did not start", engine_server)

    before = _snapshot_count(kopia, engine_repo)
    page.reload()
    row_button = page.locator("#dashboardTable button[data-act='snap']").first
    expect(row_button).to_be_visible(timeout=30_000)
    row_button.click()
    page.locator("button[x-text='confirmBox.confirmLabel']").click()
    _wait(lambda: _snapshot_count(kopia, engine_repo) > before, "the backup did not create a snapshot", engine_server)
    # Snapshot now follows the server's task in the job panel: kopia's counters, then a close button.
    job = page.locator("div[x-show='showJob']")
    expect(job).to_contain_text("Uploaded Bytes", timeout=30_000)
    close = job.locator("button[aria-label='Close']")
    expect(close).to_be_visible(timeout=30_000)
    close.click()

    h.go_tab(page, "Tasks")
    page.get_by_test_id("task-hide-routine").uncheck()   # a test snapshot takes under 2 s: routine
    expect(page.locator("#tasksTable").get_by_text("Succeeded").first).to_be_visible(timeout=30_000)

    rival = _rival(kopia, engine_repo)
    try:
        _wait(lambda: _state(engine_server) == "blocked", "KopiaLens did not step back", engine_server, timeout=60)
        h.go_tab(page, "Dashboard")
        expect(page.get_by_test_id("engine-banner")).to_contain_text("is running backups for this repository", timeout=20_000)
    finally:
        rival.terminate()
        rival.wait(timeout=20)

    chip.click()
    page.get_by_role("button", name="Who runs backups…").first.click()
    dialog.get_by_text("KopiaLens shows and analyses the backups").click()
    dialog.get_by_role("button", name="Save").click()
    page.locator("button[x-text='confirmBox.confirmLabel']").click()
    _wait(lambda: _state(engine_server) == "off", "handing back did not stop KopiaLens's backups", engine_server)
