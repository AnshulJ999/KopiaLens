"""ignorematch: the wcmatch port must agree with kopia's own behaviour vectors.

The case lists below are PORTED from kopia's ``internal/wcmatch/wcmatch_test.go`` (fetched
2026-09-12) - the case-sensitive column, which is what kopia ships with (``IgnoreCase(false)``).
Paths keep kopia's test convention: a leading ``/`` is part of the text, not an anchor. The
regex fast path is cross-validated against the ported matcher on every non-negated case plus a
real-policy pattern corpus.
"""

from __future__ import annotations

import re

import pytest

from app.core.ignorematch import compile_pattern, file_match_regex

# (pattern, path, is_dir, expected) - ported from TestMatch (case-sensitive column) and
# TestMatchWithBaseDir. Base-dir cases carry a 4th element before expected: the base dir.
# NOTE: in these vectors a negated rule "matching" a path means Match() returned true, which the
# ignorefs state machine interprets as re-include; the raw Match semantics are what we pin here.
WC_CASES = [
    # Basic
    ("", "", False, True),
    ("*", "", False, True),
    ("**", "", False, True),
    ("b", "a", False, False),
    ("*.*", "foo.txt", False, True),
    ("foo\\.txt", "foo.txt", False, True),
    ("*/", ".git/", True, True),
    # Sequences
    ("ab[cd]", "abc", False, True),
    ("ab[!de]", "abc", False, True),
    ("[\\\\]", "\\", False, True),
    ("[!\\\\]", "a", False, True),
    ("[!\\\\]", "\\", False, False),
    ("[-abc]", "-", False, True),
    ("[abc-]", "-", False, True),
    ("[[]", "[", False, True),
    ("[a-z]", "q", False, True),
    ("[a-\\z]", "q", False, True),
    ("[a-d]", "e", False, False),
    ("[a\\-c]", "-", False, True),
    ("[a\\-c]", "b", False, False),
    ("[[:]", ":", False, True),
    ("[[:ab]", ":", False, True),
    ("[[:ab]", "[", False, True),
    ("[[:ab]", "b", False, True),
    ("[[:ab]", "c", False, False),
    ("[![:digit:]ab]", "a", False, False),
    ("[![:digit:]ab]", "3", False, False),
    ("[![:digit:]ab]", "c", False, True),
    # Case (case-sensitive matching)
    ("abc", "abc", False, True),
    ("abc", "AbC", False, False),
    ("AbC", "abc", False, False),
    ("AbC", "AbC", False, True),
    ("[A-F]", "C", False, True),
    ("[A-F]", "c", False, False),
    ("[a-f]", "c", False, True),
    ("[a-f]", "C", False, False),
    # Basic wildcards
    ("ab*", "abcd", False, True),
    ("ab*cd", "abcd", False, True),
    ("ab*cd", "abxxxcd", False, True),
    ("ab***cd", "abcd", False, True),
    ("ab***cd", "abxxxcd", False, True),
    ("ab*", "ab/cd", False, False),
    ("ab**", "ab/cd", False, False),
    ("?*?", "abc", False, True),
    ("???*", "abc", False, True),
    ("*???", "abc", False, True),
    ("???", "abc", False, True),
    ("*", "abc", False, True),
    ("??", "a", False, False),
    # Recursive wildcards
    ("**", "foo", False, True),
    ("**", "foo/bar", False, True),
    ("/a/**/b/foo.txt", "/a/b/c/d/foo.txt", False, False),
    ("/a/**/b/foo.txt", "/a/b/c/b/foo.txt", False, True),
    ("/a/**/b/foo.txt", "/a/b/c/b/foo.txt/other", False, False),
    ("/a/**/b/foo.txt", "/a/b/c/b/foo.txt/e/b/foo.txt", False, True),
    ("foo/**", "/foo/bar/a/b/c.txt", False, True),
    ("/foo/**/bar/**/p.txt", "/foo/bar/p.txt", False, True),
    ("/foo/**/bar/**/p.txt", "/foo/x/bar/p.txt", False, True),
    ("/foo/**/bar/**/p.txt", "/foo/x/x/bar/x/y/p.txt", False, True),
    ("**/bar/a.txt", "/foo/x/x/bar/a.txt", False, True),
    # Rooted / unrooted
    ("bar/a.txt", "/foo/bar/a.txt", False, False),
    ("/bar/a.txt", "/foo/bar/a.txt", False, False),
    ("bar/a.txt", "/bar/a.txt", False, True),
    ("*.txt", "/foo/bar/a.txt", False, True),
    ("/*.txt", "/foo/bar/a.txt", False, False),
    ("**/*.txt", "/foo/bar/a.txt", False, True),
    # Directories
    ("foo/", "/foo", False, False),
    ("foo/", "/foo/", True, True),
    ("foo/", "/foo/bar", False, False),
    ("foo/", "/foo/bar/", False, False),
    ("foo/", "/bar/foo", False, False),
    ("foo/", "/bar/foo/", True, True),
    ("foo/**/bar/", "/foo/bar", False, False),
    ("foo/**/bar/", "/foo/bar/", True, True),
    ("foo/**/bar/", "/foo/xx/yy/bar/", True, True),
    ("**/", "/foo", False, False),
    ("**/", "/foo/", True, True),
    # Negated (raw Match semantics)
    ("!foo", "/bar", False, True),
    ("!foo", "foo/", False, False),
    ("!*", "/foo", False, False),
    ("!*/", "/.git/", False, False),
    ("!*/", "/foo/bar/", False, False),
    ("!foo/bar", "/foo/bar", False, False),
    ("!foo/bar", "/car/foo/bar", False, True),
    ("!foo/bar/", "/foo/bar/", False, False),
    ("!foo/bar/", "/car/foo/bar/", False, True),
    # Whitespace trimming
    ("  \tfoo", "foo", False, True),
    ("  foo  ", "foo", False, True),
    (" foo\\ ", "/foo ", False, True),
    ("/users/user1/logs", "/users/user1/logs", False, True),
    ("/users/**/logs", "/users/user1/logs", False, True),
    ("/users/user?/logs", "/users/user1/logs", False, True),
    ("/users/user*/logs", "/users/user1/logs", False, True),
    ("/users/*/logs", "/users/user1/logs", False, True),
]

