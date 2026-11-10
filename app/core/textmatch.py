"""LIKE matching for a user's typed text, shared by the Kopia and Backblaze searches.

What the person types matches literally, except `*`, which stands for any run of characters:
`report*2024` finds `report-final-2024.pdf`. That replaces SQL LIKE's own `_` (one character) and
`%` (anything), which used to widen a search by accident (searching `my_file` also found
`myXfile`, and a lone `%` returned every file).

Mac names may be stored in either Unicode form (NFC or NFD), so a caller asks for every form and
matches a path against any of them.
"""

from __future__ import annotations

import unicodedata

LIKE_ESCAPE = "!"
ESCAPE_SQL = " ESCAPE '" + LIKE_ESCAPE + "'"


def like_literal(value: str) -> str:
    """Quote a string so LIKE matches it literally (pair it with ESCAPE_SQL)."""
    for ch in (LIKE_ESCAPE, "%", "_"):
        value = value.replace(ch, LIKE_ESCAPE + ch)
    return value


def like_contains(query: str) -> str:
    """A contains-pattern for one search term: `*` becomes `%`, everything else stays literal."""
    return "%" + "%".join(like_literal(part) for part in query.split("*")) + "%"


def search_patterns(query: str) -> list[str]:
    """One `%term%` pattern per unicode form of the query (NFC, NFD), `*` handled as a wildcard."""
    forms = dict.fromkeys(unicodedata.normalize(f, query) for f in ("NFC", "NFD"))
    return [like_contains(f) for f in forms]
