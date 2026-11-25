"""The user docs (docs/*.md): links and "?" buttons resolve, no internal words leak, and the
defaults the pages state match the code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
FRONTEND = ROOT / "frontend"

LEAK = re.compile(r"\b(?:KL|REL|BZ|CH|PERF|STO)\d+\b|\bAnshul\b")
INTERNAL = re.compile(r"\bgenerations?\b|\bcid\b|content objects?|delta index|owner role|freeable|—",
                      re.IGNORECASE)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
PAGE_LINK = re.compile(r"^([\w.-]+)\.md(?:#(.*))?$")


def _prose(text: str) -> str:
    """The page without fenced code blocks and inline code, which quote programs verbatim."""
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    return re.sub(r"`[^`\n]*`", "", text)


def _slug(heading: str) -> str:
    """A heading's link id, made the way js/docs.js docSlug (and GitHub) make it."""
    s = re.sub(r"<[^>]+>", "", heading.strip().lower())
    s = "".join(c for c in s if c.isalnum() or c.isspace() or c == "-")
    return re.sub(r"\s", "-", s)


def _pages() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(DOCS.glob("*.md"))}


def _anchors(text: str) -> set[str]:
    return {_slug(m.group(1)) for m in re.finditer(r"^#{1,6} (.+)$", _prose(text), flags=re.M)}


PAGES = _pages()
ANCHORS = {name: _anchors(text) for name, text in PAGES.items()}


def _target_problem(page: str, section: str | None) -> str | None:
    if page not in PAGES:
        return f"no page {page}.md"
    if section and section not in ANCHORS[page]:
        return f"no heading #{section} in {page}.md"
    return None


def test_links_between_pages_reach_a_page_and_heading():
    bad = []
    for name, text in PAGES.items():
        for target in LINK.findall(_prose(text)):
            if m := PAGE_LINK.match(target):
                page, section = m.group(1), m.group(2)
            elif target.startswith("#"):
                page, section = name, target[1:]
            else:
                continue
            if problem := _target_problem(page, section):
                bad.append(f"{name}.md -> {target}: {problem}")
    assert not bad, bad


def test_help_buttons_in_the_app_reach_a_page_and_heading():
    calls = re.compile(r"""openDocs\(\s*['"]([\w-]+)['"]\s*(?:,\s*['"]([\w-]+)['"])?\s*\)""")
    bad = []
    for f in [*sorted((FRONTEND / "partials").rglob("*.html")), *sorted((FRONTEND / "js").rglob("*.js"))]:
        for page, section in calls.findall(f.read_text(encoding="utf-8")):
            if problem := _target_problem(page, section or None):
                bad.append(f"{f.name}: openDocs({page}, {section}): {problem}")
    settings_map = re.search(r"const SETTINGS_DOCS = \{(.*?)\};", (FRONTEND / "js" / "docs.js").read_text(encoding="utf-8"), re.S)
    assert settings_map, "SETTINGS_DOCS not found in js/docs.js"
    for key, page, section in re.findall(r'(\w+): \["([\w-]+)"(?:, "([\w-]+)")?\]', settings_map.group(1)):
        if problem := _target_problem(page, section or None):
            bad.append(f"SETTINGS_DOCS.{key}: {problem}")
    assert not bad, bad


def test_pages_carry_no_item_codes_names_or_internal_words():
    bad = []
    for name, text in PAGES.items():
        for n, line in enumerate(_prose(text).splitlines(), 1):
            if m := (LEAK.search(line) or INTERNAL.search(line)):
                bad.append(f"{name}.md:{n}: {m.group(0)!r} in {line.strip()[:70]}")
    assert not bad, bad


def _kl(key):
    from app.settings import DEFAULT_SETTINGS
    return lambda: DEFAULT_SETTINGS["kopialens"][key]


def _setup_minutes():
    from app.core import auth
    return auth.SETUP_WINDOW // 60


def _find_depth():
    from app.core import repoconnect
    return repoconnect.FIND_DEPTH


def _session_range():
    from app.core import auth
    return min(auth.SESSION_CHOICES), max(auth.SESSION_CHOICES) // 365


def _sign_in_wait():
    from app.core import auth
    return int(auth._THROTTLE_MAX)


def _keep_versions():
    from app.index import versions
    from app.settings import DEFAULT_SETTINGS
    return DEFAULT_SETTINGS["kopialens"]["index_keep_versions"], versions.MAX_KEEP


def _approval_hours():
    from app.core import approvals
    return approvals.TTL // 3600


def _plan_hours():
    from app.api import actions
    return int(actions._PLANS_TTL) // 3600


def _licence_grace_days():
    from app.licensing.licensekit import GRACE
    return GRACE.days


def _kl_in(key, unit):
    """A setting stated in another unit: seconds as minutes (60) or hours (3600), a fraction as a percentage (0.01)."""
    from app.settings import DEFAULT_SETTINGS
    def value():
        return round(DEFAULT_SETTINGS["kopialens"][key] / unit)
    value.__name__ = key
    return value


