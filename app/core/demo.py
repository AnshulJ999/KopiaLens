"""Demo copies: a repo's index with every name already faked.

The copy is a self-contained data folder (``<data>/demo/repo<N>-<style>/``) holding one plain
(unencrypted) generation as ``repo1``. Real names never exist in it, so nothing that reads it
can leak one. Sizes, counts, dates and ids stay real.

Names map by name alone (see ``fakenames``). Sibling collisions are resolved globally: a real
name that collides anywhere takes its re-rolled form everywhere, so a file reads the same in
Search, the Tree and every path.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from config import APP_VERSION, DATA_DIR

from ..index import store
from . import fakenames
from .applog import get_logger

log = get_logger("demo")

DEMO_DIR = "demo"
_MAX_ROUNDS = 30
_SEG_SPLIT = r"[\\/@:]"
_KEPT_COLUMNS = {"ext", "repo"}


def copy_dir(repo: str, style: str) -> str:
    return os.path.join(DATA_DIR, DEMO_DIR, f"repo{repo}-{style}")


def _source_parts(source: str) -> tuple[str, str, str]:
    user, rest = source.split("@", 1)
    host, path = rest.split(":", 1)
    return user, host, path


def _sep_for(path: str) -> str:
    return "/" if path.startswith("/") else "\\"


def _join(parent: str, name: str, sep: str) -> str:
    if not parent:
        return sep + name
    return (parent if parent.endswith(sep) else parent + sep) + name


def _is_root_segment(seg: str) -> bool:
    return seg == "" or bool(re.fullmatch(r"[A-Za-z]:", seg))


class _Names:
    """real name -> fake name, per kind ('d' folder, 'f' file)."""

    def __init__(self, namer: fakenames.Namer, categories: dict[str, str]):
        self.namer = namer
        self.categories = categories
        self.attempt: dict[tuple[str, str], int] = {}
        self.fake: dict[tuple[str, str], str] = {}

    def get(self, kind: str, real: str) -> str:
        key = (kind, real)
        if key not in self.fake:
            self._make(key)
        return self.fake[key]

    def _make(self, key: tuple[str, str]) -> None:
        kind, real = key
        n = self.attempt.get(key, 0)
        if _is_root_segment(real):
            self.fake[key] = real
        elif kind == "d":
            self.fake[key] = self.namer.folder(real, self.categories.get(real.lower(), "Mixed"), n)
        else:
            self.fake[key] = self.namer.file(real, n)

    def bump(self, key: tuple[str, str]) -> None:
        self.attempt[key] = self.attempt.get(key, 0) + 1
        self._make(key)

    def kept(self, kind: str, real: str) -> bool:
        """Kept by design: a technical name, or (generic style) a name of common words only."""
        if _is_root_segment(real) or real.lower() in fakenames.TECHNICAL:
            return True
        if self.namer.style != "generic":
            return False
        stem = real if kind == "d" else fakenames.split_ext(real)[0]
        return fakenames.is_generic(stem)

    def table(self) -> pa.Table:
        keys = list(self.fake)
        return pa.table({"kind": [k for k, _ in keys], "real": [r for _, r in keys],
                         "fake": [self.fake[k] for k in keys]})


def _tables(gen) -> set[str]:
    return set(gen.manifest.get("artifacts", {}))


def _categories(cur, tables: set[str]) -> dict[str, str]:
    if not {"pathx", "anc", "dir"} <= tables:
        return {}
    counts: dict[str, dict[str, int]] = {}
    for name, ext, n in cur.execute("""
            SELECT d.name, lower(coalesce(p.ext, '')), count(*) FROM pathx p
            JOIN anc a ON a.dir_id = p.dir_id JOIN dir d ON d.dir_id = a.anc_id
            GROUP BY 1, 2""").fetchall():
        counts.setdefault(name.lower(), {})[ext] = n
    return {name: fakenames.category_of(c) for name, c in counts.items()}


def _sibling_sql(tables: set[str]) -> str:
    parts = []
    if "dir" in tables:
        parts.append("SELECT 'p' || parent_id AS g, 'd' AS kind, name AS real FROM dir "
                     "WHERE parent_id IS NOT NULL")
    if "pathx" in tables:
        parts.append("SELECT 'p' || dir_id, 'f', name FROM pathx")
    if "tent" in tables:
        parts.append("SELECT 't' || dir_id, CASE WHEN etype = 1 THEN 'd' ELSE 'f' END, name FROM tent")
    return " UNION ".join(parts)


def _resolve_collisions(cur, names: _Names, tables: set[str]) -> int:
    """Re-roll colliding names until every folder's children are unique (case-insensitive)."""
    sql = _sibling_sql(tables)
    if not sql:
        return 0
    cur.execute(f"CREATE OR REPLACE TEMP TABLE _demo_sib AS SELECT DISTINCT * FROM ({sql})")
    for kind, real in cur.execute("SELECT DISTINCT kind, real FROM _demo_sib").fetchall():
        names.get(kind, real)
    bumped = 0
    for _ in range(_MAX_ROUNDS):
        cur.register("_demo_names", names.table())
        groups = cur.execute("""
            SELECT list(DISTINCT {'kind': s.kind, 'real': s.real}) FROM _demo_sib s
            JOIN _demo_names n ON n.kind = s.kind AND n.real = s.real
            GROUP BY s.g, lower(n.fake) HAVING count(DISTINCT (s.kind, s.real)) > 1""").fetchall()
        cur.unregister("_demo_names")
        if not groups:
            return bumped
        losers: set[tuple[str, str]] = set()
        for (members,) in groups:
            ranked = sorted(((m["kind"], m["real"]) for m in members),
                            key=lambda k: names.namer._pick(k[0], k[1], "order", n=1 << 62))
            losers.update(ranked[1:])
        for key in losers:
            names.bump(key)
        bumped += len(losers)
    raise RuntimeError("demo copy: sibling name collisions did not settle")