# (pattern, base_dir, path, expected) - ported from TestMatchWithBaseDir. The harness treats a
# trailing-slash path as a directory; none of these have one, so is_dir is False throughout.
BASE_CASES = [
    ("", "/base/", "", True),
    ("", "/base", "", True),
    ("*", "/base/", "/foo", False),
    ("*", "/base", "/foo", False),
    ("**", "/base/", "/foo", False),
    ("**", "/base", "/foo", False),
    ("*", "/base", "/base/foo", True),
    ("*", "/base/", "/base/foo", True),
    ("**", "/base", "/base/foo", True),
    ("**", "/base/", "/base/foo", True),
    ("*.txt", "/base/", "/base/foo.txt", True),
    ("*.txt", "/base/", "/other/foo.txt", False),
    ("/src/file.txt", "/base", "/base/src/file.txt", True),
    ("/src/file.txt", "/base", "/other/src/file.txt", False),
    ("**/src/file.txt", "/base", "/base/foo/src/file.txt", True),
    ("src/file.txt", "/base", "/base/foo/src/file.txt", False),
    ("src/file.txt", "/base", "/other/foo/src/file.txt", False),
    ("file.txt", "/base", "/base/foo/src/file.txt", True),
    ("file.txt", "/base", "/other/foo/src/file.txt", False),
]

ERROR_PATTERNS = ["[a", "[a-", "\\", "[\\", "[a-\\", "[[:alnum", "[[:foobar:]]"]


def _is_dir(path: str) -> bool:
    if len(path) > 1 and path[-1] == "/":
        return True
    return False


def _strip_slash(path: str) -> str:
    if _is_dir(path):
        return path[:-1]
    return path


@pytest.mark.parametrize("pattern,path,is_dir,expected", WC_CASES, ids=lambda v: str(v)[:24])
def test_ported_vectors(pattern, path, is_dir, expected):
    is_dir = is_dir or _is_dir(path)
    path = _strip_slash(path)
    assert compile_pattern(pattern).match(path, is_dir) is expected


@pytest.mark.parametrize("pattern,base,path,expected", BASE_CASES, ids=lambda v: str(v)[:24])
def test_ported_base_dir_vectors(pattern, base, path, expected):
    is_dir = _is_dir(path)
    path = _strip_slash(path)
    assert compile_pattern(pattern, base_dir=base).match(path, is_dir) is expected


@pytest.mark.parametrize("pattern", ERROR_PATTERNS)
def test_error_patterns_raise(pattern):
    with pytest.raises(ValueError):
        compile_pattern(pattern)


def test_dir_only_file_named_like_the_dir_does_not_match():
    """The kopia semantics that matters for pricing: pattern `Excluded/` matches the DIRECTORY
    itself, so everything under it is pruned by the ancestor walk - but the raw matcher returns
    False for any FILE (dir-only patterns never match files directly)."""
    pat = compile_pattern("Excluded/")
    assert pat.match("Excluded", is_dir=True)
    assert pat.match("a/Excluded", is_dir=True)
    assert not pat.match("Excluded", is_dir=False)
    assert not pat.match("Excluded/x.txt", is_dir=False)  # kept out via the ANCESTOR, not directly
    assert _kept_out_by_matcher("Excluded/", "/Excluded/x.txt")
    assert not _kept_out_by_matcher("Excluded/", "/Excluded")  # a FILE named Excluded stays


def test_ancestor_pruning_is_part_of_kept_out():
    pat = compile_pattern("/Program Files/**")
    assert pat.match("/Program Files/game/data.pak", is_dir=False)
    assert not pat.match("/Program Files", is_dir=True)  # a/** does not match a itself
    assert not pat.match("/program Files/game/data.pak", is_dir=False)  # case-sensitive
    assert not pat.match("/Other/game/data.pak", is_dir=False)


