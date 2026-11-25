"""Guard for trap 39: a module that names a Full-covered file must use the files layer.

The 2026-09-24 bug class was new code opening an encrypted file with a raw ``json.load``. This
scan is deliberately simple: any app module whose source names one of the covered files must also
use ``filecrypto``. The plain-by-design files (``data_version.json``, the worker status file) are
not in the list.
"""

from __future__ import annotations

import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[1]

COVERED_NAMES = (
    "bg_worker_state.json", "repo_size_history.json", "index_result_repo",
    "index_progress_detail_repo", "jobs_history.jsonl", "audit.jsonl", "mounts.json",
    "reindex_pending.json", "policy-backups",
)


def test_every_module_naming_a_covered_file_uses_the_files_layer():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if "/tests/" in f"/{rel}" or rel == "core/filecrypto.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        named = [n for n in COVERED_NAMES if n in text]
        if named and "filecrypto" not in text:
            offenders.append(f"{rel} names {named} without filecrypto")
    assert not offenders, offenders


def test_the_kopia_inventory_lists_one_path_once(monkeypatch):
    """Windows: PATH gives `kopia.EXE`, a bundle `kopia.exe` - the same file."""
    import os

    from app.core import kopiabin
    monkeypatch.setattr(kopiabin, "_bundled_copies", lambda: [])
    monkeypatch.setattr(kopiabin, "_usual_locations", lambda: [r"C:\K\kopia.exe"])
    monkeypatch.setattr(kopiabin, "_kopiaui_bundles", lambda: [])
    monkeypatch.setattr(kopiabin.shutil, "which", lambda _n: r"C:\K\kopia.EXE")
    monkeypatch.setattr(kopiabin, "explicit_setting", lambda: "")
    monkeypatch.delenv(kopiabin.ENV_OVERRIDE, raising=False)
    paths = [p for p, _ in kopiabin.candidates()]
    expected = 1 if os.name == "nt" else 2          # case-sensitive filesystems keep both
    assert len(paths) == expected


def test_a_real_rewrite_failure_is_not_reported_as_deferred(monkeypatch):
    from app.index import keyswitch, publish
    monkeypatch.setattr(keyswitch.crypto, "configured", lambda: True)
    monkeypatch.setattr(keyswitch.crypto, "unlocked", lambda: True)
    monkeypatch.setattr(keyswitch, "repo_states",
                        lambda: [{"repo": "3", "generation": 1, "encrypted": False}])
    monkeypatch.setattr(keyswitch, "switch_caches", lambda on, say=None: {})
    monkeypatch.setattr("app.core.filecrypto.switch_files", lambda on, say=None: {})
    monkeypatch.setattr(keyswitch, "bz_state", lambda: {"exists": False})

    def busy(repo, on, say=None):
        raise publish.WriterBusy("another writer holds it")
    monkeypatch.setattr(keyswitch, "switch_repo", busy)
    assert keyswitch.apply_level("index")["deferred"] == ["3"]

    def broken(repo, on, say=None):
        raise RuntimeError("the rewrite changed the row count 5 -> 4")
    monkeypatch.setattr(keyswitch, "switch_repo", broken)
    with pytest.raises(RuntimeError, match="row count"):
        keyswitch.apply_level("index")


def test_a_garbage_keychain_entry_falls_back(monkeypatch):
    import keyring

    from app.index import crypto
    crypto.lock()
    monkeypatch.setattr(keyring, "get_password", lambda service, account: "not a key!")
    assert crypto.unlock_from_keychain() is False and not crypto.unlocked()
