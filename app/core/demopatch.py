"""Apply the rules file's renames to a finished demo copy in place, without a rebuild.

Every rename (text in a name -> what to show) is replaced as a whole word in every text value the
copy holds: index tables, caches, recorded kopia replies, task history, logs and the Backblaze data.
A rename already applied finds nothing, so running it again is harmless. The repos' build
fingerprints are updated, so the next build keeps reusing the patched copy.

    python -m app.core.demopatch [--repo 1,2,3]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

import duckdb

from . import demo, fakenames
from config import DATA_DIR

_TEXT = (".json", ".jsonl", ".log", ".txt", ".xml")


def _pairs(rules: dict) -> dict[str, str]:
    keep = {str(k).lower() for k in rules.get("keep_names") or ()}
    return {str(k).lower(): str(v) for k, v in (rules.get("rename") or {}).items()
            if k and v and str(k).lower() not in keep and str(k).lower() != str(v).lower()}


class _Renamer:
    def __init__(self, pairs: dict[str, str]):
        self.pairs = pairs
        self.re = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(
            re.escape(k) for k in sorted(pairs, key=len, reverse=True)) + r")(?![A-Za-z0-9])", re.IGNORECASE)

    def text(self, s: str) -> str:
        return self.re.sub(lambda m: self.pairs[m.group(0).lower()], s)

    def present(self, s: str) -> set[str]:
        return {m.group(0).lower() for m in self.re.finditer(s)}


def _re2(text: str) -> str:
    """Escape for DuckDB's RE2, which rejects some of Python's escapes (a space, say)."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text)


def _sql_replace(col: str, keys: set[str], pairs: dict[str, str]) -> str:
    # RE2 has no lookbehind: the word boundary is captured and put back; run twice for back-to-back hits.
    expr = f'"{col}"'
    for k in sorted(keys, key=len, reverse=True):
        pat = ("(?i)(^|[^A-Za-z0-9])" + _re2(k) + "([^A-Za-z0-9]|$)").replace("'", "''")
        rep = "\\1" + pairs[k].replace("\\", "\\\\").replace("'", "''") + "\\2"
        for _ in range(2):
            expr = f"regexp_replace({expr}, '{pat}', '{rep}', 'g')"
    return expr


def _parquet(path: str, r: _Renamer) -> int:
    con = duckdb.connect()
    try:
        text_cols = [d[0] for d in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()
                     if d[1] == "VARCHAR"]
        if not text_cols:
            return 0
        big = "(?i)(^|[^A-Za-z0-9])(" + "|".join(_re2(k) for k in r.pairs) + ")([^A-Za-z0-9]|$)"
        found: set[str] = set()
        for c in text_cols:
            for (v,) in con.execute(f'SELECT DISTINCT "{c}" FROM read_parquet(?) '
                                    f'WHERE regexp_matches("{c}", ?)', [path, big]).fetchall():
                found |= r.present(v)
        if not found:
            return 0
        sel = ", ".join(f"{_sql_replace(c, found, r.pairs)} AS \"{c}\"" for c in text_cols)
        tmp = path + ".patch"
        con.execute(f"COPY (SELECT * REPLACE ({sel}) FROM read_parquet(?)) TO '{tmp}' (FORMAT parquet)", [path])
    finally:
        con.close()
    os.replace(tmp, path)
    return len(found)


def _textfile(path: str, r: _Renamer) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            s = fh.read()
    except (UnicodeDecodeError, OSError):
        return 0      # encrypted or binary: holds no shown names
    found = r.present(s)
    if found:
        with open(path + ".patch", "w", encoding="utf-8", newline="") as fh:
            fh.write(r.text(s))
        os.replace(path + ".patch", path)
    return len(found)


def _sqlite(path: str, r: _Renamer) -> int:
    con = sqlite3.connect(path)
    con.create_function("kl_hit", 1, lambda v: 1 if isinstance(v, str) and r.re.search(v) else 0, deterministic=True)
    con.create_function("kl_rename", 1, r.text, deterministic=True)
    n = 0
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                    "AND name NOT LIKE 'sqlite_%'").fetchall():
            for c in [c[1] for c in con.execute(f'PRAGMA table_info("{table}")')]:
                n += con.execute(f'UPDATE "{table}" SET "{c}" = kl_rename("{c}") WHERE kl_hit("{c}")').rowcount
        con.commit()
    finally:
        con.close()
    return n


def patch(repos: str = "1,2,3", style: str = "balanced") -> dict:
    ids = demo._repo_ids(repos)
    rules = demo.load_rules()
    target = demo.copy_dir(ids, style)
    if not os.path.isdir(target):
        raise SystemExit(f"no finished copy at {target}")
    pairs = _pairs(rules)
    r = _Renamer(pairs)
    changed: dict[str, int] = {}
    skipped: list[str] = []
    for root in (target, target + ".names"):
        for folder, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(folder, name)
                low = name.lower()
                if low.endswith(".parquet"):
                    n = _parquet(path, r)
                elif low.endswith(_TEXT):
                    n = _textfile(path, r)
                elif low.endswith((".sqlite", ".db")):
                    n = _sqlite(path, r)
                else:
                    if not low.endswith((".patch", ".dat", ".lock", ".tmp")):
                        skipped.append(os.path.relpath(path, target))
                    continue
                if n:
                    changed[os.path.relpath(path, root)] = n
    key = fakenames.load_key(DATA_DIR)
    code = demo._code_hash()
    for n, repo in enumerate(ids, 1):
        build = os.path.join(target, f"repo{n}", demo._BUILD_FILE)
        if not os.path.isfile(build):
            continue
        repo_style = "generic" if repo in rules.get("strict_repos", []) else style
        with open(build, encoding="utf-8") as fh:
            meta = json.load(fh)
        meta["fingerprint"] = demo._fingerprint(repo, str(n), repo_style, rules, key, code)
        demo._dump(build, meta)
    return {"path": target, "files_changed": changed, "not_checked": skipped}


if __name__ == "__main__":
    args = sys.argv[1:]
    repos = args[args.index("--repo") + 1] if "--repo" in args else "1,2,3"
    out = patch(repos)
    print(json.dumps(out, indent=1))
