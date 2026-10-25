"""Fake names: deterministic per key, one-way, style rules."""

from __future__ import annotations

import os

import pytest

from app.core import fakenames
from app.core.fakenames import Namer

KEY_A = b"a" * 32
KEY_B = b"b" * 32
STRICT = ("codename", "generic")


@pytest.mark.parametrize("style", STRICT)
def test_same_name_same_fake_and_key_changes_it(style):
    a, b = Namer(KEY_A, style), Namer(KEY_B, style)
    assert a.file("Holiday Budget Final.xlsx") == a.file("Holiday Budget Final.xlsx")
    assert a.folder("Acme Corp Clients", "Documents") == a.folder("Acme Corp Clients", "Documents")
    names = [f"Client {i} Contract.docx" for i in range(40)]
    assert [a.file(n) for n in names] != [b.file(n) for n in names]


@pytest.mark.parametrize("style", STRICT)
def test_private_words_never_survive(style):
    n = Namer(KEY_A, style)
    for real, fake in [("Priya Sharma Passport.pdf", n.file("Priya Sharma Passport.pdf")),
                       ("Acme Tax 2023", n.folder("Acme Tax 2023", "Documents"))]:
        for word in ("priya", "sharma", "passport", "acme", "tax"):
            assert word not in fake.lower(), (real, fake)


def test_extension_and_technical_names_stay():
    for style in fakenames.STYLES:
        n = Namer(KEY_A, style)
        assert n.file("Wedding Speech.docx").endswith(".docx")
        assert n.folder("node_modules") == "node_modules"
        assert n.folder(".git") == ".git"


def test_generic_style_keeps_common_word_names_and_rerolls_digits():
    n = Namer(KEY_A, "generic")
    assert n.folder("Old Backups") == "Old Backups"
    fake = n.file("IMG_1234.jpg")
    assert fake.startswith("IMG_") and fake.endswith(".jpg")
    assert n.folder("Mystery Vault", "Photos") in {
        w + q for w in fakenames.FOLDER_WORDS["Photos"] for q in fakenames.QUALIFIERS}


def test_balanced_keeps_names_and_replaces_only_risky_ones():
    n = Namer(KEY_A, "balanced", replace={"Acme Studio Mix"})
    assert n.folder("Program Files (x86)") == "Program Files (x86)"
    assert n.file("Killswitch Engage - My Curse.rpp") == "Killswitch Engage - My Curse.rpp"
    assert n.user("anshul") == "anshul"
    for name in ("Acme Studio Mix", "Game [FitGirl Repack]", "RUNE", "Stash"):
        assert name.lower() not in n.folder(name).lower()
    assert n.folder("Rune Factory") == "Rune Factory"          # all-caps scene groups only
    assert n.file("Some Game.iso").endswith(".iso") and "Some Game" not in n.file("Some Game.iso")


def test_balanced_rewrites_ids_contacts_and_sites_in_place():
    n = Namer(KEY_A, "balanced")
    steam = n.folder("76561198088033968")
    assert steam.startswith("7656119") and len(steam) == 17 and steam != "76561198088033968"
    assert "9831275513" not in n.file("disruption of 9831275513 sim.pdf")
    assert n.file("e94d8278230228a9@v2") == "e94d8278230228a9@v2"      # hex ids are not phones
    assert n.file("icon18_safari@2x.png") == "icon18_safari@2x.png"    # not an email
    assert "me.real@gmail.com" not in n.file("me.real@gmail.com.txt")
    site = n.folder("https_www.private-site.com_0.indexeddb.leveldb")
    assert site.startswith("https_") and "private-site" not in site and site.endswith(".indexeddb.leveldb")


def test_codename_style_is_obviously_a_demo():
    n = Namer(KEY_A, "codename")
    adj, noun = n.folder("Old Backups").lower().split(" ")
    assert adj in fakenames.CODE_ADJ and noun in fakenames.CODE_NOUN
    assert n.user("anshul") == "demo"


def test_attempt_gives_a_different_name():
    n = Namer(KEY_A, "generic")
    assert n.file("notes.md", 0) != n.file("notes.md", 1)
    assert n.folder("Photos", "Photos", 1).startswith("Photos ")


def test_thousands_of_same_named_siblings_settle_in_few_rounds():
    """Re-rolling must make 5,000 siblings that share one fake name unique within a few rounds."""
    n = Namer(KEY_A, "generic")
    attempt = {f"real {i}": 0 for i in range(5000)}
    for rounds in range(1, 11):
        seen: dict[str, str] = {}
        losers = []
        for real in sorted(attempt):
            fake = n._bump("Photos", real, attempt[real], " ").lower()
            if fake in seen:
                losers.append(real)
            else:
                seen[fake] = real
        if not losers:
            break
        for real in losers:
            attempt[real] += 1
    assert not losers, f"{len(losers)} siblings still collide after {rounds} rounds"


def test_category_needs_a_majority():
    assert fakenames.category_of({"jpg": 8, "txt": 2}) == "Photos"
    assert fakenames.category_of({"jpg": 3, "mp3": 3, "exe": 4}) == "Mixed"
    assert fakenames.category_of({}) == "Mixed"


def test_key_is_created_once_and_reused(tmp_path):
    k1 = fakenames.load_key(str(tmp_path))
    k2 = fakenames.load_key(str(tmp_path))
    assert k1 == k2 and len(k1) == 32
    assert os.path.exists(os.path.join(tmp_path, fakenames.KEY_FILE))
