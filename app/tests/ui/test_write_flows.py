"""Flows that change the repository, driven through the browser on a Pro install. Each gets the
result from kopia itself afterwards. They share a fixture repository of their own, so the
read-mostly flows never see these changes."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

from . import _harness as h
from .conftest import arrange_indexed


@pytest.fixture(scope="module")
def own_repo(tmp_path_factory, kopia) -> dict:
    root = tmp_path_factory.mktemp("kl-ui-write")
    return {"root": root, **h.build_fixture_repo(root, kopia)}


@pytest.fixture(scope="module")
def pro_server(own_repo):
    if h.known_answer_licence() is None:
        pytest.skip("LicenseKit test vectors not found")
    srv = h.Server(own_repo["root"], own_repo["env"], "pro", pro=True).start()
    try:
        arrange_indexed(srv, own_repo)
        srv.activate_test_licence()
        yield srv
    finally:
        srv.stop()


def _kopia(kopia: str, repo: dict, *args: str) -> str:
    return h.kopia_run(kopia, repo["env"], Path(repo["config"]), *args)


def _snapshot_listings(kopia: str, repo: dict) -> list[str]:
    snaps = json.loads(_kopia(kopia, repo, "snapshot", "list", "--all", "--json"))
    return [_kopia(kopia, repo, "ls", "-r", s["rootEntry"]["obj"]) for s in snaps]


def _ignore_rules(kopia: str, repo: dict) -> list[str]:
    rules = []
    for pol in json.loads(_kopia(kopia, repo, "policy", "list", "--json")):
        rules += (pol.get("files") or {}).get("ignore") or []
    return rules


def _confirm(page) -> None:
    page.locator("button[x-text='confirmBox.confirmLabel']").click()


def test_a_custom_exclusion_writes_exactly_the_typed_pattern(pro_server, open_page, kopia, own_repo):
    page = open_page(pro_server)
    h.go_tab(page, "Search")
    page.get_by_placeholder("Search paths (matches the full path)").fill("needle")
    page.get_by_role("row", name="needle-report.txt", exact=False).dblclick()
    page.locator("button.kl-btn", has_text="Exclude…").first.click()

    page.locator(".kl-seg span", has_text="Custom").click()
    page.get_by_placeholder("kopia ignore pattern, e.g. /Users/*/Cache/**").fill("/Documents/needle-*.txt")
    commit = page.locator(".kl-footer button.kl-btn.primary", has_text="Exclude…")
    expect(commit).to_be_enabled(timeout=30_000)
    commit.click()
    _confirm(page)

    deadline = time.monotonic() + 60
    while "/Documents/needle-*.txt" not in _ignore_rules(kopia, own_repo) and time.monotonic() < deadline:
        time.sleep(1)
    assert _ignore_rules(kopia, own_repo) == ["/Documents/needle-*.txt"]


def test_delete_matching_removes_the_file_from_every_snapshot(pro_server, open_page, kopia, own_repo):
    """The delete runs the previewed list: the server refuses a plan delete without the
    preview's ticket, so the file only goes if the panel sends it."""
    assert all("big-video.mp4" in ls for ls in _snapshot_listings(kopia, own_repo)[:2])

    page = open_page(pro_server)
    h.go_tab(page, "Search")
    page.get_by_placeholder("Search paths (matches the full path)").fill("big-video")
    expect(page.get_by_role("row", name="big-video.mp4", exact=False)).to_be_visible()
    page.get_by_role("button", name="🗑 Delete matching…").click()

    page.get_by_role("button", name="Dry run", exact=True).click()
    expect(page.get_by_text("dry run checked")).to_be_visible(timeout=60_000)
    page.locator("button.kl-btn.danger", has_text="Delete…").click()
    expect(page.get_by_text("Delete 1 file?")).to_be_visible()
    _confirm(page)

    deadline = time.monotonic() + 120
    while any("big-video.mp4" in ls for ls in _snapshot_listings(kopia, own_repo)):
        assert time.monotonic() < deadline, f"big-video.mp4 is still in a snapshot\n{pro_server.tail()}"
        time.sleep(2)
    assert len(_snapshot_listings(kopia, own_repo)) == 3, "remove-files rewrites snapshots, never drops one"


def test_settings_say_the_licence_is_checked_weekly(pro_server, open_page):
    page = open_page(pro_server)
    page.get_by_title("Settings", exact=True).click()
    page.get_by_role("button", name="Licence", exact=True).click()
    section = page.get_by_test_id("settings-licence")
    expect(section).to_contain_text("Active")
    expect(section).to_contain_text("KopiaLens checks this licence with")
    expect(section).to_contain_text("once a week.")