def _index_time():
    from app.settings import DEFAULT_SETTINGS
    return tuple(int(p) for p in DEFAULT_SETTINGS["kopialens"]["index_time"].split(":"))


def _ports():
    from app.settings import DEFAULT_SETTINGS
    kl = DEFAULT_SETTINGS["kopialens"]
    return kl["port"], kl["control_port"]


def _memory_range():
    from app.index import store
    return store.MEMORY_SHARE, store.MIN_MEMORY_MB, store.MAX_MEMORY_MB // 1024


def _worker_threads():
    from app.index import store
    return store.WORKER_THREADS


def _scanner_threads():
    """The disk scanner's automatic thread cap (shell/disk-scanner)."""
    src = (Path(__file__).resolve().parents[2] / "shell" / "disk-scanner" / "src" / "main.rs").read_text(encoding="utf-8")
    return int(re.search(r"available_parallelism\(\)[^;]*\.min\((\d+)\)", src).group(1))


def _all_repos_depth():
    from app.api import search
    return search._ALL_REPOS_DEPTH


def _restore_preview_cap():
    from app.api import restore
    return restore.LIST_CAP


def _size_chart_versions():
    m = re.search(r"\.versions\) \|\| \[\]\)\.slice\(0, (\d+)\)", (FRONTEND / "js" / "detail.js").read_text(encoding="utf-8"))
    return int(m.group(1))


def _search_delete_max():
    m = re.search(r"const SEARCH_DELETE_MAX = ([\d_]+)", (FRONTEND / "js" / "search.js").read_text(encoding="utf-8"))
    return int(m.group(1).replace("_", ""))


