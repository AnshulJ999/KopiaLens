"""A Free install (no licence): Pro buttons carry a PRO tag and open the upgrade notice instead of
acting; the preview stays visible; Settings -> Licence shows Free."""

from __future__ import annotations

from playwright.sync_api import expect

from . import _harness as h


def test_free_sees_pro_tags_and_the_upgrade_notice(app_server, open_page):
    page = open_page(app_server)
    h.go_tab(page, "Search")
    page.get_by_placeholder("Search paths", exact=True).fill("needle")
    page.get_by_role("row", name="needle-report.txt", exact=False).dblclick()

    # The file panel hands off to the Delete panel, whose red button carries the Pro lock.
    page.locator("#kl-file-remove").get_by_role("button", name="Remove from snapshots…").click()
    remove = page.locator("div[x-show='showDel'] button.kl-btn.danger", has_text="Remove from")
    expect(remove).to_contain_text("PRO")
    expect(remove).to_be_enabled(timeout=30_000)
    remove.click()
    notice = page.get_by_role("dialog", name="Part of KopiaLens Pro")
    expect(notice).to_contain_text("Removing files from snapshots needs Pro. The preview stays free.")
    expect(notice.get_by_role("button", name="See plans")).to_be_focused()
    page.keyboard.press("Escape")
    expect(notice).to_be_hidden()
    expect(page.locator("div[x-show='showDel']")).to_be_visible()   # the panel stayed open

    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    page.get_by_title("Settings", exact=True).click()
    page.get_by_role("button", name="Licence", exact=True).click()
    section = page.get_by_test_id("settings-licence")
    expect(section).to_contain_text("Free")
    expect(section.get_by_role("button", name="Activate")).to_be_visible()
    expect(page.locator("header .pro-mark")).to_be_hidden()
