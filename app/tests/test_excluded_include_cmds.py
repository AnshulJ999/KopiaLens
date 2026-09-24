"""The include-cmds endpoint must only generate commands for KOPIA repos.

The Backblaze view is a repo record but not a kopia repo: it carries no CLI args, so a command
built for it is bare - and a bare `kopia snapshot create` targets the DEFAULT kopia repo. Offering
that under the "Backblaze" label handed users a wrong-repo command.
"""

from __future__ import annotations

import app.api.excluded as excluded
from app.core import kopia


def test_include_cmds_skips_non_kopia_repos(monkeypatch):
    fake_repos = {
        "1": {"name": "Main", "args": []},
        "2": {"name": "Big", "args": ["--config-file", "big.config"]},
        "bz": {"name": "Backblaze Personal Backup", "kind": "backblaze", "args": []},
    }
    monkeypatch.setattr(excluded, "REPOS", fake_repos)
    monkeypatch.setattr(excluded, "repo_kind",
                        lambda r: "backblaze" if r == "bz" else "kopia")
    # the args now come from _repo_args, which holds its own REPOS reference (test_repo_kind_guard
    # forbids any other module reading the raw args), so the guard's view has to be faked too
    monkeypatch.setattr(kopia, "REPOS", fake_repos)
    out = excluded.include_cmds(path="C:\\test")
    assert [c["repo"] for c in out["commands"]] == ["1", "2"]  # bz never appears
    # the surviving commands keep their repo targeting intact, and name the resolved kopia program
    from app.core.kopiabin import exe as kopia_exe
    assert out["commands"][0]["command"].endswith("snapshot create C:\\test")
    assert kopia_exe() in out["commands"][0]["command"]
    assert "--config-file" not in out["commands"][0]["command"]
    assert "--config-file big.config" in out["commands"][1]["command"]


def test_include_cmds_requires_path():
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        excluded.include_cmds(path="")
    assert e.value.status_code == 400
