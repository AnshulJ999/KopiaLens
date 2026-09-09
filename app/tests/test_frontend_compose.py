"""The / route composes index.html from partials.

Pins: no include marker may survive composition, the Alpine mount point and the entry
script tags must be present, and the stamp must be a real epoch value (the split
made every script tag stamped; a marker or missing tag here means a broken shell).
"""

from app import main as m


def test_compose_resolves_every_marker():
    html = m._compose_html()
    assert "{%" not in html and "{{" not in html, "a template tag survived rendering"
    assert 'x-data="kopiaLens()"' in html
    assert '<script src="/static/js/util.js' in html
    assert '<script src="/static/js/app.js' in html
    assert '/static/css/tokens.css?v=' in html
    assert '/static/css/app.css?v=' in html


def test_asset_version_is_an_epoch_stamp():
    v = m._asset_version()
    assert v.isdigit() and len(v) == 10


def test_compose_stamps_appearance_onto_html(monkeypatch):
    monkeypatch.setattr(m, "load_settings", lambda: {"kopialens": {"theme": "midnight", "density": "compact"}})
    html = m._compose_html()
    assert 'data-theme="midnight"' in html
    assert 'data-density="compact"' in html


def test_compose_falls_back_on_unknown_appearance_values(monkeypatch):
    # settings.json is hand-editable and the template is autoescape=False, so an unknown
    # value must never reach the page (validated server-side, not escaped).
    monkeypatch.setattr(m, "load_settings", lambda: {"kopialens": {"theme": 'x"><script>alert(1)</script>', "density": "bogus"}})
    html = m._compose_html()
    assert 'data-theme="system"' in html and 'data-density="normal"' in html
    assert "alert(1)" not in html


def test_compose_stamps_defaults_when_appearance_absent(monkeypatch):
    monkeypatch.setattr(m, "load_settings", lambda: {"kopialens": {}})
    html = m._compose_html()
    assert 'data-theme="system"' in html and 'data-density="normal"' in html
