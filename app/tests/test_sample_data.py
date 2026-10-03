"""Sample data: a generated repository the real indexer reads through the fake kopia."""

from __future__ import annotations

import datetime as dt
import json
import os

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from app.core import sampledata


def test_a_small_sample_indexes_through_the_real_worker_with_no_errors(tmp_path):
    """The fake kopia must stay in step with what the indexer asks and parses: every stage
    runs, and the index holds exactly the snapshots and latest files the model generated."""
    out = str(tmp_path / "sample")
    r = sampledata.build(out, files=400, seed=11)
    assert r["errors"] is None
    assert {"index", "dashboard", "analysis", "churn", "folders", "policies"} <= set(r["stages"])
    assert r["backblaze"]["ok"] and 0 < r["backblaze"]["backed"] < r["backblaze"]["files"]

    model = sampledata.Sample(sampledata.load_profile(), files=400, seed=11)
    snaps = json.loads(model.answer("1", ["snapshot", "list", "--json"])[1])
    latest = {}
    for s in snaps:
        latest[s["source"]["path"]] = s
    base = os.path.join(out, "repo1")
    with open(os.path.join(base, "manifest.json"), encoding="utf-8") as fh:
        art = {k: [os.path.join(base, f) for f in v] for k, v in json.load(fh)["artifacts"].items()}
    con = duckdb.connect()
    indexed = con.execute("SELECT count(*) FROM read_parquet(?)", [art["isnaps"]]).fetchone()[0]
    files = con.execute("SELECT count(*) FROM read_parquet(?) WHERE in_latest", [art["pathx"]]).fetchone()[0]
    assert indexed == len(snaps)
    assert files == sum(s["stats"]["fileCount"] for s in latest.values())


def test_retention_keeps_the_newest_snapshot_of_each_recent_period():
    day = dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc).timestamp()
    times = [day + i * 86400 for i in range(30)]   # 30 daily snapshots, Sep 1-30
    keep = sampledata.retention(times, {"keepLatest": 3, "keepDaily": 7, "keepMonthly": 2})
    assert sorted(keep) == list(range(23, 30))      # the last 7 days; Sep 30 is also the month's
    assert keep[29] == ["latest-1", "daily-1", "monthly-1"]
    assert keep[27] == ["latest-3", "daily-3"]


def test_the_name_check_refuses_a_word_the_generator_never_uses(tmp_path):
    """It reads the tables the manifest lists (partitioned, as the indexer writes them)."""
    base = tmp_path / "repo1"
    tables = {"pathx": ("name", ["IMG_0042.jpg", "budget-2024.xlsx", "Priya passport.pdf"]),
              "dir": ("name", ["Documents", "Invoices 2023"]),
              "src": ("source", [r"alex@HOME-PC:C:\Users\alex\Documents"]),
              "tent": ("name", ["IMG_0042.jpg"])}
    artifacts = {}
    for table, (col, values) in tables.items():
        part = base / "pool" / table / "src_id=1"
        part.mkdir(parents=True)
        pq.write_table(pa.table({col: values}), part / "g1.parquet")
        artifacts[table] = [f"pool/{table}/src_id=1/g1.parquet"]
    (base / "manifest.json").write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
    r = sampledata.check_names(str(tmp_path))
    assert r["tables"] == 4
    assert r["unknown_words"] == 2 and r["examples"] == ["passport", "priya"]


_INSTALL_SCRIPT = """
import json, os
from app.core import repoconnect, sampledata
from app.index import store
rec = sampledata.install(files=300)
gen = store.current(rec["id"])
try:
    files = gen.cursor().execute("SELECT count(*) FROM pathx WHERE in_latest").fetchone()[0]
finally:
    gen.release()
before = sorted(os.listdir(os.environ["KOPIALENS_DATA_DIR"]))
repoconnect.remove(rec["id"])
after = sorted(os.listdir(os.environ["KOPIALENS_DATA_DIR"]))
print(json.dumps({"rec": rec, "files": files, "before": before, "after": after}))
"""


def test_the_sample_installs_as_a_demo_repo_and_removes_cleanly(tmp_path):
    """Added to a normal install: a demo repo that is not a backup, readable, with its replies
    next to it; removing it leaves none of its files behind."""
    import subprocess
    import sys
    data = tmp_path / "data"
    data.mkdir()
    env = dict(os.environ, KOPIALENS_DATA_DIR=str(data), PYTHONIOENCODING="utf-8")
    for k in ("KOPIALENS_CACHE_DIR", "KOPIALENS_CONFIG_DIR", "KOPIALENS_REPOS_FILE"):
        env.pop(k, None)
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    res = subprocess.run([sys.executable, "-c", _INSTALL_SCRIPT], cwd=here, env=env, capture_output=True,
                         text=True, encoding="utf-8", timeout=600)
    assert res.returncode == 0, res.stderr[-1500:]
    out = json.loads(res.stdout.strip().splitlines()[-1])
    rec, rid = out["rec"], out["rec"]["id"]
    assert (rec["name"], rec["demo"], rec["sample"], rec["counts_as_backup"]) == ("Sample data", True, True, False)
    assert out["files"] > 0
    assert {f"repo{rid}", f"demo_replies_repo{rid}.json"} <= set(out["before"])
    assert not any(name.startswith("sample-build") for name in out["before"])
    assert not [n for n in out["after"] if f"repo{rid}" in n]


def test_a_request_about_a_demo_repo_never_reads_the_disk(monkeypatch):
    import config
    from fastapi.testclient import TestClient

    from app import main
    from app.core import disklookup
    monkeypatch.setitem(config.REPOS, "98", {"kind": "kopia", "demo": True, "name": "Sample data"})
    monkeypatch.setitem(config.REPOS, "97", {"kind": "kopia", "demo": False, "name": "Real"})
    monkeypatch.setattr(disklookup, "_kl", lambda: {"disk_lookup": "scan"})
    client = TestClient(main.app, base_url="http://127.0.0.1")
    demo_st = client.get("/api/disk-lookup", params={"repo": "98", "refresh": "true"}).json()
    real_st = client.get("/api/disk-lookup", params={"repo": "97", "refresh": "true"}).json()
    assert (demo_st["mode"], demo_st["available"]) == ("off", False)
    assert real_st["mode"] == "scan"