# Every default or fixed limit a page states: (page, pattern with one group per number, value(s) in code).
# Numbers written as words ("one hour") are matched as words.
DEFAULTS = [
    ("backups.md", r"History is kept for (\d+) days by default", _kl("task_history_days")),
    ("sign-in.md", r"\((\d+) days by default\)", _kl("session_days")),
    ("sign-in.md", r"(\d+) day to (\d+) year", _session_range),
    ("sign-in.md", r"setup is open for \*\*(\d+) minutes\*\*", _setup_minutes),
    ("sign-in.md", r"up to (\d+) seconds", _sign_in_wait),
    ("getting-started.md", r"setup is open for (\d+) minutes", _setup_minutes),
    ("docker.md", r"For the first (\d+) minutes after a start", _setup_minutes),
    ("docker.md", r"up to (\d+) folders deep", _find_depth),
    ("glossary.md", r"more than (\d+) minutes after KopiaLens started", _setup_minutes),
    ("indexing.md", r"\((\d+) by default, up to (\d+)", _keep_versions),
    ("api-tokens.md", r"expires after (\w+) hours?", _approval_hours),
    ("api.md", r"It expires after (\w+) hours?", _approval_hours),
    ("api.md", r"It's valid for (\w+) hours?", _plan_hours),
    ("cli.md", r"is valid for (\w+) hours?", _plan_hours),
    ("licensing.md", r"for (\d+) more days after the end date", _licence_grace_days),
    ("tasks.md", r"kept for (\d+) days by default", _kl("task_history_days")),
    ("logs.md", r"removed after (\d+) days by default", _kl("log_cleanup_days")),
    ("churn.md", r"how many folder levels deep to group by, (\d+) by default", _kl("churn_depth")),
    ("churn.md", r"The default is (\d+) days", _kl("churn_days")),
    ("excluded.md", r"\((\d+) seconds by default\)", _kl("disk_scan_budget")),
    ("search.md", r"\((\d+) by default\)", _kl("rank_count")),
    ("search.md", r"offered for up to (\d+) files", _search_delete_max),
    ("search.md", r"Results stop at (\d+) rows", _all_repos_depth),
    ("indexing.md", r"once a day at (\d+):(\d+)", _index_time),
    ("indexing.md", r"tried (\w+) more after (\d+) minutes", lambda: (_kl("index_max_retries")(), _kl_in("index_retry_delay", 60)())),
    ("indexing.md", r"on its own (\d+) minutes after the last delete", _kl_in("reindex_after_delete_delay", 60)),
    ("indexing.md", r"\*\*Indexing time limit\*\* \| (\d+) hours", _kl_in("index_worker_timeout", 3600)),
    ("indexing.md", r"\*\*First index time limit\*\* \| (\d+) hours", _kl_in("index_first_timeout", 3600)),
    ("indexing.md", r"unreachable repository after\*\* \| (\d+) minutes", _kl_in("index_wait_unreachable", 60)),
    ("indexing.md", r"makes no progress after\*\* \| (\d+) minutes", _kl_in("index_stall_timeout", 60)),
    ("settings.md", r"Once a day at (\d+):(\d+)", _index_time),
    ("settings.md", r"\*\*Quick refresh\*\* \| (\d+) hours?", _kl_in("light_interval", 3600)),
    ("settings.md", r"\*\*Dashboard auto-refresh\*\* \| (\d+) minutes", _kl_in("dashboard_refresh", 60)),
    ("settings.md", r"\*\*Dashboard rebuild interval\*\* \| (\d+) hours", _kl_in("dashboard_interval", 3600)),
    ("disk-scanning.md", r"\*\*Scan time limit\*\* \| (\d+) seconds", _kl("disk_scan_budget")),
    ("disk-scanning.md", r"\*\*Scan time limit\*\* \((\d+) seconds by default\)", _kl("disk_scan_budget")),
    ("disk-scanning.md", r"\*\*Excluded-folder tolerance\*\* \| (\d+)%", _kl_in("excluded_tolerance", 0.01)),
    ("disk-scanning.md", r"Automatic uses up to (\d+)", _scanner_threads),
    ("settings.md", r"\*\*Indexing time limit\*\* \| (\d+) hours", _kl_in("index_worker_timeout", 3600)),
    ("settings.md", r"\*\*First index time limit\*\* \| (\d+) hours", _kl_in("index_first_timeout", 3600)),
    ("settings.md", r"unreachable repository after\*\* \| (\d+) minutes", _kl_in("index_wait_unreachable", 60)),
    ("settings.md", r"makes no progress after\*\* \| (\d+) minutes", _kl_in("index_stall_timeout", 60)),
    ("settings.md", r"\*\*Update the index after deletes\*\* \| (\d+) minutes", _kl_in("reindex_after_delete_delay", 60)),
    ("settings.md", r"\*\*Most files in one delete plan\*\* \| (\d+)", _kl("remove_plan_cap")),
    ("settings.md", r"\*\*Retry after a failed run\*\* \| (\d+) minutes", _kl_in("index_retry_delay", 60)),
    ("settings.md", r"\*\*Retries per scheduled slot\*\* \| (\d+)", _kl("index_max_retries")),
    ("settings.md", r"ahead of time\*\* \| (\d+) minutes", _kl_in("folder_prewarm_budget", 60)),
    ("settings.md", r"\*\*Quiet period after startup\*\* \| (\d+) minutes", _kl_in("startup_delay", 60)),
    ("settings.md", r"\*\*Ranked files\*\* \| (\d+)", _kl("rank_count")),
    ("settings.md", r"\*\*Ports\*\* \| (\d+) and (\d+)", _ports),
    ("settings.md", r"\*\*Log cleanup\*\* \| (\d+) days", _kl("log_cleanup_days")),
    ("settings.md", r"\*\*Task history\*\* \| (\d+) days", _kl("task_history_days")),
    ("delete-files.md", r"A plan holds at most (\d+) files", _kl("remove_plan_cap")),
    ("delete-files.md", r"updates its index (\d+) minutes after the last delete", _kl_in("reindex_after_delete_delay", 60)),
    ("guide-accidental-backup.md", r"A plan holds at most (\d+) files by default", _kl("remove_plan_cap")),
    ("restore.md", r"With more than ([\d,]+) files, the preview checks the first ([\d,]+)",
     lambda: (_restore_preview_cap(), _restore_preview_cap())),
    ("hardware.md", r"to an (eighth) of the computer's memory, between (\d+) MB and (\d+) GB", _memory_range),
    ("hardware.md", r"up to (\d+) processor cores", _worker_threads),
    ("hardware.md", r"it waits (\d+) minutes by default", _kl_in("startup_delay", 60)),
    ("faq.md", r"for at most (\d+) seconds by default", _kl("disk_scan_budget")),
    ("file-panel.md", r"up to the newest (\d+)\)", _size_chart_versions),
]
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "twelve": 12, "24": 24,
         "once": 1, "eighth": 8}


def _number(s: str) -> int:
    s = s.replace(",", "")
    return int(s) if s.isdigit() else WORDS[s.lower()]


@pytest.mark.parametrize("page,pattern,value", DEFAULTS, ids=[f"{p}:{v.__name__}" for p, _, v in DEFAULTS])
def test_stated_defaults_match_the_code(page, pattern, value):
    m = re.search(pattern, PAGES[page.removesuffix(".md")])
    assert m, f"{page} no longer says {pattern!r}: update the row in DEFAULTS"
    want = value()
    want = want if isinstance(want, tuple) else (want,)
    assert tuple(_number(g) for g in m.groups()) == tuple(want), f"{page}: docs say {m.group(0)!r}, the code has {want}"


def test_docker_page_shows_the_compose_example_as_published():
    """docker.md shows compose.example.yml inline (the site sync publishes the file); they must not drift."""
    m = re.search(r"^ *```yaml\n(.*?)^ *```", PAGES["docker"], re.S | re.M)
    assert m, "docker.md no longer has the compose example"
    shown = "\n".join(line[3:] if line.startswith("   ") else line for line in m.group(1).splitlines())
    example = (ROOT / "packaging" / "docker" / "compose.example.yml").read_text(encoding="utf-8")
    body = lambda text: text[text.index("services:"):].strip()  # header comments may differ
    assert body(shown) == body(example), "docker.md's compose block differs from packaging/docker/compose.example.yml"