def _fake_path(path: str, sep: str, names: _Names, last_is_file: bool) -> str:
    segs = path.split(sep)
    out = []
    for i, seg in enumerate(segs):
        if _is_root_segment(seg):
            out.append(seg)
        else:
            out.append(names.get("f" if last_is_file and i == len(segs) - 1 else "d", seg))
    return sep.join(out)


def _fake_source(source: str, names: _Names) -> str:
    user, host, path = _source_parts(source)
    return (f"{names.namer.user(user)}@{names.namer.host(host)}:"
            f"{_fake_path(path, _sep_for(path), names, False)}")


def _write(cur, sql: str, out_path: str) -> int:
    reader = cur.execute(sql).to_arrow_reader(200_000)
    rows = 0
    with pq.ParquetWriter(out_path, reader.schema) as writer:
        for batch in reader:
            writer.write_batch(batch)
            rows += batch.num_rows
    return rows


def make_copy(repo: str, style: str) -> dict:
    """Write a demo copy of ``repo``'s current generation. Returns counts only, never names."""
    namer = fakenames.Namer(fakenames.load_key(DATA_DIR), style)
    target = copy_dir(repo, style)
    building = target + ".building"
    if os.path.isdir(building):
        shutil.rmtree(building)
    pool = os.path.join(building, "repo1", "pool")
    os.makedirs(pool)
    gen = store.current(repo)
    try:
        cur = gen.cursor()
        tables = _tables(gen)
        names = _Names(namer, _categories(cur, tables))
        bumped = _resolve_collisions(cur, names, tables)

        sources = dict(cur.execute("SELECT src_id, source FROM src").fetchall()) if "src" in tables else {}
        src_map = {s: _fake_source(s, names) for s in sources.values()}
        extra = list(gen.manifest.get("sources", {}))
        for t in ("isnaps", "sanalysis"):
            if t in tables:
                extra += [r[0] for r in cur.execute(f"SELECT DISTINCT source FROM {t}").fetchall()]
        for s in extra:
            src_map.setdefault(s, _fake_source(s, names))
        cur.register("_demo_src", pa.table({"real": list(src_map), "fake": list(src_map.values())}))

        if "dir" in tables:
            dirs = cur.execute("SELECT dir_id, src_id, dir_path, name, parent_id FROM dir "
                               "ORDER BY depth, dir_id").fetchall()
            fake_dir: dict[int, str] = {}
            rows = {"dir_id": [], "dir_path": [], "name": [], "sep": []}
            for dir_id, src_id, dir_path, name, parent_id in dirs:
                sep = _sep_for(_source_parts(sources[src_id])[2]) if src_id in sources else "\\"
                if parent_id is None or parent_id not in fake_dir:
                    fpath = _fake_path(dir_path, sep, names, False)
                    fname = fpath.split(sep)[-1] if name else name
                else:
                    fname = names.get("d", name)
                    fpath = _join(fake_dir[parent_id], fname, sep)
                fake_dir[dir_id] = fpath
                rows["dir_id"].append(dir_id)
                rows["dir_path"].append(fpath)
                rows["name"].append(fname)
                rows["sep"].append(sep)
            cur.register("_demo_dir", pa.table(rows))

        cur.register("_demo_names", names.table())
        written: dict[str, int] = {}
        selects = {
            "src": "SELECT t.src_id, m.fake AS source FROM src t JOIN _demo_src m ON m.real = t.source",
            "isnaps": "SELECT t.* REPLACE ('1' AS repo, m.fake AS source) FROM isnaps t "
                      "JOIN _demo_src m ON m.real = t.source",
            "sanalysis": "SELECT t.* REPLACE ('1' AS repo, m.fake AS source) FROM sanalysis t "
                         "JOIN _demo_src m ON m.real = t.source",
            "dir": "SELECT t.* REPLACE (f.dir_path AS dir_path, f.name AS name) FROM dir t "
                   "JOIN _demo_dir f USING (dir_id) ORDER BY t.dir_id",
            "pathx": """SELECT t.* REPLACE (n.fake AS name, CASE
                            WHEN f.dir_path = '' THEN f.sep || n.fake
                            WHEN right(f.dir_path, 1) = f.sep THEN f.dir_path || n.fake
                            ELSE f.dir_path || f.sep || n.fake END AS full_path)
                        FROM pathx t JOIN _demo_dir f USING (dir_id)
                        JOIN _demo_names n ON n.kind = 'f' AND n.real = t.name
                        ORDER BY t.src_id, t.path_id""",
            "tent": """SELECT * FROM (SELECT t.* REPLACE (n.fake AS name,
                            (min(t.pos) OVER (PARTITION BY t.dir_id)
                             + row_number() OVER (PARTITION BY t.dir_id ORDER BY n.fake) - 1) AS pos,
                            NULL::VARCHAR AS s_errors, NULL::VARCHAR AS extra)
                       FROM tent t JOIN _demo_names n
                         ON n.kind = (CASE WHEN t.etype = 1 THEN 'd' ELSE 'f' END) AND n.real = t.name)
                       ORDER BY dir_id, pos""",
        }
        for name in sorted(tables):
            sql = selects.get(name, f"SELECT * FROM {name}")
            written[name] = _write(cur, sql, os.path.join(pool, f"{name}.parquet"))
        real_rows = {name: cur.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                     for name in tables}
    finally:
        gen.release()

    lost = {n: real_rows[n] - written[n] for n in tables if real_rows[n] != written[n]}
    if lost:
        raise RuntimeError(f"demo copy dropped rows: {lost}")

    m = gen.manifest
    manifest = {
        "generation": m.get("generation"), "repo": "1", "schema_version": m.get("schema_version"),
        "counters": m.get("counters", {}), "stats": m.get("stats", {}), "encrypted": False,
        "sources": {src_map[k]: dict(v, key=src_map[k]) if isinstance(v, dict) and "key" in v else v
                    for k, v in m.get("sources", {}).items()},
        "tree_incomplete": [src_map[s] for s in m.get("tree_incomplete", []) if s in src_map],
        "tree_forced_for": [src_map[s] for s in m.get("tree_forced_for", []) if s in src_map],
        "artifacts": {name: [f"pool/{name}.parquet"] for name in sorted(tables)},
    }
    with open(os.path.join(building, "repo1", "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    info = {"style": style, "source_repo": repo, "app_version": APP_VERSION,
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    with open(os.path.join(building, "demo.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh)

    leaks = leak_check(building, names, src_map)
    if leaks["leaked"]:
        raise RuntimeError(f"demo copy failed its leak check: {leaks['leaked']} real names found")
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.replace(building, target)
    result = {"path": target, "rows": written, "renamed": len(names.fake), "re_rolled": bumped,
              "kept_by_design": leaks["kept"], "checked_names": leaks["checked"]}
    log.info("demo copy of repo %s (%s): %s names, %s re-rolled", repo, style,
             result["renamed"], bumped)
    return result


def leak_check(copy_root: str, names: _Names, real_sources) -> dict:
    """Count real names found as whole path segments anywhere in the copy. Counts only."""
    forbidden = {real.lower() for (kind, real) in names.fake if not names.kept(kind, real)}
    for s in real_sources:
        user, host, path = _source_parts(s)
        forbidden.update({user.lower(), host.lower()})
        forbidden.update(seg.lower() for seg in path.replace("\\", "/").split("/")
                         if not _is_root_segment(seg) and not names.kept("d", seg))
    # Names under 3 characters or all digits identify nothing and collide with drive letters,
    # extensions and ids.
    forbidden = {f for f in forbidden if len(f) >= 3 and not f.isdigit()}
    # A fake name that equals a real name elsewhere comes from the word lists, not from the data.
    forbidden -= {fake.lower() for fake in names.fake.values()}
    leaked = 0
    con = duckdb.connect()
    con.register("_forbidden", pa.table({"seg": sorted(forbidden)}))
    repo_dir = os.path.join(copy_root, "repo1")
    for fname in os.listdir(os.path.join(repo_dir, "pool")):
        path = os.path.join(repo_dir, "pool", fname).replace("\\", "/")
        schema = pq.read_schema(path)
        for col in schema.names:
            if col in _KEPT_COLUMNS or not pa.types.is_string(schema.field(col).type):
                continue
            leaked += con.execute(f"""
                SELECT count(*) FROM (SELECT unnest(regexp_split_to_array(lower("{col}"),
                    '{_SEG_SPLIT}')) AS seg FROM read_parquet('{path}'))
                WHERE seg IN (SELECT seg FROM _forbidden)""").fetchone()[0]
    con.close()
    with open(os.path.join(repo_dir, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    # Only the source fields carry data; the rest is the copy's own fixed bookkeeping.
    data = [manifest.get("sources", {}), manifest.get("tree_incomplete", []),
            manifest.get("tree_forced_for", [])]
    leaked += sum(1 for text in _strings(data)
                  for seg in re.split(_SEG_SPLIT, text.lower()) if seg in forbidden)
    kept = sum(1 for (kind, real) in names.fake if names.kept(kind, real))
    return {"leaked": leaked, "checked": len(forbidden), "kept": kept}


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Make a demo copy of a repo's index.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--style", choices=fakenames.STYLES, default="generic")
    args = ap.parse_args()
    r = make_copy(args.repo, args.style)
    print(json.dumps({k: v for k, v in r.items() if k != "path"}, indent=1))
    print("written to", r["path"])


if __name__ == "__main__":
    main()
