"""The disk scanner program (`shell/disk-scanner`) against Python's own walk and fnmatch."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys

import pytest

from app.core import disklookup

pytestmark = pytest.mark.skipif(not disklookup.scanner_path(), reason="kopialens-scan is not built")


def _scan(*args: str) -> list[dict]:
    out = subprocess.run([disklookup.scanner_path(), *args], capture_output=True, text=True,
                         encoding="utf-8", timeout=60, check=True).stdout
    return [json.loads(line) for line in out.splitlines()]


@pytest.fixture
def tree(tmp_path):
    for a in range(3):
        for b in range(3):
            d = tmp_path / f"a{a}" / f"B{b}" / "deep"
            d.mkdir(parents=True)
            for i in range(a + b + 1):
                (d / f"f{i}.ISO" if i % 2 else d / f"f{i}.txt").write_bytes(b"x" * (37 * i + a))
    (tmp_path / "empty").mkdir()
    (tmp_path / "top.txt").write_bytes(b"y" * 5)
    if sys.platform != "win32":
        (tmp_path / "link").symlink_to(tmp_path / "a0")   # never followed, like kopia
    return tmp_path


def _walk_totals(path) -> tuple[int, int]:
    size = count = 0
    for dp, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(dp, f)
            if not os.path.islink(p):
                size += os.path.getsize(p)
                count += 1
    return size, count


def test_folder_totals_two_levels_deep_match_a_walk(tree):
    rows = _scan("totals", str(tree), "--depth", "2")
    assert rows[-1]["end"] and rows[-1]["complete"]
    got = {tuple(r["rel"]): (r["size"], r["files"]) for r in rows[:-1]}
    want = {(): _walk_totals(tree)}
    for dp, dirs, _files in os.walk(tree):
        rel = os.path.relpath(dp, tree)
        parts = () if rel == "." else tuple(rel.split(os.sep))
        if 1 <= len(parts) <= 2:
            want[parts] = _walk_totals(dp)
    assert got == want


@pytest.mark.parametrize("pattern,dirs,fold", [("*.iso", False, True), ("f[12].*", False, False), ("b?", True, True)])
def test_find_matches_fnmatch(tree, pattern, dirs, fold):
    args = ["find", str(tree), pattern] + (["--dirs"] if dirs else []) + (["--fold"] if fold else [])
    rows = _scan(*args)
    assert rows[-1]["complete"]
    got = sorted(r["path"] for r in rows[:-1])
    want = []
    for dp, dnames, fnames in os.walk(tree):
        names = dnames if dirs else fnames
        for n in names:
            if fnmatch.fnmatchcase(n.lower() if fold else n, pattern.lower() if fold else pattern):
                want.append(os.path.join(dp, n))
    assert got == sorted(want) and got
