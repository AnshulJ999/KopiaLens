"""User-facing copy must not leak item codes or names."""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
APP = Path(__file__).resolve().parents[1]

LEAK = re.compile(r"\b(?:KL|REL|BZ|CH|PERF|STO)\d+\b|\bAnshul\b")


def _js_strings(src: str) -> list[str]:
    """String and template literals of a JS source, comments skipped."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i + 1:j])
            i = j + 1
        else:
            i += 1
    return out


def _html_text(src: str) -> str:
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"\{#.*?#\}", "", src, flags=re.S)


def test_js_strings_have_no_item_codes():
    hits = []
    for f in sorted((FRONTEND / "js").rglob("*.js")):
        for s in _js_strings(f.read_text(encoding="utf-8")):
            if LEAK.search(s):
                hits.append(f"{f.name}: {s[:80]}")
    assert not hits, hits


def test_partials_have_no_item_codes():
    hits = []
    for f in sorted((FRONTEND / "partials").rglob("*.html")):
        for ln in _html_text(f.read_text(encoding="utf-8")).splitlines():
            if LEAK.search(ln):
                hits.append(f"{f.name}: {ln.strip()[:80]}")
    assert not hits, hits


def test_backend_notes_have_no_item_codes():
    """Strings the API sends for display (note/detail/reason values) stay clean too."""
    pat = re.compile(r"""["'](?:note|detail|reason|message)["']\s*:\s*f?["']([^"']*)["']""")
    hits = []
    for f in sorted((APP / "api").glob("*.py")):
        for m in pat.finditer(f.read_text(encoding="utf-8")):
            if LEAK.search(m.group(1)):
                hits.append(f"{f.name}: {m.group(1)[:80]}")
    assert not hits, hits


def test_scanner_skips_comments():
    assert _js_strings('// "AA1"\nconst a = "x"; /* "AA2" */') == ["x"]
