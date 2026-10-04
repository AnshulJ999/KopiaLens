"""A Free install (no licence): Pro buttons carry a PRO tag and open the upgrade notice instead of
acting; the preview stays visible; Settings -> Licence shows Free."""

from __future__ import annotations

from playwright.sync_api import expect

from . import _harness as h


def test_free_sees_pro_tags_and_the_upgrade_notice(app_server, open_page):
    page = open_page(app_server)
    h.go_tab(page, "Search")
    page.get_by_placeholder("Search paths (matches the full path)").fill("needle")
    page.get_by_role("row", name="needle-report.txt", exact=False).dblclick()

    remove = page.locator("#kl-file-remove button.kl-btn.danger")
    expect(remove).to_contain_text("PRO")
    remove.click()
    notice = page.get_by_role("dialog", name="Part of KopiaLens Pro")
    expect(notice).to_contain_text("Removing files from snapshots needs Pro. The preview stays free.")
    expect(notice.get_by_role("button", name="See plans")).to_be_focused()
    page.keyboard.press("Escape")
    expect(notice).to_be_hidden()
    expect(page.get_by_text("Remove from all snapshots")).to_be_visible()   # the panel stayed open

    page.keyboard.press("Escape")
    page.get_by_title("Settings", exact=True).click()
    page.get_by_role("button", name="Licence", exact=True).click()
    section = page.get_by_test_id("settings-licence")
    expect(section).to_contain_text("Free")
    expect(section.get_by_role("button", name="Activate")).to_be_visible()
    expect(page.locator("header .protag")).to_be_hidden()
