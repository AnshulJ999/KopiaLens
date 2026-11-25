"""A Backblaze data folder that exists but can't be read (folder permissions, or macOS privacy
settings) is reported as blocked, not as "Backblaze wasn't found"."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.backblaze import paths  # noqa: E402


@pytest.fixture
def bzdata(tmp_path, monkeypatch):
    root = tmp_path / "bzdata"
    (root / "bzfilelists").mkdir(parents=True)
    monkeypatch.setattr(paths, "_bzdata_root", str(root))
    return root


def _deny(monkeypatch, denied: str) -> None:
    real = os.listdir

    def listdir(path="."):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(denied)):
            raise PermissionError(1, "Operation not permitted", path)
        return real(path)
    monkeypatch.setattr(paths.os, "listdir", listdir)


def test_a_readable_folder_is_ok_and_a_missing_one_is_missing(bzdata, tmp_path):
    assert paths.access() == "ok"
    assert paths.access(str(tmp_path / "nowhere")) == "missing"
    assert paths.access(str(tmp_path)) == "missing"      # a folder, but not Backblaze's


def test_a_folder_that_cant_be_listed_is_blocked_in_setup(bzdata, monkeypatch):
    from app.api import setup
    _deny(monkeypatch, str(bzdata / "bzfilelists"))
    assert paths.access() == "blocked"
    state = setup.backblaze_state()
    assert state["blocked"] and not state["found"] and state["blocked_path"] == str(bzdata)

    with pytest.raises(HTTPException) as err:
        setup.backblaze_folder(setup.BackblazeFolderRequest(path=str(bzdata)))
    assert "can't open that folder" in err.value.detail
