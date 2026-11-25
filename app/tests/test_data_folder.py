"""One data folder: config, cache and index resolve under KOPIALENS_DATA_DIR."""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve(env: dict) -> dict:
    code = ("import json, config; print(json.dumps({k: getattr(config, k) for k in "
            "('LEGACY_LAYOUT', 'DATA_DIR', 'CACHEDIR', 'CONFIG_DIR', 'SETTINGS_FILE', 'REPOS_FILE')}))")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("KOPIALENS_")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**clean, **env},
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_data_dir_holds_everything(tmp_path):
    r = _resolve({"KOPIALENS_DATA_DIR": str(tmp_path)})
    assert r["LEGACY_LAYOUT"] is False
    assert r["DATA_DIR"] == str(tmp_path)
    assert r["CONFIG_DIR"] == str(tmp_path)
    assert r["CACHEDIR"] == os.path.join(str(tmp_path), "cache")
    assert r["REPOS_FILE"] == os.path.join(str(tmp_path), "repos.json")
    assert json.loads((tmp_path / "repos.json").read_text())["repos"] == []   # fresh install: no guessing


def test_finer_overrides_still_win(tmp_path):
    cache = tmp_path / "bigdrive"
    r = _resolve({"KOPIALENS_DATA_DIR": str(tmp_path / "d"), "KOPIALENS_CACHE_DIR": str(cache)})
    assert r["CACHEDIR"] == str(cache)
