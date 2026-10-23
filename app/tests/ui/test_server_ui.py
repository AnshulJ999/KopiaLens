"""A repository reached through a kopia server: connected from onboarding as browse-only, with
snapshots, the folder browser and restore working and the index-only views withheld."""

from __future__ import annotations

from playwright.sync_api import expect

from . import _harness as h


def _footer(page, label: str):
    return page.locator(".kl-footer button", has_text=label)


def test_server_repository_is_browse_only(fresh_server, open_page, kopia_server):
    page = open_page(fresh_server, signed_in=False)
    page.goto(fresh_server.url + "/")

    _footer(page, "Start setup").click()
    page.get_by_role("button", name="Connect to an existing one").click()
    page.locator(".kl-chip", has_text="Kopia repository server").click()
    page.get_by_role("button", name="Continue with browsing only").click()
    page.get_by_role("textbox", name="Server URL").fill(kopia_server["url"])
    page.get_by_text("Advanced options").click()
    page.get_by_role("textbox", name="Certificate fingerprint").fill(kopia_server["fingerprint"])
    page.get_by_role("textbox", name="Username", exact=True).fill(kopia_server["username"])
    page.get_by_role("textbox", name="Hostname", exact=True).fill(kopia_server["hostname"])
    page.get_by_label("Repository password").first.fill(kopia_server["password"])
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.get_by_text("1 source · 3 snapshots", exact=False)).to_be_visible(timeout=60_000)
    _footer(page, "Continue").click()

    page.get_by_role("button", name="Skip protection").click()
    page.get_by_role("button", name="Continue without a password").click()
    _footer(page, "Skip for now").click()
    page.get_by_role("button", name="Skip for now").last.click()
    _footer(page, "Open KopiaLens").click()

    expect(page.get_by_text("Browse-only: this repository is reached through a kopia server")).to_be_visible(timeout=30_000)
    expect(page.locator("button.tab")).to_have_text(["Snapshots", "Policies", "Tasks", "Logs"])
    expect(page.get_by_role("button", name="Index now")).to_have_count(0)
    rows = page.locator("#snapshotsTable .tabulator-row")
    expect(rows).to_have_count(3, timeout=30_000)
    expect(page.locator("#snapshotsTable .tabulator-col-title", has_text="Reclaimable")).to_have_count(0)

    rows.first.dblclick()
    browser = page.get_by_role("dialog", name="Browse snapshot")
    expect(browser.get_by_text("Snapshot from")).to_be_visible()
    browser.get_by_role("button", name="Documents").click()
    report = browser.get_by_role("row", name="needle-report.txt")
    expect(report).to_be_visible(timeout=30_000)
    expect(browser.locator("nav [aria-current=location]")).to_have_text("Documents")
    report.get_by_role("button", name="Restore…").click()
    expect(page.get_by_text("Restore needle-report.txt")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_text("Restore needle-report.txt")).to_be_hidden()
    page.keyboard.press("Escape")
    expect(browser).to_be_hidden()

    h.go_tab(page, "Policies")   # policies are read through the server as well
    expect(page.get_by_text("Keeps 10 latest, 48 hourly, 7 daily, 4 weekly, 24 monthly, 3 yearly").first).to_be_visible(timeout=30_000)

    rid = fresh_server.get("/api/repos")["repos"][0]["id"]
    assert fresh_server.request("POST", f"/api/index/build?repo={rid}")["started"] is False
