"""The public copy of CHANGELOG.md, as the website shows it.

    python packaging/public_changelog.py <CHANGELOG.md> <out> [--version X.Y.Z --date YYYY-MM-DD]

With a released stable version and its date, that version's undated heading
(``## [1.0.0] - unreleased``) gets the date. Every section still without a date is left out:
the website shows only ``## [X] - YYYY-MM-DD`` sections, and nothing unreleased goes public.
Prints the versions kept, one per line.
"""

from __future__ import annotations

import argparse
import re
import sys

HEADING = re.compile(r"^## \[([^\]]+)\](.*)$")
DATED = re.compile(r"\d{4}-\d{2}-\d{2}")


def public(text: str, version: str = "", date: str = "") -> tuple[str, list[str]]:
    out: list[str] = []
    kept: list[str] = []
    keep = True
    for line in text.splitlines(keepends=True):
        m = HEADING.match(line.rstrip("\r\n"))
        if m:
            ver, rest = m.group(1), m.group(2)
            if not DATED.search(rest) and version and ver == version and "-" not in version and date:
                line = f"## [{ver}] - {date}\n"
                rest = date
            keep = bool(DATED.search(rest))
            if keep:
                kept.append(ver)
        if keep:
            out.append(line)
    return "".join(out), kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--version", default="")
    ap.add_argument("--date", default="")
    a = ap.parse_args()
    with open(a.src, encoding="utf-8") as fh:
        text, kept = public(fh.read(), a.version, a.date)
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("\n".join(kept))
    return 0


if __name__ == "__main__":
    sys.exit(main())
