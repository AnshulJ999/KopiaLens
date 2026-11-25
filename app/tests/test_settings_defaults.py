"""Defaults every fresh install inherits."""

import re

from app.settings import DEFAULT_SETTINGS

_ABS_PATH = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/(home|mnt|root|Users)/)")
# A vendor's standard install location is the same on every machine (Backblaze's bzdata).
_STANDARD = re.compile(r"^C:[\\/](ProgramData|Program Files( \(x86\))?)[\\/]", re.IGNORECASE)


def _strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def test_defaults_hold_no_machine_specific_paths():
    """A default path from the developer's machine reaches every new install as a real setting."""
    offenders = [s for s in _strings(DEFAULT_SETTINGS) if _ABS_PATH.match(s) and not _STANDARD.match(s)]
    assert offenders == []
