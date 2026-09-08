"""The / route composes index.html from partials.

Pins: no include marker may survive composition, the Alpine mount point and the entry
script tags must be present, and the stamp must be a real epoch value (the split
made every script tag stamped; a marker or missing tag here means a broken shell).
"""

from app import main as m


def test_compose_resolves_every_marker():
    html = m._compose_html()
    assert "<!-- include:" not in html, "a partial include was not expanded"
    assert 'x-data="kopiaLens()"' in html
    assert '<script src="/static/js/util.js' in html
    assert '<script src="/static/js/app.js' in html


def test_asset_version_is_an_epoch_stamp():
    v = m._asset_version()
    assert v.isdigit() and len(v) == 10
