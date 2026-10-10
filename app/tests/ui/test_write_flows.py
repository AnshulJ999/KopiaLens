"""Flows that change the repository, driven through the browser on a Pro install. Each gets the
result from kopia itself afterwards. They use a fixture repository of their own, so the
read-mostly flows never see these changes, and each flow starts from the same saved state."""

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
def saved_pro_server(own_repo):
    """A Pro server set up and indexed once; its whole temp root is then saved."""
    if h.known_answer_licence() is None:
        pytest.skip("LicenseKit test vectors not found")
    srv = h.Server(own_repo["root"], own_repo["env"], "pro", pro=True).start()
    try:
        arrange_indexed(srv, own_repo)
        srv.activate_test_licence()
    finally:
        srv.stop()
    h.save_state(own_repo["root"])
    yield srv
    srv.stop()


@pytest.fixture
def pro_server(saved_pro_server, own_repo, tmp_path_factory):
    """The saved state put back and the server started on it."""
    h.restore_state(own_repo["root"], tmp_path_factory.getbasetemp())
    saved_pro_server.start()
    h.assert_contained(saved_pro_server, own_repo["root"])
    yield saved_pro_server
    saved_pro_server.stop()


def _kopia(kopia: str, repo: dict, *args: str) -> str:
    return h.kopia_run(kopia, repo["env"], Path(repo["config"]), *args)


def _snapshots(kopia: str, repo: dict) -> list[dict]:
    """kopia's own snapshot list, oldest first."""
    snaps = json.loads(_kopia(kopia, repo, "snapshot", "list", "--all", "--json"))
    return sorted(snaps, key=lambda s: s["startTime"])


def _snapshot_listings(kopia: str, repo: dict) -> list[str]:
    """Every path in each snapshot, oldest snapshot first."""
    return [_kopia(kopia, repo, "ls", "-r", s["rootEntry"]["obj"]) for s in _snapshots(kopia, repo)]


