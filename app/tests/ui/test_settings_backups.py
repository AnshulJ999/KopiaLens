"""Settings backups, the damaged-file notice and pausing indexing, through the browser."""

from __future__ import annotations

import json
import urllib.request

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
    shown = config.APP_VERSION + (f" ({config.APP_COMMIT})" if config.APP_COMMIT else "")
    expect(page.get_by_text(shown, exact=True)).to_be_visible()

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


def test_settings_search_finds_a_setting_and_opens_it(fresh_server, fixture_repo, open_page):
    srv = fresh_server
    _arrange(srv, fixture_repo)
    page = open_page(srv)

    page.locator("header button[title=Settings]").click()
    page.keyboard.press("Control+f")
    box = page.get_by_test_id("settings-search")
    expect(box).to_be_focused()
    box.fill("debug log")
    results = page.get_by_test_id("settings-results")
    results.get_by_role("button", name="Debug log").first.click()
    expect(box).to_have_value("")
    row = page.locator(".kl-flash", has_text="Debug log")
    expect(row).to_be_visible()

    box.fill("pasword")                 # one letter off still finds the Account section
    expect(results.get_by_role("button", name="Account Section", exact=True)).to_be_visible()
    box.fill("zzqx")
    expect(results.get_by_text("No settings match")).to_be_visible()
    box.press("Escape")
    expect(box).to_have_value("")
    expect(page.locator("nav button", has_text="Repositories")).to_be_visible()


def test_desktop_window_signs_in_by_itself_only_when_turned_on(fresh_server, fixture_repo, open_page):
    srv = fresh_server
    srv.stop()
    srv.env["KOPIALENS_SHELL_TOKEN"] = "shell-test-token"
    srv.start()
    _arrange(srv, fixture_repo)

    def window_page():
        """What the desktop app does: ask for a one-time code, open the window through it."""
        req = urllib.request.Request(srv.url + "/api/shell/open-code", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json",
                                              "x-kopialens-shell": "shell-test-token"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = json.loads(resp.read())["code"]
        page = open_page(srv, signed_in=False)
        page.kl_allow.append("status of 401")   # the sign-in page itself answers 401
        page.goto(f"{srv.url}/shell/open?code={code}&next=/")
        return page

    page = window_page()                 # off by default: the window gets the sign-in page
    expect(page.locator("input[name=password]")).to_be_visible()

    srv.post("/api/settings", {"kopialens": {"desktop_signin": True}})
    page = window_page()
    page.locator("button.tab").first.wait_for(timeout=30_000)

    browser_page = open_page(srv, signed_in=False)   # a browser has no code: still asked
    browser_page.kl_allow.append("status of 401")
    browser_page.goto(srv.url + "/")
    expect(browser_page.locator("input[name=password]")).to_be_visible()
