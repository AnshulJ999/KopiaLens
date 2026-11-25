"""Unit tests for the Backblaze exclusion engine (synthetic rules - no live bzdata needed).

Run with pytest (`pytest app/backblaze/tests/test_rules.py`) OR directly
(`python app/backblaze/tests/test_rules.py`).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from app.backblaze.rules import PatternRule, RuleEngine  # noqa: E402


def _engine(dirs=None, exts=None, pats=None):
    return RuleEngine(dirs or [], set(exts or []), pats or [])


def test_extension_excluded():
    e = _engine(exts={"vhdx", "vmdk"})
    assert e.classify(r"C:\VMs\disk.vhdx") == (True, "globalext:vhdx")
    assert e.classify(r"C:\docs\note.txt")[0] is False


def test_folder_prefix_all_drives():
    # A folder rule is stored relative (drive stripped) and applies on EVERY drive.
    e = _engine(dirs=["\\games\\"])
    assert e.classify(r"D:\Games\x.exe")[0] is True
    assert e.classify(r"C:\Games\y.dat")[0] is True       # different drive, same relative folder
    assert e.classify(r"C:\GameSaves\z")[0] is False      # not under \games\


def test_pattern_skip_ends_ext():
    r = PatternRule(skip=":\\users\\", c1="", c2="", dnc=(), ends="\\ntuser.dat", ext="dat", label="mandatory:ntuser")
    e = _engine(pats=[r])
    assert e.classify(r"C:\Users\me\ntuser.dat") == (True, "mandatory:ntuser")
    # extension mismatch -> not excluded by this rule
    assert e.classify(r"C:\Users\me\ntuser.dat.log")[0] is False
    # outside :\users\ -> skip-prefix prunes it
    assert e.classify(r"C:\Windows\ntuser.dat")[0] is False


def test_pattern_contains_and_doesnotcontain():
    r = PatternRule(skip=":\\users\\", c1="\\appdata\\local\\google\\", c2="", dnc=("bookmarks", "preferences"),
                    ends="", ext="", label="editable:chrome")
    e = _engine(pats=[r])
    assert e.classify(r"C:\Users\me\AppData\Local\Google\Chrome\Cache\f001")[0] is True
    # doesNotContain hit -> rule must NOT match
    assert e.classify(r"C:\Users\me\AppData\Local\Google\Chrome\Bookmarks")[0] is False


def test_pattern_requires_both_contains():
    r = PatternRule(skip="", c1="\\itunes\\", c2="\\podcasts\\", dnc=(), ends="", ext="", label="editable:podcasts")
    e = _engine(pats=[r])
    assert e.classify(r"C:\Music\iTunes\iTunes Media\Podcasts\ep1.m4a")[0] is True
    assert e.classify(r"C:\Music\iTunes\iTunes Media\Music\song.m4a")[0] is False  # missing contains_2


def test_backed_default():
    e = _engine(dirs=["\\games\\"], exts={"vhdx"}, pats=[
        PatternRule(skip=":\\users\\", c1="", c2="", dnc=(), ends="\\ntuser.dat", ext="dat", label="x")])
    assert e.classify(r"E:\Photos\2026\trip.jpg") == (False, None)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} rule tests passed.")


if __name__ == "__main__":
    _run_all()
