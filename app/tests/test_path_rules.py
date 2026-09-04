"""Per-source path rules.

The invariant these pin: separator and case sensitivity come from the SOURCE, not the host.
A Linux box analysing Windows-sourced snapshots must still read those paths as backslash-
separated and case-insensitive, so nothing here may ever consult os.sep or os.name.
"""

from __future__ import annotations

import pytest

from app.core import paths

SEP = chr(92)
WIN_SRC = "Anshul@pc:C:" + SEP + "Users"
UNC_SRC = "Anshul@pc:" + SEP + SEP + "nas" + SEP + "media"
POSIX_SRC = "root@server:/var/lib/data"


@pytest.fixture(autouse=True)
def _clean_overrides():
    paths.configure(None)
    yield
    paths.configure(None)


def test_windows_source_is_backslash_and_case_insensitive():
    assert paths.sep(WIN_SRC) == SEP
    assert paths.case_sensitive(WIN_SRC) is False


def test_unc_source_is_treated_as_windows():
    assert paths.sep(UNC_SRC) == SEP
    assert paths.case_sensitive(UNC_SRC) is False


def test_posix_source_is_forward_slash_and_case_sensitive():
    assert paths.sep(POSIX_SRC) == "/"
    assert paths.case_sensitive(POSIX_SRC) is True


def test_identity_folds_case_only_on_a_case_insensitive_source():
    """The correctness bug this whole model exists for: on Linux, Photo.jpg and photo.jpg are
    two files, and folding them merges two paths' first_seen into one."""
    assert paths.ident(WIN_SRC, "C:" + SEP + "A" + SEP + "Photo.JPG") == ("c:" + SEP + "a" + SEP + "photo.jpg")
    assert paths.ident(POSIX_SRC, "/var/Photo.JPG") == "/var/Photo.JPG"


def test_ident_sql_matches_ident_semantics():
    """The SQL and Python twins must agree, or the backfill writes rows the rewrite cannot find."""
    assert paths.ident_sql(WIN_SRC) == "lower(full_path)"
    assert paths.ident_sql(POSIX_SRC) == "full_path"
    assert paths.ident_sql(POSIX_SRC, "t.full_path") == "t.full_path"


def test_join_uses_the_source_separator():
    assert paths.join(WIN_SRC, "C:" + SEP + "Users", "a" + SEP + "b.txt") == \
        "C:" + SEP + "Users" + SEP + "a" + SEP + "b.txt"
    assert paths.join(POSIX_SRC, "/var/lib", "a/b.txt") == "/var/lib/a/b.txt"


def test_join_does_not_double_the_separator_on_a_root_that_ends_in_one():
    assert paths.join(WIN_SRC, "C:" + SEP, "f.txt") == "C:" + SEP + "f.txt"
    assert paths.join(POSIX_SRC, "/", "f.txt") == "/f.txt"


def test_join_of_an_empty_relative_path_is_the_root():
    assert paths.join(WIN_SRC, "C:" + SEP + "Users", "") == "C:" + SEP + "Users"


def test_an_override_beats_detection():
    """macOS is POSIX-shaped but case-insensitive, and no path shape can reveal that - the
    override is the only way to express it."""
    paths.configure({POSIX_SRC: {"case_sensitive": False}})
    assert paths.case_sensitive(POSIX_SRC) is False
    assert paths.sep(POSIX_SRC) == "/"          # untouched keys keep their detected value
    assert paths.ident(POSIX_SRC, "/var/A.TXT") == "/var/a.txt"


def test_an_override_for_another_source_does_not_leak():
    paths.configure({POSIX_SRC: {"case_sensitive": False}})
    assert paths.case_sensitive(WIN_SRC) is False
    assert paths.sep(WIN_SRC) == SEP


def test_unknown_keys_in_an_override_are_ignored():
    paths.configure({WIN_SRC: {"sep": "/", "nonsense": True}})
    assert paths.sep(WIN_SRC) == "/"
    assert "nonsense" not in paths.rules(WIN_SRC)
