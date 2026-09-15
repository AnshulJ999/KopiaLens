"""Source-level pins for the Policies tab wiring (KL23 frontend).

The composed shell must carry the tab (an include or script tag dropped in a merge silently
kills the tab), and every pol* symbol the partial binds must exist in the JS sources - the
compose test cannot see identifiers, and a binding with no method behind it only fails at
click time (the gap class: two agents, one file, nobody owning the wiring).
"""

import re
from pathlib import Path

from app import main as m

FRONTEND = Path(m.__file__).resolve().parent.parent / "frontend"


def test_compose_carries_the_policies_tab():
    html = m._compose_html()
    assert "active==='policies'" in html, "policies tab markup missing from the composed shell"
    assert '<script src="/static/js/policies.js' in html, "policies.js script tag missing"


def test_app_declares_policies_part_once():
    app = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert app.count("policiesPart") >= 3, "policiesPart must be listed, guarded and assigned"
    assert '{ id: "policies", label: "Policies" }' in app, "tabs array entry missing"


def test_compose_carries_the_exclusion_dialog():
    html = m._compose_html()
    assert "showExclude" in html, "exclusion dialog markup missing from the composed shell"
    assert '<script src="/static/js/exclude.js' in html, "exclude.js script tag missing"


def test_app_declares_exclude_part_once():
    app = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert app.count("excludePart") >= 3, "excludePart must be listed, guarded and assigned"


def test_every_pol_binding_in_the_partial_exists_in_js():
    html = "\n".join((FRONTEND / "partials" / p).read_text(encoding="utf-8")
                     for p in ("tabs/policies.html", "policyedit.html", "jobs.html"))
    js = "\n".join((FRONTEND / "js" / f).read_text(encoding="utf-8")
                   for f in ("policies.js", "policyedit.js", "app.js"))
    tokens = set(re.findall(r"\b(?:pol|showPol|ed)[A-Z]\w*", html))
    assert tokens, "no policy bindings found - the partial stopped binding anything"
    missing = sorted(t for t in tokens if t not in js)
    assert not missing, f"partial binds symbols no JS file defines: {missing}"


def test_every_ex_binding_in_the_dialog_exists_in_js():
    html = (FRONTEND / "partials" / "exclude.html").read_text(encoding="utf-8")
    js = (FRONTEND / "js" / "exclude.js").read_text(encoding="utf-8") + "\n" + \
         (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    tokens = set(re.findall(r"\bex[A-Z]\w*", html)) | {"showExclude"}
    assert tokens, "no exclusion-dialog bindings found"
    missing = sorted(t for t in tokens if t not in js)
    assert not missing, f"dialog binds symbols no JS file defines: {missing}"


def test_detail_panels_carry_the_exclude_entry_points():
    html = (FRONTEND / "partials" / "detail.html").read_text(encoding="utf-8")
    assert html.count("openExclude(") >= 4, "file + folder panels lost their exclude entries"
    assert "folder_exclude()" in html, "the folder panel exclude section macro vanished"
