"""Kopia-compatible ignore-pattern matching.

Pricing a policy's exclusion rules against the index requires the SAME matching kopia performs at
snapshot time. Kopia compiles ``files.ignore`` rules with its vendored ``internal/wcmatch``
(``fs/ignorefs/ignorefs.go``) as:

- paths are RELATIVE to the walk root with ``/`` separators;
- ``IgnoreCase(false)`` - matching is CASE-SENSITIVE on every OS;
- ``BaseDir(dirPath)`` - rules are anchored to the directory whose policy defines them; an
  unanchored pattern under a base dir gets an implicit ``**/`` after the base.

Paths fed to the matcher carry a LEADING SLASH: kopia builds them as ``d.relativePath + "/" +
name`` with the walk root's relativePath ``"."``, then ``trimLeadingCurrentDir`` turns ``./name``
into ``/name`` (this is why kopia's own test vectors use leading-slash paths, and why a rooted
pattern's leading ``/`` matches anything). Pricing must match against ``/`` + relative-path.

This module is a direct port of that Go code (token model + ``doMatch``), so the pricing numbers
mean what kopia will actually do. ``file_match_regex`` additionally translates a compiled pattern
into a RE2-safe regex for DuckDB ``regexp_full_match`` (the SQL fast path); it returns ``None``
for the rare shapes the translation does not cover (negation, POSIX classes) and the caller falls
back to the ported matcher. Tests cross-validate the two paths.

Semantics worth remembering (all verified against the Go source, 2026-09-12):
- an unanchored pattern with no ``/`` matches at ANY depth; with a middle ``/`` it becomes rooted;
- ``a/**`` matches only paths UNDER ``a`` - not ``a`` itself;
- ``a**b`` is NOT a double star: contiguous stars after a non-``/`` char collapse to one ``*``;
- a trailing ``/`` marks the pattern directory-only (a matched directory prunes its whole subtree);
- ``!pattern`` negates (re-includes) - the pricing caller treats negated patterns conservatively.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Sentinel returned by the rune scanner at end-of-text, mirroring Go's 0 rune.
_EOS = ""

WC_MATCH, WC_NO_MATCH, WC_ABORT_ALL, WC_ABORT_TO_DOUBLESTAR = 0, 1, 2, 3


# ---- token model -----------------------------------------------------------------------

@dataclass(frozen=True)
class _AnyChar:
    """``?`` - one non-separator character."""


@dataclass(frozen=True)
class _DirSep:
    """``/``."""


@dataclass(frozen=True)
class _Star:
    double: bool


@dataclass(frozen=True)
class _Rune:
    ch: str


@dataclass(frozen=True)
class _SeqItemRune:
    ch: str

    def match(self, ch: str) -> bool:
        return ch == self.ch


@dataclass(frozen=True)
class _SeqItemRange:
    start: str
    end: str

    def match(self, ch: str) -> bool:
        return self.start <= ch <= self.end


@dataclass(frozen=True)
class _SeqItemClass:
    name: str

    def match(self, ch: str) -> bool:
        return _CLASS_MATCHERS[self.name](ch)


@dataclass(frozen=True)
class _Seq:
    items: tuple
    negated: bool


def _cat(ch: str) -> str:
    return unicodedata.category(ch)


# Go unicode predicates, approximated via unicodedata categories.
_CLASS_MATCHERS = {
    "alnum": lambda ch: _cat(ch)[0] in "LN",
    "alpha": lambda ch: _cat(ch)[0] == "L",
    "ascii": lambda ch: ord(ch) <= 127,
    "blank": lambda ch: _cat(ch) == "Zs" or ch == "\t",
    "cntrl": lambda ch: _cat(ch) == "Cc",
    "digit": lambda ch: _cat(ch) == "Nd",
    "graph": lambda ch: _cat(ch)[0] in "LMNPS" or _cat(ch) == "Zs",
    "lower": lambda ch: _cat(ch) == "Ll",
    "print": lambda ch: _cat(ch)[0] in "LMNPS" or ch == " ",
    "punct": lambda ch: _cat(ch)[0] in "PS",
    "space": lambda ch: ch.isspace(),
    "upper": lambda ch: _cat(ch) == "Lu",
    "xdigit": lambda ch: ch in "0123456789abcdefABCDEF",
}


# ---- rune scanner (port of rune_scanner.go) --------------------------------------------

class _RuneScanner:
    __slots__ = ("pos", "text")

    def __init__(self, text: str):
        self.pos = 0
        self.text = text

    def peek(self, index: int) -> str:
        p = self.pos + index
        if 0 <= p < len(self.text):
            return self.text[p]
        return _EOS

    def read(self) -> str:
        c = self.peek(0)
        if c != _EOS:
            self.pos += 1
            return c
        return _EOS

    def eos(self) -> bool:
        return self.pos >= len(self.text)

    def index_of(self, ch: str) -> int:
        """Index of ``ch`` relative to the current position (Go's indexOf on the remainder)."""
        if self.eos():
            return -1
        try:
            return self.text.index(ch, self.pos) - self.pos
        except ValueError:
            return -1


# ---- pattern ---------------------------------------------------------------------------

@dataclass
class WildcardPattern:
    """A compiled ignore rule (port of wcmatch.WildcardMatcher)."""
    pattern: str
    tokens: tuple
    dir_only: bool
    negated: bool
    base_dir: str = ""

    def match(self, text: str, is_dir: bool) -> bool:
        """``text`` is a relative path with ``/`` separators. The walk root itself (empty) is
        never fed to the matcher by kopia; callers must respect that."""
        if self.dir_only and not is_dir:
            return self.negated
        return (_do_match(self.tokens, text) == WC_MATCH) != self.negated


def _is_star(tok) -> bool:
    return isinstance(tok, _Star)


def _is_dir_sep(tok) -> bool:
    return isinstance(tok, _DirSep)


def _do_match(tokens: tuple, text: str) -> int:
    t = _RuneScanner(text)
    tch = _EOS
    pi = 0
    while pi < len(tokens):
        tch = t.peek(0)
        if t.eos() and not _is_star(tokens[pi]):
            return WC_ABORT_ALL
        tok = tokens[pi]
        if isinstance(tok, _Rune):
            if tch != tok.ch:
                return WC_NO_MATCH
        elif isinstance(tok, _DirSep):
            if tch != "/":
                return WC_NO_MATCH
        elif isinstance(tok, _AnyChar):
            if tch == "/":
                return WC_NO_MATCH
        elif isinstance(tok, _Star):
            if pi == len(tokens) - 1:
                # Trailing ** matches everything; trailing * must not cross a separator.
                if not tok.double and "/" in text[t.pos:]:
                    return WC_NO_MATCH
                return WC_MATCH
            if tok.double and pi + 2 < len(tokens) and _is_dir_sep(tokens[pi + 1]) \
                    and _do_match(tokens[pi + 2:], text[t.pos:]) == WC_MATCH:
                return WC_MATCH
            if not tok.double and _is_dir_sep(tokens[pi + 1]):
                # '*' followed by '/': skip to just before the next separator (the loop's
                # read() then consumes it). slash_index is RELATIVE to t.pos, as in Go.
                rel = t.index_of("/")
                if rel == -1:
                    return WC_NO_MATCH
                t.pos += rel - 1
            else:
                while not t.eos():
                    res = _do_match(tokens[pi + 1:], text[t.pos:])
                    if res != WC_NO_MATCH:
                        if not tok.double or res != WC_ABORT_TO_DOUBLESTAR:
                            return res
                    elif not tok.double and tch == "/":
                        return WC_ABORT_TO_DOUBLESTAR
                    tch = t.read()
                return WC_ABORT_ALL
        elif isinstance(tok, _Seq):
            hit = any(item.match(tch) for item in tok.items)
            if hit == tok.negated:
                return WC_NO_MATCH
        pi += 1
        t.read()
    if t.eos():
        return WC_MATCH
    return WC_NO_MATCH


def _parse_seq(p: _RuneScanner, pattern: str) -> _Seq:
    """Port of the '[' case in NewWildcardMatcher."""
    ch = p.read()
    negated_seq = ch == "!"
    if negated_seq:
        ch = p.read()
    items: list = []
    while ch != "]" and ch != _EOS:
        if ch == "\\":
            ch = p.read()
            if ch == _EOS:
                raise ValueError(f"invalid pattern {pattern!r}: end of line found after '\\'")
        if p.peek(0) == "-" and p.peek(1) not in ("]", _EOS):
            p.read()  # consume '-'
            end_ch = p.read()
            if end_ch == "\\":
                end_ch = p.read()
                if end_ch == _EOS:
                    raise ValueError(f"invalid pattern {pattern!r}: end of line found after '\\'")
            items.append(_SeqItemRange(ch, end_ch))
        elif ch == "[" and p.peek(0) == ":":
            closing = p.index_of("]")
            if closing == -1:
                raise ValueError(f"invalid pattern {pattern!r}: unterminated sequence, expected ']'")
            if closing - 1 <= p.pos or p.peek(closing - 1) != ":":
                # Not a POSIX class - treat as a literal '['.
                items.append(_SeqItemRune(ch))
            else:
                cls = p.text[p.pos + 1:p.pos + closing - 1].lower()
                p.pos += closing + 1
                if cls not in _CLASS_MATCHERS:
                    raise ValueError(f"invalid pattern {pattern!r}: unrecognized class [:{cls}:]")
                items.append(_SeqItemClass(cls))
        else:
            items.append(_SeqItemRune(ch))
        ch = p.read()
    if ch != "]":
        raise ValueError(f"invalid pattern {pattern!r}: unterminated sequence, expected ']'")
    return _Seq(tuple(items), negated_seq)


def compile_pattern(pattern: str, base_dir: str = "") -> WildcardPattern:
    """Port of wcmatch.NewWildcardMatcher(rule, IgnoreCase(false), BaseDir(base_dir)).

    ``base_dir`` is the policy directory RELATIVE to the walk root with ``/`` separators
    (empty for a policy defined on the source root itself).
    """
    if len(pattern) > 2 and pattern[len(pattern) - 2] != "\\":
        pattern = pattern.strip()

    base = base_dir
    if len(base) > 1 and base.endswith("/"):
        base = base[:-1]

    p = _RuneScanner(pattern)
    while p.peek(0) != _EOS and p.peek(0).isspace():
        p.read()

    negated = False
    if p.peek(0) == "!":
        negated = True
        p.read()

    is_rooted = p.peek(0) == "/"

    tokens: list = []
    if base != "" and pattern != "":
        for ch in base:
            tokens.append(_DirSep() if ch == "/" else _Rune(ch))
        if not is_rooted:
            tokens.append(_DirSep())

    first_slash = p.index_of("/")
    if not is_rooted and pattern != "**" and pattern != "":
        if first_slash == -1 or first_slash == len(p.text) - 1 - p.pos:
            tokens.append(_Star(True))
            tokens.append(_DirSep())
        elif base == "":
            # An unrooted pattern with a middle slash is rooted, unless a base dir already rooted it.
            tokens.append(_DirSep())

    dir_only = False
    if len(p.text) > 0 and p.text[-1] == "/":
        dir_only = True
        p.text = p.text[:-1]

    ch = p.read()
    while ch != _EOS:
        if ch == "\\":
            if p.eos():
                raise ValueError(f"invalid pattern {pattern!r}: end of line found after '\\'")
            tokens.append(_Rune(p.read()))
        elif ch == "?":
            tokens.append(_AnyChar())
        elif ch == "/":
            tokens.append(_DirSep())
        elif ch == "*":
            if p.peek(0) == "*":
                prev_ch = p.peek(-2)
                while p.peek(0) == "*":
                    p.read()
                if prev_ch == _EOS or (prev_ch == "/" and (p.peek(0) == "/" or p.eos())):
                    tokens.append(_Star(True))
                    ch = p.read()
                    continue
            tokens.append(_Star(False))
        elif ch == "[":
            tokens.append(_parse_seq(p, pattern))
        else:
            tokens.append(_Rune(ch))
        ch = p.read()

    return WildcardPattern(pattern=pattern, tokens=tuple(tokens), dir_only=dir_only,
                           negated=negated, base_dir=base)


# ---- SQL regex translation (fast path) -------------------------------------------------

def _seq_to_class_body(seq: _Seq) -> str | None:
    """Translate a [...] sequence to a regex class body; None when not covered."""
    out = []
    if seq.negated:
        out.append("^")
    for item in seq.items:
        if isinstance(item, _SeqItemRune):
            out.append(_escape_class_char(item.ch))
        elif isinstance(item, _SeqItemRange):
            out.append(f"{_escape_class_char(item.start)}-{_escape_class_char(item.end)}")
        else:
            return None  # POSIX classes: RE2 translation not trusted, use the ported matcher
    return "".join(out)


def _escape_class_char(ch: str) -> str:
    if ch in "\\]^-[":
        return "\\" + ch
    return ch


def tokens_to_regex(tokens: tuple) -> str | None:
    """RE2 regex (for DuckDB ``regexp_full_match``) matching the SAME single paths the token
    matcher matches. Returns None for shapes the translation does not cover."""
    parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if isinstance(tok, _Rune):
            parts.append(re.escape(tok.ch))
        elif isinstance(tok, _DirSep):
            parts.append("/")
        elif isinstance(tok, _AnyChar):
            parts.append("[^/]")
        elif isinstance(tok, _Star):
            if not tok.double:
                parts.append("[^/]*")
            elif i == len(tokens) - 1:
                if i > 0 and not _is_dir_sep(tokens[i - 1]):
                    return None  # unreachable by construction, but stay safe
                parts.append(".*")
            else:
                # A mid-pattern double star is always followed by a separator.
                if not _is_dir_sep(tokens[i + 1]):
                    return None
                parts.append("(?:.*/)?")
                i += 1  # consume the separator
        elif isinstance(tok, _Seq):
            body = _seq_to_class_body(tok)
            if body is None or body == "":
                return None  # POSIX classes / empty class: RE2 not trusted, use the matcher
            parts.append(f"[{body}]")
        i += 1
    return "".join(parts)


def file_match_regex(pattern: str, base_dir: str = "") -> str | None:
    """Regex matching a FILE path (forward-slash form) that the pattern keeps out, counting a
    matched ancestor directory as pruning the file. Returns None when the fast path does not
    apply (negated patterns, POSIX classes) and the caller must use the ported matcher.

    For a non-dir-only pattern a file matches directly OR sits under a matched directory:
    ``M(/.*)?``. For a directory-only pattern the direct branch must not fire for files:
    ``M(/.+)`` (a matched ancestor prefix must be followed by more path). The regex is matched
    against the relative path WITH its leading slash, exactly as kopia feeds the matcher.
    """
    pat = compile_pattern(pattern, base_dir)
    if pat.negated:
        return None
    m = tokens_to_regex(pat.tokens)
    if not m:
        return None  # includes the degenerate pattern "/" (empty token list)
    return f"(?:{m})(?:/.*)?" if not pat.dir_only else f"(?:{m})(?:/.+)"
