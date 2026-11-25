"""Shape profile of real repositories, the input of the sample-data generator.

Numbers only: histograms, shares, quantiles and per-source scale figures. No names, paths, ids
or hashes ever enter the profile, so it can be committed. File types (extension, share, sizes)
come from ``--types-from`` only; every other figure averages the given repos with equal weight.

    python -m app.core.sampleprofile 1 2 --types-from 1
"""

from __future__ import annotations

import argparse
import json
import math
import os

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "sample_profile.json")
_TOP_TYPES = 60
_QUANTS = (0.05, 0.25, 0.5, 0.75, 0.95, 0.99)
_LOG2 = "CASE WHEN {x} <= 0 THEN 0 ELSE floor(log2({x}))::INTEGER + 1 END"


def _hist(cur, sql: str) -> dict[str, float]:
    """``sql`` yields (bin, count); returns bin -> share of the total."""
    rows = cur.execute(sql).fetchall()
    total = sum(c for _, c in rows) or 1
    return {str(int(b)): c / total for b, c in rows}


def _log2_hist(cur, value_sql: str) -> dict[str, float]:
    return _hist(cur, f"SELECT {_LOG2.format(x='n')} AS b, count(*) FROM ({value_sql}) GROUP BY b ORDER BY b")


def _sources(cur) -> list[dict]:
    rows = cur.execute("""
        WITH s AS (SELECT src_id, count(*) AS snaps,
                          date_diff('second', min(start_time), max(start_time)) / 86400.0 AS span
                   FROM snap GROUP BY src_id),
             g AS (SELECT src_id, median(gap) AS gap FROM (
                     SELECT src_id, date_diff('second', lag(start_time) OVER (PARTITION BY src_id
                            ORDER BY start_time), start_time) / 3600.0 AS gap FROM snap)
                   WHERE gap IS NOT NULL GROUP BY src_id),
             f AS (SELECT src_id, count(*) AS files FROM pathx WHERE in_latest GROUP BY src_id),
             d AS (SELECT src_id, count(*) AS dirs FROM dir GROUP BY src_id)
        SELECT s.snaps, s.span, g.gap, coalesce(f.files, 0), coalesce(d.dirs, 0)
        FROM s LEFT JOIN g USING (src_id) LEFT JOIN f USING (src_id) LEFT JOIN d USING (src_id)
        ORDER BY 1, 2, 3, 4, 5""").fetchall()
    return [{"snapshots": int(a), "span_days": round(float(b), 1),
             "median_gap_hours": round(float(c), 2) if c is not None else None,
             "files": int(e), "dirs": int(f)} for a, b, c, e, f in rows]


