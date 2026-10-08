"""Every Settings control saves through a KL_FIELDS row.

``saveKl(key)`` looks the key up in KL_FIELDS (frontend/js/settings.js) and saves nothing when the
row is missing, so a control added without its row looks fine and silently drops every edit.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _text(pattern: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(FRONTEND.glob(pattern)))


def _field_rows() -> set[str]:
    js = (FRONTEND / "js" / "settings.js").read_text(encoding="utf-8")
    block = js[js.index("KL_FIELDS: {"):]
    return set(re.findall(r"^\s+(\w+): \{", block[:block.index("\n    },")], re.M))


def test_every_settings_control_has_a_field_row():
    used = set(re.findall(r"saveKl\('(\w+)'\)", _text("partials/**/*.html") + _text("js/*.js")))
    assert used, "no saveKl controls found - the pattern no longer matches the markup"
    missing = sorted(used - _field_rows())
    assert not missing, f"controls with no KL_FIELDS row (their edits save nothing): {missing}"
