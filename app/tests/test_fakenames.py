"""Fake names: deterministic per key, one-way, style rules."""

from __future__ import annotations

import os

import pytest

from app.core import fakenames
from app.core.fakenames import Namer

KEY_A = b"a" * 32
KEY_B = b"b" * 32


@pytest.mark.parametrize("style", fakenames.STYLES)
def test_same_name_same_fake_and_key_changes_it(style):
    a, b = Namer(KEY_A, style), Namer(KEY_B, style)
    assert a.file("Holiday Budget Final.xlsx") == a.file("Holiday Budget Final.xlsx")
    assert a.folder("Acme Corp Clients", "Documents") == a.folder("Acme Corp Clients", "Documents")
    names = [f"Client {i} Contract.docx" for i in range(40)]
    assert [a.file(n) for n in names] != [b.file(n) for n in names]


@pytest.mark.parametrize("style", fakenames.STYLES)
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


def test_codename_style_is_obviously_a_demo():
    n = Namer(KEY_A, "codename")
    adj, noun = n.folder("Old Backups").lower().split(" ")
    assert adj in fakenames.CODE_ADJ and noun in fakenames.CODE_NOUN
    assert n.user("anshul") == "demo"


def test_attempt_gives_a_different_name():
    n = Namer(KEY_A, "generic")
    assert n.file("notes.md", 0) != n.file("notes.md", 1)
    assert n.folder("Photos", "Photos", 1).startswith("Photos ")


def test_category_needs_a_majority():
    assert fakenames.category_of({"jpg": 8, "txt": 2}) == "Photos"
    assert fakenames.category_of({"jpg": 3, "mp3": 3, "exe": 4}) == "Mixed"
    assert fakenames.category_of({}) == "Mixed"


def test_key_is_created_once_and_reused(tmp_path):
    k1 = fakenames.load_key(str(tmp_path))
    k2 = fakenames.load_key(str(tmp_path))
    assert k1 == k2 and len(k1) == 32
    assert os.path.exists(os.path.join(tmp_path, fakenames.KEY_FILE))