def _rates(cur) -> dict:
    """Per snapshot, as a share of the source's latest file count: files added, deleted and
    changed. Median over sources with at least 3 snapshots."""
    rows = cur.execute("""
        WITH s AS (SELECT src_id, count(*) AS snaps, min(start_time) AS first FROM snap GROUP BY src_id),
             p AS (SELECT x.src_id,
                          count(*) FILTER (WHERE x.in_latest) AS latest,
                          count(*) FILTER (WHERE x.first_seen > s.first) AS added,
                          count(*) FILTER (WHERE NOT x.in_latest) AS deleted,
                          sum(greatest(x.versions - 1, 0)) AS changed
                   FROM pathx x JOIN s USING (src_id) GROUP BY x.src_id)
        SELECT s.snaps, p.latest, p.added, p.deleted, p.changed
        FROM p JOIN s USING (src_id) WHERE s.snaps >= 3 AND p.latest > 0""").fetchall()

    def med(vals: list[float]) -> float:
        vals = sorted(vals)
        return round(vals[len(vals) // 2], 6) if vals else 0.0
    steps = [(n - 1, latest, a, d, c) for n, latest, a, d, c in rows]
    return {"added": med([a / latest / k for k, latest, a, _, _ in steps]),
            "deleted": med([d / latest / k for k, latest, _, d, _ in steps]),
            "changed": med([c / latest / k for k, latest, _, _, c in steps]),
            "sources": len(steps)}


def _types(cur) -> dict:
    q = ", ".join(str(x) for x in _QUANTS)
    rows = cur.execute(f"""
        SELECT coalesce(nullif(lower(p.ext), ''), '') AS ext, count(*) AS n,
               quantile_cont(p.size, [{q}]) AS qs,
               sum(c.packed)::DOUBLE / nullif(sum(p.size) FILTER (WHERE c.packed IS NOT NULL), 0)
        FROM pathx p LEFT JOIN csize c ON c.cid_id = p.cid_id
        WHERE p.in_latest GROUP BY 1 ORDER BY n DESC""").fetchall()
    total = sum(r[1] for r in rows) or 1
    top = rows[:_TOP_TYPES]
    other = rows[_TOP_TYPES:]
    out = [{"ext": ext, "share": n / total, "sizes": [int(v) for v in qs],
            "packed_ratio": round(min(float(pr), 1.0), 3) if pr else 1.0} for ext, n, qs, pr in top]
    return {"types": out, "other_share": sum(r[1] for r in other) / total}


def profile(repo: str, with_types: bool) -> dict:
    from ..index import store
    cur = store.read_cursor(repo)
    try:
        one = lambda sql: cur.execute(sql).fetchone()[0]  # noqa: E731
        out = {
            "sources": _sources(cur),
            "depth": _hist(cur, "SELECT depth, count(*) FROM dir GROUP BY 1 ORDER BY 1"),
            "subdirs_per_dir": _log2_hist(cur, """SELECT count(c.dir_id) AS n FROM dir d
                LEFT JOIN dir c ON c.parent_id = d.dir_id GROUP BY d.dir_id"""),
            "files_per_dir": _log2_hist(cur, """SELECT count(p.path_id) AS n FROM dir d
                LEFT JOIN pathx p ON p.dir_id = d.dir_id AND p.in_latest GROUP BY d.dir_id"""),
            "versions": _log2_hist(cur, "SELECT versions AS n FROM pathx"),
            "rates": _rates(cur),
            "dupes": {
                "share": one("""SELECT count(*) FILTER (WHERE g > 1)::DOUBLE / count(*) FROM (
                    SELECT count(*) OVER (PARTITION BY cid_id) AS g FROM pathx
                    WHERE in_latest AND cid_id IS NOT NULL AND size > 0)"""),
                "group_size": _log2_hist(cur, """SELECT count(*) AS n FROM pathx
                    WHERE in_latest AND cid_id IS NOT NULL AND size > 0
                    GROUP BY cid_id HAVING count(*) > 1"""),
            },
            "zero_size_share": one("SELECT avg((size = 0)::INTEGER) FROM pathx WHERE in_latest"),
            "split_share": one("SELECT avg(ind::INTEGER) FROM tent WHERE etype = 0"),
            "symlink_share": one("SELECT avg((etype = 2)::INTEGER) FROM tent WHERE etype IN (0, 2)"),
            "mtime_age_days": [round(float(v), 1) for v in one(f"""
                SELECT quantile_cont(date_diff('second', f.mtime, s.start_time) / 86400.0,
                                     [{', '.join(str(x) for x in _QUANTS)}])
                FROM (SELECT * FROM filev USING SAMPLE 300000 ROWS) f
                JOIN snap s ON s.snap_id = f.snap_id WHERE f.mtime IS NOT NULL""")],
        }
        if with_types:
            out.update(_types(cur))
        return out
    finally:
        cur.close()


def _mean_hist(hists: list[dict[str, float]]) -> dict[str, float]:
    keys = sorted({k for h in hists for k in h}, key=int)
    return {k: round(sum(h.get(k, 0.0) for h in hists) / len(hists), 6) for k in keys}


def merge(parts: list[dict], types: dict) -> dict:
    """Equal weight per repo, so the bigger repo does not decide the shape alone."""
    mean = lambda vals: round(sum(vals) / len(vals), 6)  # noqa: E731
    return {
        "version": 1,
        "sources": sorted((s for p in parts for s in p["sources"]),
                          key=lambda s: (s["snapshots"], s["files"])),
        **{k: _mean_hist([p[k] for p in parts])
           for k in ("depth", "subdirs_per_dir", "files_per_dir", "versions")},
        "rates": {k: mean([p["rates"][k] for p in parts]) for k in ("added", "deleted", "changed")},
        "dupes": {"share": mean([p["dupes"]["share"] for p in parts]),
                  "group_size": _mean_hist([p["dupes"]["group_size"] for p in parts])},
        **{k: mean([p[k] for p in parts]) for k in ("zero_size_share", "split_share", "symlink_share")},
        "mtime_age_days": [round(sum(v) / len(parts), 1)
                           for v in zip(*(p["mtime_age_days"] for p in parts))],
        "quantiles": list(_QUANTS),
        "types": types["types"],
        "other_type_share": round(types["other_share"], 6),
    }


def _clean(obj):
    if isinstance(obj, float):
        return 0.0 if math.isnan(obj) else round(obj, 6)
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--types-from", required=True)
    ap.add_argument("--out", default=PROFILE_PATH)
    args = ap.parse_args(argv)
    parts = []
    types = None
    for repo in args.repos:
        p = profile(repo, with_types=repo == args.types_from)
        if repo == args.types_from:
            types = {"types": p.pop("types"), "other_share": p.pop("other_share")}
        parts.append(p)
        print(f"repo {repo}: {len(p['sources'])} sources profiled")
    if types is None:
        raise SystemExit("--types-from must be one of the repos")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(_clean(merge(parts, types)), fh, indent=1)
        fh.write("\n")
    print(f"written {args.out}")


if __name__ == "__main__":
    main()
