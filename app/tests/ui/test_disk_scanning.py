"""Settings > Disk scanning: a place added there is checked in the Excluded tab against the path
its backups use (here a copy of the fixture's source stands in for a network share)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from playwright.sync_api import expect

from . import _harness as h


def _latest_names(kopia: str, fixture_repo: dict) -> set[str]:
    """Top-level names in the fixture source's newest snapshot, straight from kopia."""
    config = Path(fixture_repo["config"])
    snaps = json.loads(h.kopia_run(kopia, fixture_repo["env"], config, "snapshot", "list",
                                   fixture_repo["source"], "--json"))
    newest = max(snaps, key=lambda s: s["startTime"])
    out = h.kopia_run(kopia, fixture_repo["env"], config, "ls", newest["rootEntry"]["obj"])
    return {line.strip().rstrip("/\\") for line in out.splitlines() if line.strip()}


def test_an_added_place_is_compared_with_its_backed_up_path(indexed_server, open_page, fixture_repo, kopia):
    share = Path(fixture_repo["root"]) / f"share-{indexed_server.data.name}"
    shutil.copytree(fixture_repo["source"], share)
    (share / "only-on-the-share.txt").write_text("not in any backup")
    backed = _latest_names(kopia, fixture_repo)
    # Only the added place: the test never counts this computer's real drives.
    indexed_server.post("/api/settings", {"kopialens": {"disk_lookup": "scan", "disk_show_local": False}})

    page = open_page(indexed_server)
    page.get_by_title("Settings", exact=True).click()
    page.get_by_role("button", name="Disk scanning", exact=True).click()
    section = page.get_by_test_id("settings-diskscan")
    section.get_by_role("button", name="Add a place").click()
    section.get_by_label("Folder on this computer").fill(str(share))
    section.get_by_label("Backed up as").fill(fixture_repo["source"])
    section.get_by_label("On computer").fill(h.HOST)
    section.get_by_role("button", name="Save", exact=True).click()
    expect(section).to_contain_text(f"Backed up as {fixture_repo['source']} on {h.HOST}")
    page.locator("div[x-show='showSettings'] button[aria-label='Close']").click()

    h.go_tab(page, "Excluded")
    place = page.get_by_role("row", name=share.name, exact=False)
    expect(place).to_contain_text("backed up as")
    place.locator(".tabulator-cell").first.dblclick()   # the row's centre can be a column resize handle
    rows = page.locator("#excludedTable .tabulator-row")
    expect(page.locator("span[x-text='excludedMeta']")).to_contain_text(f"backed up as {fixture_repo['source']} on {h.HOST}")
    expect(rows.filter(has_text="only-on-the-share.txt").locator(".pill")).to_have_text("new (not yet)")   # newer than any snapshot
    page.get_by_role("button", name="Backed up", exact=True).click()
    # Unchanged since the newest snapshot, so the scanner's sizes equal the backed-up ones.
    folders = {p.name for p in share.iterdir() if p.is_dir() and any(f.is_file() for f in p.rglob("*"))} & backed
    assert folders, f"the fixture's newest snapshot has no top-level folders: {backed}"
    for name in folders:
        expect(rows.filter(has_text=name).first.locator(".pill")).to_have_text("backed up")
    # The fixture's Archive folder is empty on disk: nothing to back up, listed under All only.
    assert (share / "Archive").is_dir() and not any((share / "Archive").iterdir())
    expect(rows.filter(has_text="Archive")).to_have_count(0)
    page.get_by_role("button", name="All", exact=True).click()
    expect(rows.filter(has_text="Archive").first.locator(".pill")).to_have_text("empty")