def _wait_for(check, what: str, server: h.Server, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"{what}\n{server.tail()}"
        time.sleep(1)


def _open_file(page, query: str, name: str) -> None:
    h.go_tab(page, "Search")
    page.get_by_placeholder("Search paths", exact=True).fill(query)
    page.get_by_role("row", name=name, exact=False).dblclick()


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
    page.get_by_placeholder("Search paths", exact=True).fill("needle")
    page.get_by_role("row", name="needle-report.txt", exact=False).dblclick()
    page.locator("div[x-show='detail'] button.kl-btn", has_text="Exclude…").first.click()

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
    page.get_by_placeholder("Search paths", exact=True).fill("big-video")
    expect(page.get_by_role("row", name="big-video.mp4", exact=False)).to_be_visible()
    page.get_by_test_id("search-more").click()
    page.get_by_role("button", name="Delete all 1 matching…").click()

    page.get_by_role("button", name="Dry run", exact=True).click()
    expect(page.get_by_text("dry run checked")).to_be_visible(timeout=60_000)
    page.locator("button.kl-btn.danger:visible", has_text="Remove 1 file from").click()
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


def test_deleting_a_snapshot_removes_exactly_that_one(pro_server, open_page, kopia, own_repo):
    times = [s["startTime"] for s in _snapshots(kopia, own_repo)]
    page = open_page(pro_server)
    h.go_tab(page, "Snapshots")
    rows = page.locator("#snapshotsTable .tabulator-row")
    expect(rows).to_have_count(3)
    rows.filter(has_text="latest-3").click()          # the oldest of the three
    page.get_by_role("button", name="Delete snapshot…", exact=True).click()
    page.get_by_role("button", name="Delete 1 snapshot…", exact=True).click()
    expect(page.get_by_text("Delete 1 snapshot?")).to_be_visible()
    _confirm(page)

    _wait_for(lambda: len(_snapshots(kopia, own_repo)) == 2, "the snapshot was not deleted", pro_server)
    assert [s["startTime"] for s in _snapshots(kopia, own_repo)] == times[1:]


def test_deleting_the_snapshots_that_hold_a_file_keeps_the_others(pro_server, open_page, kopia, own_repo):
    times = [s["startTime"] for s in _snapshots(kopia, own_repo)]
    page = open_page(pro_server)
    _open_file(page, "big-video", "big-video.mp4")
    page.locator(".kl-sec", has_text="Delete the snapshots holding it").get_by_role("button", name="Delete 2 snapshots…").click()
    run = page.locator("div[x-show='showDel'] button.kl-btn.danger", has_text="Delete 2 snapshots")
    expect(run).to_be_enabled(timeout=30_000)
    run.click()
    expect(page.get_by_text("Delete 2 snapshots?")).to_be_visible()
    _confirm(page)

    _wait_for(lambda: len(_snapshots(kopia, own_repo)) == 1, "the snapshots were not deleted", pro_server)
    assert [s["startTime"] for s in _snapshots(kopia, own_repo)] == times[2:]
    assert "big-video.mp4" not in _snapshot_listings(kopia, own_repo)[0]


def test_pruning_the_old_version_keeps_the_newer_one(pro_server, open_page, kopia, own_repo):
    """notes-0.txt changed between the first and second snapshot, so it has two versions."""
    page = open_page(pro_server)
    _open_file(page, "notes-0", "notes-0.txt")
    page.get_by_role("button", name="Prune versions…").click()
    ticks = page.locator("div[x-show='showPrune'] .kl-list input[type=checkbox]")
    expect(ticks).to_have_count(2)
    ticks.last.check()                                  # newest first, so the last row is the oldest
    run = page.get_by_role("button", name="Remove 1 version…")
    expect(run).to_be_enabled(timeout=30_000)
    run.click()
    expect(page.get_by_text("Remove 1 version of notes-0.txt?")).to_be_visible()
    _confirm(page)

    _wait_for(lambda: "notes-0.txt" not in _snapshot_listings(kopia, own_repo)[0],
              "the old version is still in the first snapshot", pro_server)
    listings = _snapshot_listings(kopia, own_repo)
    assert len(listings) == 3
    assert all("notes-0.txt" in ls for ls in listings[1:]), "the newer version must stay"


def test_exclude_and_delete_adds_the_rule_and_removes_the_file(pro_server, open_page, kopia, own_repo):
    page = open_page(pro_server)
    _open_file(page, "needle", "needle-report.txt")
    page.locator("div[x-show='detail']").get_by_role("button", name="Exclude & delete…").first.click()
    step = page.get_by_role("button", name="Next: delete")
    expect(step).to_be_enabled(timeout=30_000)
    step.click()
    review = page.get_by_role("button", name="Next: review")
    expect(review).to_be_enabled(timeout=30_000)
    review.click()
    page.locator("div[x-show='showReview']").get_by_role("button", name="Exclude and delete…").click()
    expect(page.get_by_text("Exclude and delete?")).to_be_visible()
    _confirm(page)

    _wait_for(lambda: not any("needle-report.txt" in ls for ls in _snapshot_listings(kopia, own_repo)),
              "needle-report.txt is still in a snapshot", pro_server)
    assert any("needle-report.txt" in rule for rule in _ignore_rules(kopia, own_repo))
    assert len(_snapshots(kopia, own_repo)) == 3


def _full_runs(kopia: str, repo: dict) -> list[dict]:
    """kopia's record of snapshot garbage collection, a task only full maintenance runs."""
    info = json.loads(_kopia(kopia, repo, "maintenance", "info", "--json"))
    return ((info.get("schedule") or {}).get("runs") or {}).get("snapshot-gc") or []


def test_full_maintenance_runs_in_kopia(pro_server, open_page, kopia, own_repo):
    before = len(_full_runs(kopia, own_repo))
    page = open_page(pro_server)
    h.go_tab(page, "Repository")
    page.get_by_role("button", name="Run maintenance…").click()
    page.get_by_role("button", name="Run full maintenance…").click()
    expect(page.get_by_text("Run full maintenance?")).to_be_visible()
    _confirm(page)

    _wait_for(lambda: len(_full_runs(kopia, own_repo)) > before, "kopia recorded no full maintenance", pro_server)
    assert len(_snapshots(kopia, own_repo)) == 3
