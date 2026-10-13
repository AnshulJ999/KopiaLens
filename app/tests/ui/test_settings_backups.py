"""Settings backups, the damaged-file notice and pausing indexing, through the browser."""

from __future__ import annotations

import config
from playwright.sync_api import expect

from . import _harness as h


def _arrange(srv: h.Server, fixture_repo: dict) -> None:
    """An account and the fixture repository, no encryption and no index (a restart then needs
    no unlock)."""
    srv.post("/api/account/create", {"username": h.USERNAME, "password": h.PASSWORD})
    srv.sign_in(h.USERNAME, h.PASSWORD)
    srv.post("/api/setup/attach", {"config_file": fixture_repo["config"], "name": "Fixture"})
    srv.post("/api/onboarding/finish")


def _settings_tab(page, label: str) -> None:
    page.locator("header button[title=Settings]").click()
    page.locator("nav button", has_text=label).click()


def test_back_up_restore_pause_and_recover_a_damaged_file(fresh_server, fixture_repo, open_page):
    srv = fresh_server
    _arrange(srv, fixture_repo)
    page = open_page(srv)

    _settings_tab(page, "Config")
    page.get_by_role("button", name="Back up now").click()
    row = page.locator("div.divide-y > div", has_text="Manual").first
    expect(row).to_be_visible()

    srv.post("/api/settings", {"kopialens": {"index_time": "04:30"}})
    row.get_by_role("button", name="Restore…").click()
    page.get_by_role("button", name="Restore", exact=True).click()
    expect(page.get_by_text("Settings and repository list restored.")).to_be_visible()
    page.get_by_role("button", name="Later").click()
    assert srv.get("/api/repos")["settings"]["kopialens"]["index_time"] == "23:00"

    page.locator("nav button", has_text="Indexing").click()
    page.get_by_role("button", name="Until I resume").click()
    chip = page.locator("header + nav").get_by_text("Indexing paused")
    expect(chip).to_be_visible()
    assert srv.get("/api/index/pause")["paused"] is True
    page.locator("section", has_text="Pause indexing").get_by_role("button", name="Resume").click()
    expect(chip).to_be_hidden()

    page.locator("nav button", has_text="About").click()
    expect(page.get_by_text(config.APP_VERSION, exact=True)).to_be_visible()

    srv.stop()
    (srv.data / "settings.json").write_text("{torn", encoding="utf-8")
    srv.start()
    page = open_page(srv)
    notice = page.get_by_text("Your settings file was damaged. KopiaLens restored the last good copy")
    expect(notice).to_be_visible()
    page.get_by_role("button", name="Dismiss").click()
    expect(notice).to_be_hidden()
    assert h.wait_until(lambda: srv.get("/api/config-backups")["notices"] == [])


def test_desktop_app_tray_settings_save(fresh_server, fixture_repo, open_page):
    srv = fresh_server
    _arrange(srv, fixture_repo)
    page = open_page(srv)

    _settings_tab(page, "Desktop app")
    page.get_by_test_id("tray-click").select_option("menu")
    page.get_by_test_id("tray-double-click").select_option("none")
    page.get_by_test_id("notify-level").select_option("warnings")
    expect(page.get_by_text("Right-clicking always shows the menu.")).to_be_visible()
    page.wait_for_timeout(500)
    kl = srv.get("/api/repos")["settings"]["kopialens"]
    assert (kl["tray_click"], kl["tray_double_click"], kl["notifications"]) == ("menu", "none", "warnings")
