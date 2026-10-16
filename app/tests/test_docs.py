"""The user docs (docs/*.md): links and "?" buttons resolve, no internal words leak, and the
defaults the pages state match the code (kopialens-docs-guide.md, sections 5, 9 and 10)."""

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
    ("glossary.md", r"more than (\d+) minutes after KopiaLens started", _setup_minutes),
    ("indexing.md", r"\((\d+) by default, up to (\d+)", _keep_versions),
    ("api-tokens.md", r"expires after (\w+) hours?", _approval_hours),
    ("api.md", r"It expires after (\w+) hours?", _approval_hours),
    ("api.md", r"It's valid for (\w+) hours?", _plan_hours),
    ("cli.md", r"is valid for (\w+) hours?", _plan_hours),
    ("licensing.md", r"for (\d+) more days after the end date", _licence_grace_days),
]
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "twelve": 12, "24": 24}


def _number(s: str) -> int:
    return int(s) if s.isdigit() else WORDS[s.lower()]


@pytest.mark.parametrize("page,pattern,value", DEFAULTS, ids=[f"{p}:{v.__name__}" for p, _, v in DEFAULTS])
def test_stated_defaults_match_the_code(page, pattern, value):
    m = re.search(pattern, PAGES[page.removesuffix(".md")])
    assert m, f"{page} no longer says {pattern!r}: update the row in DEFAULTS"
    want = value()
    want = want if isinstance(want, tuple) else (want,)
    assert tuple(_number(g) for g in m.groups()) == tuple(want), f"{page}: docs say {m.group(0)!r}, the code has {want}"