# ---- regex fast path cross-validation --------------------------------------------------

# A corpus of realistic relative paths (forward-slash form) spanning the real policy shapes.
_PATHS = [
    "a.tmp", "x/a.tmp", "deep/nest/a.tmp", "A.TMP",
    "node_modules", "x/node_modules", "x/node_modules/y/z.js",
    "Program Files/App/game.dll", "Program Files", "program Files/App/game.dll",
    "$RECYCLE.BIN", "x/$RECYCLE.BIN/f", "pagefile.sys", "x/pagefile.sys",
    "Users/a/AppData/Local/Temp/f.txt", "Users/a/AppData/Local/Temp",
    "Games/Ascendara/saves/g1.sav", "Games/Ascendara/save/g1.sav",
    "Excluded", "x/Excluded", "x/Excluded/y.txt", "Excluded/z.txt",
    "jellyfin/x/transcodes/v.ts", "jellyfin/transcodes/v.ts",
    "log.txt", "logs/x.log", "x/logs/y.log", "Cache_Data/f", "x/Cache_Data/f",
    "foo", "foo/bar", "foo/bar/baz.txt", "bar/a.txt", "x/bar/a.txt",
    "hiberfil.sys", "swapfile.sys", "file.txt", "x/file.txt", "x/y/file.txt",
    "src/file.txt", "foo/src/file.txt", "base/foo.txt", "other/foo.txt",
    ".git", "x/.git", "x/.git/objects/ab", ".git/config",
    "sp ace/x.txt", "pl+us/x.txt", "pa(r)en/x.txt", "sq[uare]/x.txt",
    "a-b/x.txt", "a.b.c/x.txt", "up/x/UP/x.txt",
]

# Real policy shapes (from the live repo1 export) that the regex path must handle.
_REAL_PATTERNS = [
    "*.tmp", "*.log", "*.cache", "*.duckdb", "*.vhdx", "*.safetensors",
    "node_modules", "node_modules/**", "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "$RECYCLE.BIN", "$$WINDOWS.~BT/**", "$$logfile",
    "**/AMD/*Cache/**", "**/AppData/Local/Zoom", "**/NTUSER.DAT", "**/ntuser.dat.LOG*",
    "/$Recycle.Bin/**", "/.bzvol/**", "/AppData Backup/**", "/Games/**",
    "/Program Files (x86)/**", "/Program Files/**", "/ProgramData/*/Cache/**",
    "/ProgramData/*/temp/**", "/ProgramData/Microsoft/Diagnosis/**",
    "/PS3 Emulator/**/cache/**", "/System Volume Information/**", "/Temp/**",
    "/Users/**/AppData/Local/Temp/**", "/Users/*/AppData/Local/*-updater/**",
    "/Users/*/AppData/Local/*.log", "/Users/*/AppData/Local/*/Cache/**",
    "/Users/*/AppData/Local/Google/Chrome/User Data/**/Cache/**",
    "/Users/*/AppData/Local/Jellyfin?Media?Player/**",
    "/Windows/**", "/WUDownloadCache/**", "/jellyfin/**/log/**",
    "/openssh/**", "/stremio-cache/**",
    "ProgramData/*/*.key.*", "ProgramData/*/host*.json", "ProgramData/Backblaze/bzdata/bzlogs/**/*.zip",
    "AMD", "BitMagnet", "Cache", "Code Cache", "Excluded/", "OneDriveTemp/",
    "Everything*.db", "PlayniteBackup-*.zip", "GoogleDriveMirror", "venv",
    "ProgramData/Tailscale", "ProgramData/Tailscale/**",
]


def _kept_out_by_matcher(pattern: str, path: str) -> bool:
    """The walk-level predicate on a SLASHED relative path (leading slash, as kopia feeds the
    matcher): the file matches, or one of its ancestor directories does."""
    pat = compile_pattern(pattern)
    parts = path[1:].split("/")
    for i in range(1, len(parts)):
        if pat.match("/" + "/".join(parts[:i]), is_dir=True):
            return True
    return pat.match(path, is_dir=False)


@pytest.mark.parametrize("pattern", _REAL_PATTERNS)
def test_regex_fast_path_agrees_with_the_matcher(pattern):
    rx = file_match_regex(pattern)
    assert rx is not None, f"pattern {pattern!r} should be regex-translatable"
    compiled = re.compile(rx)
    for rel in _PATHS:
        path = "/" + rel
        want = _kept_out_by_matcher(pattern, path)
        got = compiled.fullmatch(path) is not None
        assert got == want, f"pattern {pattern!r} path {path!r}: regex={got} matcher={want}"


def test_regex_fast_path_declines_negated_patterns():
    assert file_match_regex("!foo") is None


def test_regex_fast_path_declines_posix_classes():
    assert file_match_regex("[[:alpha:]]*") is None


def test_invalid_pattern_is_reported_not_matched():
    with pytest.raises(ValueError):
        compile_pattern("bad\\")
