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
