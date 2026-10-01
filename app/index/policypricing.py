"""Price a policy's exclusion rules against the index ("keeping out" numbers,/).

For a source and a set of ignore patterns, counts the files + bytes the source CURRENTLY backs up
(``pathx`` rows with ``in_latest``) that the patterns keep out - directly, or through a matched
ancestor directory (a matched directory prunes its whole subtree). This is the number KopiaUI
structurally cannot compute (it has no index): a rule priced at N files / X GB means those files
stop being protected from the next snapshot if the rule is added, and start being backed up again
if it is removed. A rule priced at 0 either matches nothing (dead) or has been active since before
its matches were ever backed up - both are honest index answers.

Matching follows kopia exactly (``core.ignorematch``): relative path with a leading slash and
``/`` separators, case-sensitive, anchored to the policy's own directory (global rules re-anchor
to every source root they apply to). A source's own rule list replaces the global one entirely -
never concatenates (kopia ``policy_merge.go``), verified against kopia source and live data.

Fast path: each pattern is translated to a RE2 regex and evaluated by DuckDB
``regexp_full_match`` with a cheap LIKE prefilter on literal fragments. Negated patterns (or the
rare shapes the regex translation declines) fall back to the ported matcher over one full fetch,
simulating kopia's ignorefs rule state machine in rule order - correct, but noticeably slower and
flagged as ``slow_path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid

from config import DATA_DIR

from ..core import paths as pathrules
from ..core import rulelayers as rlayers
from ..core.applog import get_logger
from ..core.ignorematch import compile_pattern, file_match_regex
from . import sidecache
from .queries import dir_scope_sql, like_literal, scope_sql
from .resolve import src_id as resolve_src_id
from .store import read_cursor

log = get_logger("policypricing")

_EXAMPLE_CAP = 5000           # most examples one LIVE request may ask for (the file list)
_CACHE_EXAMPLES = 500         # what a cached price keeps; a request for more recomputes
_CACHE_TTL = 1800.0           # numbers move only when the index or the policy moves
_CACHE_MAX = 4096
_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()

_BATCH = 20                   # patterns per FILTER-batched scan (one pathx + one dir scan each)
_LIVE_PRICE_CAP = 32          # above this, live pricing is refused - read the nightly numbers

# Single-flight for expensive multi-source rollups (metrics.py's build-lock shape): concurrent
# identical callers block on the same lock and then hit the price cache the leader populated.
_ROLLUP_LOCKS: dict[tuple, threading.Lock] = {}
_ROLLUP_LOCKS_LOCK = threading.Lock()


def rollup_lock(key: tuple) -> threading.Lock:
    with _ROLLUP_LOCKS_LOCK:
        return _ROLLUP_LOCKS.setdefault(key, threading.Lock())


_META = "*?[]!\\"             # pattern metacharacters that end a literal run
_MIN_LITERAL = 3              # shorter fragments are poor prefilters


def invalidate(repo: str | None = None) -> None:
    """Drop cached pricing (called after a policy save changes the rules)."""
    with _CACHE_LOCK:
        if repo is None:
            _CACHE.clear()
        else:
            for key in [k for k in _CACHE if k[0] == repo]:
                _CACHE.pop(key, None)


def _cache_get(key: tuple):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            if time.time() - hit[0] < _CACHE_TTL:
                return hit[1]
            _CACHE.pop(key, None)
    return None


def _hit_covers(hit: dict, limit: int) -> bool:
    """A cached price serves a request only when its examples reach the request's limit (or the
    rule simply has fewer matches). Caching keeps a few hundred examples, so a 5,000-file list
    recomputes instead of silently returning a short list."""
    have = len(hit.get("examples") or [])
    return have >= min(limit, int(hit.get("files") or 0))


def _cache_put(key: tuple, value: dict) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:_CACHE_MAX // 4]:
                _CACHE.pop(stale, None)
        if len(value.get("examples") or []) > _CACHE_EXAMPLES:
            value = {**value, "examples": value["examples"][:_CACHE_EXAMPLES]}
        _CACHE[key] = (time.time(), value)


def _literal_fragments(pattern: str) -> list[str]:
    """Literal fragments of a pattern (longest first, max 3) for the LIKE prefilter. A fragment
    appears verbatim in every path the regex matches, so LIKE '%frag%' is a safe superset."""
    runs: list[str] = []
    cur: list[str] = []
    for ch in pattern:
        if ch in _META:
            if cur:
                runs.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        runs.append("".join(cur))
    runs = [r for r in runs if len(r) >= _MIN_LITERAL]
    runs.sort(key=len, reverse=True)
    return runs[:3]


def _rel_expr() -> str:
    """SQL expression converting ``pathx.full_path`` (or ``dir.dir_path``) to the matcher's
    form: relative to the source root, with a leading slash and ``/`` separators - exactly the
    text kopia feeds the matcher. Params: (root_len+1, sep, '/'). The ltrim runs AFTER the
    replace, so it always trims the forward slash the root strip left behind."""
    return "'/' || ltrim(replace(substr(full_path, ?), ?, '/'), ?)"


def _dir_rel_expr() -> str:
    return "'/' || ltrim(replace(substr(dir_path, ?), ?, '/'), ?)"


def _cond_for_inner(rel: str, rx: str, fragments: list[str], root_len: int, sep: str) -> tuple[str, list]:
    conds = [f"regexp_full_match({rel}, ?)"]
    params: list = [root_len + 1, sep, '/', rx]
    for frag in fragments:
        conds.append("full_path LIKE ? ESCAPE '!'")
        params.append("%" + like_literal(frag.replace("/", sep)) + "%")
    return " AND ".join(f"({c})" for c in conds), params


_MAT_PX = "px_priced"        # prefix of the per-call temp tables the batch scans read
_MAT_DIR = "dir_priced"


def _anchor_conds(cur, anchor: str | None, px_table: str, dir_table: str) -> tuple[str, list, str, list]:
    """``(px_sql, px_params, dir_sql, dir_params)`` restricting a price run to a folder's subtree.

    A rule list that lives on a folder INSIDE a source (a folder policy, or an ignore file) is
    anchored there: kopia applies it to that subtree only, and the matched text is relative to the
    folder. ``anchor=None`` prices at the source root - the original behaviour."""
    if not anchor:
        return "", [], "", []
    px_sql, px_params = scope_sql(cur, anchor, include_path=True, alias=px_table)
    dr_sql, dr_params = dir_scope_sql(cur, anchor, alias=dir_table)
    return px_sql, px_params, (dr_sql or "1 = 0"), dr_params


def _materialize_source(cur, src: int, ns: str) -> tuple[str, str]:
    """ One source's ``in_latest`` paths and its dir rows, as TEMP TABLES.

    DuckDB plans a 40-rule FILTER list over the multi-file Parquet view badly - 0.78 s for 567
    rows against 0.035 s for the same rows in a temp table, and superlinear in rule count, which
    is the 193 s ``policy_prices`` stage (memory trap 31). One materialization per source serves
    every batch and every example lookup. The names carry a per-call suffix and the caller
    drops them: two pricing calls share the read connection, and a fixed name lets one clobber the
    other's table mid-query - the same shape the analysis tables had."""
    px, dr = f"{_MAT_PX}_{ns}", f"{_MAT_DIR}_{ns}"
    cur.execute(f"CREATE OR REPLACE TEMP TABLE {px} AS "
                f"SELECT * FROM pathx WHERE src_id = ? AND in_latest", [src])
    cur.execute(f"CREATE OR REPLACE TEMP TABLE {dr} AS "
                f"SELECT * FROM dir WHERE src_id = ?", [src])
    return px, dr


def _price_batch_with_regex(cur, src: int, entries: list[list],
                            root_len: int, sep: str,
                            px_table: str = "pathx", dir_table: str = "dir",
                            anchor: str | None = None) -> None:
    """Price a batch of patterns in ONE pathx scan + ONE dir scan, writing files/bytes/dirs
    into ``entries`` ([pattern, rx, fragments, [files, bytes, dirs]]) in place.

    One combined pattern (every rule as an alternative) first keeps only the rows ANY rule
    matches - usually a small fraction - and each rule's own condition runs once per remaining
    row. Falls back to ``_price_batch_plain`` if the combined pattern is refused. ``anchor``
    restricts the scan to one folder's subtree AND makes the matched text relative to it
    (``root_len`` is the anchor's length then)."""
    import duckdb
    any_rx = "|".join(f"(?:{e[1]})" for e in entries)
    px_sql, px_ap, dr_sql, dr_ap = _anchor_conds(cur, anchor, px_table, dir_table)
    flags, fparams = [], []
    for i, (_pat, rx, frags, _acc) in enumerate(entries):
        parts = ["full_path LIKE ? ESCAPE '!'"] * len(frags) + ["regexp_full_match(rel, ?)"]
        fparams.extend("%" + like_literal(f.replace("/", sep)) + "%" for f in frags)
        fparams.append(rx)
        flags.append("(" + " AND ".join(f"({c})" for c in parts) + f") AS m{i}")
    agg = ", ".join(f"count(*) FILTER (WHERE m{i}), coalesce(sum(size) FILTER (WHERE m{i}), 0)"
                    for i in range(len(entries)))
    where = "src_id = ? AND in_latest" + (f" AND {px_sql}" if px_sql else "")
    dflags = ", ".join(f"(length(rel) > 1 AND regexp_full_match(rel, ?)) AS d{i}"
                       for i in range(len(entries)))
    dagg = ", ".join(f"count(*) FILTER (WHERE d{i})" for i in range(len(entries)))
    dwhere = "src_id = ?" + (f" AND {dr_sql}" if dr_sql else "")
    rel_p = [root_len + 1, sep, '/']
    try:
        # Positional params follow the SQL text: the rule flags precede the inner scan.
        row = cur.execute(
            f"SELECT {agg} FROM (SELECT size, {', '.join(flags)} FROM ("
            f"SELECT * FROM (SELECT size, full_path, {_rel_expr()} AS rel FROM {px_table} "
            f"WHERE {where}) WHERE regexp_full_match(rel, ?)))",
            fparams + rel_p + [src] + px_ap + [any_rx]).fetchone()
        drow = cur.execute(
            f"SELECT {dagg} FROM (SELECT {dflags} FROM ("
            f"SELECT * FROM (SELECT {_dir_rel_expr()} AS rel FROM {dir_table} WHERE {dwhere}) "
            f"WHERE regexp_full_match(rel, ?)))",
            [e[1] for e in entries] + rel_p + [src] + dr_ap + [any_rx]).fetchone()
    except duckdb.Error as exc:
        log.warning("pricing: combined pattern refused (%s) - pricing this batch rule by rule",
                    str(exc)[:200])
        _price_batch_plain(cur, src, entries, root_len, sep, px_table, dir_table, anchor)
        return
    for i, entry in enumerate(entries):
        entry[3][0] = int(row[i * 2] or 0)
        entry[3][1] = int(row[i * 2 + 1] or 0)
        entry[3][2] = int(drow[i] or 0)


def _price_batch_plain(cur, src: int, entries: list[list], root_len: int, sep: str,
                       px_table: str = "pathx", dir_table: str = "dir",
                       anchor: str | None = None) -> None:
    """Every rule's condition over every row - the fallback for ``_price_batch_with_regex``.

    Each pattern's condition appears TWICE in the pathx SELECT (count + sum FILTER pairs) and
    the FILTER aggregates come BEFORE the WHERE clause in the SQL text - so per-pattern params
    are passed twice and ``src_id``'s param lands LAST."""
    rel = _rel_expr()
    drel = _dir_rel_expr()
    px_sql, px_ap, dr_sql, dr_ap = _anchor_conds(cur, anchor, px_table, dir_table)
    conds, pparams, dparams = [], [], []
    for _pat, rx, frags, _acc in entries:
        c, p = _cond_for_inner(rel, rx, frags, root_len, sep)
        conds.append(c)
        pparams.extend(p)
        pparams.extend(p)                      # count + sum aggregates repeat the condition
        dparams.extend([root_len + 1, sep, '/', root_len + 1, sep, '/', rx])
    sel = ", ".join(
        f"count(*) FILTER (WHERE {c}) AS f{i}, coalesce(sum(size) FILTER (WHERE {c}), 0) AS b{i}"
        for i, c in enumerate(conds))
    where = "src_id = ? AND in_latest" + (f" AND {px_sql}" if px_sql else "")
    row = cur.execute(f"SELECT {sel} FROM {px_table} WHERE {where}",
                      pparams + [src] + px_ap).fetchone()
    dsel = ", ".join(
        f"count(*) FILTER (WHERE length({drel}) > 1 AND regexp_full_match({drel}, ?)) AS d{i}"
        for i in range(len(entries)))
    dwhere = "src_id = ?" + (f" AND {dr_sql}" if dr_sql else "")
    drow = cur.execute(f"SELECT {dsel} FROM {dir_table} WHERE {dwhere}",
                       dparams + [src] + dr_ap).fetchone()
    for i, entry in enumerate(entries):
        entry[3][0] = int(row[i * 2] or 0)
        entry[3][1] = int(row[i * 2 + 1] or 0)
        entry[3][2] = int(drow[i] or 0)


def _machine(rules: list, text: str, is_dir: bool) -> tuple[bool, int | None]:
    """kopia's ignorefs rule state machine (ignorefs.go shouldIncludeByName) for ONE path:
    rules apply in order; once a path is ignored only negated rules are consulted and vice
    versa. Returns (ignored, index of the rule that flipped it to ignored)."""
    should = False
    who = None
    for i, pat in enumerate(rules):
        if (not should and not pat.negated) or (should and pat.negated):
            m = pat.match(text, is_dir)
            if m != should:
                should = m
                who = i if m else None
    return should, who


def _to_rel(full_path: str, root: str, sep: str) -> str:
    rel = full_path[len(root):] if full_path.startswith(root) else full_path
    return "/" + rel.replace(sep, "/").lstrip("/")


def _price_with_matcher(repo: str, source_key: str, patterns: list[str],
                        limit: int, anchor: str | None = None) -> list[dict]:
    """Slow path: one fetch of the source's in_latest paths + dirs, matched by the ported
    matcher with the full rule state machine (negated rules only mean something in sequence).
    Directory evaluations are memoized; descendants of an ignored directory are attributed to
    the rule that ignored it. ``anchor`` restricts the fetch to one folder's subtree and makes
    the matched text relative to that folder."""
    sep = pathrules.sep(source_key)
    root = anchor or pathrules.source_root(source_key)
    fold = not pathrules.case_sensitive(source_key)
    rules = [compile_pattern(p) for p in patterns]
    files: list[tuple[str, int]] = []
    dirs: list[str] = []
    cur = read_cursor(repo)
    try:
        src = resolve_src_id(cur, source_key)
        if src is not None:
            files = cur.execute(
                "SELECT full_path, size FROM pathx WHERE src_id = ? AND in_latest",
                [src]).fetchall()
            dirs = [r[0] for r in cur.execute(
                "SELECT dir_path FROM dir WHERE src_id = ?", [src]).fetchall()]
    finally:
        cur.close()
    if anchor:
        files = [f for f in files if rlayers.under(f[0], anchor, sep, fold)]
        dirs = [d for d in dirs if rlayers.under(d, anchor, sep, fold)]

    per_rule: list[dict] = [{"pattern": p, "files": 0, "bytes": 0, "dirs": 0,
                             "examples": []} for p in patterns]
    if src is None:
        return per_rule

    dir_memo: dict[str, tuple[bool, int | None]] = {}

    def dir_state(dpath: str) -> tuple[bool, int | None]:
        got = dir_memo.get(dpath)
        if got is None:
            got = _machine(rules, _to_rel(dpath, root, sep), is_dir=True)
            dir_memo[dpath] = got
        return got

    for dpath in dirs:
        ignored, who = dir_state(dpath)
        if ignored and who is not None and _to_rel(dpath, root, sep) != "/":
            per_rule[who]["dirs"] += 1

    # A file is kept out if an ancestor directory is ignored (the walk never descends), else if
    # the file itself is ignored. Ancestors ABOVE the source root never match: their relative
    # form is empty or '/', and kopia never feeds the walk root to the matcher.
    for full_path, size in files:
        rel = _to_rel(full_path, root, sep)
        if rel == "/":
            continue
        kept_out = False
        who = None
        segs = rel[1:].split("/")
        for i in range(1, len(segs)):
            ignored, w = dir_state(root + sep + sep.join(segs[:i]))
            if ignored:
                kept_out, who = True, w
                break
        if not kept_out:
            ignored, w = _machine(rules, rel, is_dir=False)
            if ignored:
                kept_out, who = True, w
        if kept_out and who is not None:
            item = per_rule[who]
            item["files"] += 1
            item["bytes"] += int(size or 0)
            if len(item["examples"]) < limit:
                item["examples"].append((full_path, int(size or 0)))

    for item in per_rule:
        item["examples"].sort(key=lambda e: e[1], reverse=True)
        item["examples"] = item["examples"][:limit]
    return per_rule


def price_patterns(repo: str, source_key: str, patterns: list[str], limit: int = 10,
                   use_cache: bool = True, anchor: str | None = None) -> dict:
    """Price each pattern against one source's current index view.

    ``anchor`` prices a rule list that lives on a folder INSIDE the source (a folder policy, or an
    ignore file): the scan is restricted to that folder's subtree and the matched text is relative
    to the folder, which is where kopia anchors it.

    Returns ``{"source", "src_found", "slow_path", "results": [...]}`` where each result carries
    ``pattern, files, bytes, dirs, examples, total_examples, capped, error, negated``.
    ``files`` counts pathx rows (in_latest); ``bytes`` sums their latest sizes; ``dirs`` counts
    matched directories whose subtrees are therefore kept out too. Examples are biggest-first
    and capped at ``limit`` (the honest totals are always exact).
    """
    limit = max(0, min(int(limit or 0), _EXAMPLE_CAP))
    akey = anchor or ""
    keyed = [(p, (repo, source_key, akey, p)) for p in patterns]
    results: list = [None] * len(keyed)
    uncached: list[int] = []

    for idx, (_pat, key) in enumerate(keyed):
        hit = _cache_get(key) if use_cache else None
        if hit is not None and _hit_covers(hit, limit):
            results[idx] = (idx, dict(hit))
        else:
            uncached.append(idx)

    slow = False
    if uncached:
        computed = _compute(repo, source_key, [keyed[i][0] for i in uncached],
                            anchor=anchor, limit=limit)
        slow = bool(computed["slow_path"])
        for pos, idx in enumerate(uncached):
            item = computed["results"][pos]
            if use_cache:
                _cache_put(keyed[idx][1], item)
            results[idx] = (idx, dict(item))

    out: list[dict] = [None] * len(patterns)  # type: ignore[list-item]
    for idx, item in results:
        item = dict(item)
        examples = item.get("examples") or []
        item["total_examples"] = item.get("files", 0) if not item.get("error") else 0
        item["examples"] = [{"path": p, "size": s} for p, s in examples[:limit]]
        item["capped"] = item["total_examples"] > limit
        out[idx] = item
    return {"source": source_key, "src_found": True, "slow_path": slow, "results": out}


def price_scope(repo: str, scope: list[dict], patterns: list[str], limit: int = 10,
                use_cache: bool = True) -> dict:
    """Price a rule set over every backup it reaches, each at its own anchor.

    ``scope`` comes from ``rulelayers.scope_for_target`` (a policy target) or
    ``rulelayers.folder_scope`` (a rule list anchored at one folder). ``results`` holds the merged
    totals in the single-source shape; ``breakdown`` names each source's anchor and contribution,
    which is the "keeps out N from 2 backups: A, B" line."""
    merged: dict[str, dict] = {}
    breakdown: list[dict] = []
    slow = False
    for entry in scope:
        source = entry["source"]
        anchor = entry.get("anchor")
        out = price_patterns(repo, source, patterns, limit=limit, use_cache=use_cache, anchor=anchor)
        slow = slow or bool(out.get("slow_path"))
        totals: dict[str, dict] = {}
        for res in out["results"]:
            item = merged.setdefault(res["pattern"], {
                "pattern": res["pattern"], "files": 0, "bytes": 0, "dirs": 0, "examples": [],
                "error": res["error"], "negated": res["negated"]})
            if res["error"]:
                continue
            item["files"] += res["files"]
            item["bytes"] += res["bytes"]
            item["dirs"] += res["dirs"]
            item["examples"].extend(res["examples"])
            totals[res["pattern"]] = {"files": res["files"], "bytes": res["bytes"],
                                      "dirs": res["dirs"]}
        breakdown.append({"source": source, "anchor": anchor, "layer": entry.get("layer"),
                          "results": totals})
    results: list[dict] = []
    for item in merged.values():
        item["examples"].sort(key=lambda e: e["size"], reverse=True)
        item["examples"] = item["examples"][:limit]
        item["total_examples"] = item["files"]
        item["capped"] = item["files"] > limit
        results.append(item)
    order = {p: i for i, p in enumerate(patterns)}
    results.sort(key=lambda r: order.get(r["pattern"], 0))
    sources = list(dict.fromkeys(e["source"] for e in scope))
    layers = {"root": list(dict.fromkeys(e["source"] for e in scope
                                         if e.get("layer") == rlayers.KIND_ROOT)),
              "folder": list(dict.fromkeys(e["source"] for e in scope
                                           if e.get("layer") != rlayers.KIND_ROOT))}
    return {"scope": {"sources": sources, "layers": layers},
            "results": results, "breakdown": breakdown, "slow_path": slow}


def _compute(repo: str, source_key: str, patterns: list[str],
             anchor: str | None = None, limit: int = _EXAMPLE_CAP) -> dict:
    """Uncached computation, collecting up to ``limit`` examples per rule (the totals are always
    exact; responses slice down, the cache keeps fewer).

    The fast path prices patterns in BATCHES - one pathx scan + one dir scan per ``_BATCH``
    patterns instead of one scan per pattern (the per-pattern rescan was the dominant live-path
    cost, ~432s extrapolated on the Global rollup). The fast/slow decision stays all-or-nothing
    per batch: if ANY valid pattern needs the ported matcher (negation, POSIX classes), the
    whole batch goes through the sequence simulation - per-rule numbers must stay comparable
    across one rule list, because negated rules change what the positive rules around them
    mean. Invalid patterns get error results and never enter either path."""
    results: list = [None] * len(patterns)
    valid_idx: list[int] = []
    valid_pats: list[str] = []
    for i, pat in enumerate(patterns):
        try:
            compiled = compile_pattern(pat)
        except ValueError as exc:
            results[i] = {"pattern": pat, "files": 0, "bytes": 0, "dirs": 0, "examples": [],
                          "error": str(exc), "negated": False}
            continue
        valid_idx.append(i)
        valid_pats.append((pat, compiled))

    if not valid_pats:
        return {"results": results, "slow_path": False}

    entries = [[pat, file_match_regex(pat), _literal_fragments(pat), [0, 0, 0]]
               for pat, _c in valid_pats]
    if not all(e[1] is not None for e in entries):
        got = _price_with_matcher(repo, source_key, [p for p, _ in valid_pats], limit,
                                  anchor=anchor)
        for pos, i in enumerate(valid_idx):
            item = dict(got[pos])
            item["error"] = None
            item["negated"] = valid_pats[pos][1].negated
            results[i] = item
        return {"results": results, "slow_path": True}

    sep = pathrules.sep(source_key)
    root = anchor or pathrules.source_root(source_key)
    mat: tuple[str, str] | None = None
    cur = read_cursor(repo)
    try:
        src = resolve_src_id(cur, source_key)
        if src is None:
            log.debug("pricing: source %r not found in repo %s index", source_key, repo)
            for i, (pat, compiled) in zip(valid_idx, valid_pats):
                results[i] = {"pattern": pat, "files": 0, "bytes": 0, "dirs": 0, "examples": [],
                              "error": None, "negated": compiled.negated}
            return {"results": results, "slow_path": False}
        # one batch is a single scan either way; more than one is where the temp tables pay.
        if len(entries) > _BATCH:
            mat = _materialize_source(cur, src, uuid.uuid4().hex[:8])
            px, dr = mat
        else:
            px, dr = "pathx", "dir"
        for start in range(0, len(entries), _BATCH):
            _price_batch_with_regex(cur, src, entries[start:start + _BATCH], len(root), sep,
                                    px_table=px, dir_table=dr, anchor=anchor)
        for i, (pat, compiled), entry in zip(valid_idx, valid_pats, entries):
            files, total, dirs = entry[3]
            examples: list = []
            if files and limit:  # examples only where the rule actually matches
                cond, cparams = _cond_for_inner(_rel_expr(), entry[1], entry[2], len(root), sep)
                px_sql, px_ap, _ds, _dp = _anchor_conds(cur, anchor, px, dr)
                extra = f" AND {px_sql}" if px_sql else ""
                examples = cur.execute(
                    f"SELECT full_path, size FROM {px} WHERE src_id = ? AND in_latest AND {cond}"
                    f"{extra} ORDER BY size DESC LIMIT ?",
                    [src] + cparams + px_ap + [limit]).fetchall()
            results[i] = {"pattern": pat, "files": files, "bytes": total, "dirs": dirs,
                          "examples": examples, "error": None, "negated": compiled.negated}
        return {"results": results, "slow_path": False}
    finally:
        for name in mat or ():
            try:
                cur.execute(f"DROP TABLE IF EXISTS {name}")
            except Exception:  # noqa: BLE001 - cleanup must never mask the result
                pass
        cur.close()


# ---- nightly-stored pricing ( half 2) -------------------------------------------
# Whole-policy pricing runs in the worker as a stored stage; the tab reads the numbers
# instantly (the serve-stale-recompute-in-background rule). A stored number ages gracefully:
# after a policy save the file keeps serving with its old ts until the next nightly recomputes.
# LIVE pricing remains for the one case no cache can cover - a rule the user is about to add
# (and small rule sets generally, capped at _LIVE_PRICE_CAP per request).

# v2: the fingerprint skip landed as KL70/KL71 changed pricing semantics - a v1 file (or any
# file written by older pricing code) must read as absent so the first post-merge nightly
# reprices everything once instead of fingerprint-reusing old-code prices.
# v3: a target's price now covers every backup its rules reach, including the ones whose walk
# passes through its folder () - old numbers were priced against the owning source only.
_STORE_VERSION = 3

# Ignore files (): rules read from dot-ignore files, priced over every walk that reaches
# their folder. The budget stops PRICING (never discovery/reading) on an outlier repo.
_IGNORE_NAME = "policy_ignore_files_repo%s"
_IGNORE_VERSION = 1
_IGNORE_PRICE_BUDGET = 600.0


def stored_name(repo: str) -> str:
    return f"policy_prices_repo{repo}"


def stored(repo: str) -> dict | None:
    """The parsed stored-prices payload, or None when the worker has not produced one yet."""
    data = sidecache.read(stored_name(repo), base=DATA_DIR)
    if isinstance(data, dict) and data.get("version") == _STORE_VERSION:
        return data
    return None


def _rules_hash(rules: list) -> str:
    return hashlib.md5(json.dumps(rules).encode()).hexdigest()


def _snapset_hashes(repo: str) -> dict[str, str]:
    """Per-source hash of the INDEXED snapshot-id set (isnaps). A price's inputs - the source's
    in_latest paths+sizes and its dir set - are a function of (rule list, indexed snapshot set):
    the builder carries an unchanged source verbatim, so if neither the rules nor the snapshot
    set changed, last night's numbers are still exact. This is the skip fingerprint."""
    cur = read_cursor(repo)
    try:
        rows = cur.execute(
            "SELECT source, snapshot_id FROM isnaps ORDER BY source, snapshot_id").fetchall()
    finally:
        cur.close()
    digests: dict[str, "hashlib._Hash"] = {}
    for source, sid in rows:
        d = digests.setdefault(source, hashlib.md5())
        d.update((sid or "").encode())
    return {k: d.hexdigest() for k, d in digests.items()}


def precompute(repo: str) -> dict:
    """ worker stage: price EVERY policy's own rules over every backup its rules reach
    (its own source and sources inheriting its list at their roots, plus - - every source
    whose walk passes through the policy's folder, anchored there; folds the ignore
    files' rules in afterwards). The tab reads the result instantly.

    Skip-when-unchanged (): a (target, source, anchor) price is reused from the previous
    night when the rule list's hash and that source's indexed snapshot-set hash are unchanged -
    on a quiet night this skips most of the stage. Returns the stored payload."""
    from ..core.policy import (
        _GLOBAL_TARGET,
        effective_ignore_origin,
        load_defined_policies,
        load_policies,
        refresh_defined_policies,
    )

    t0 = time.time()
    refresh_defined_policies(repo, force=True)   # one export; ownership must reflect now
    defined = load_defined_policies(repo).get("policies") or {}
    effective = load_policies(repo).get("policies") or {}

    prev = stored(repo) or {}
    prev_fp: dict = prev.get("fp") or {}
    prev_rules: dict = prev.get("rules") or {}
    prev_scope: dict = prev.get("scope") or {}
    snapsets = _snapset_hashes(repo)

    rules_out: dict[str, dict] = {}
    scope_out: dict[str, dict] = {}
    fp_src: dict[str, dict] = {}
    fp_gc: dict[str, dict] = {}
    fp_scope: dict[str, dict] = {}
    global_scope: list[str] = []
    reused = 0
    priced = 0
    for target in sorted(defined):
        if target == _GLOBAL_TARGET:
            continue
        own = ((defined.get(target) or {}).get("files") or {}).get("ignore") or []
        if not own:
            continue
        scope = rlayers.scope_for_target(repo, target, defined)
        if not scope:
            continue
        own_h = _rules_hash(own)
        prior = (prev_fp.get("scope") or {}).get(target) or {}
        keys = {f"{e['source']}|{e['anchor'] or ''}": e for e in scope}
        reusable = (target in prev_rules and set(prior) == set(keys)
                    and all(prior[k].get("rules") == own_h
                            and prior[k].get("snaps") == snapsets.get(k.split("|")[0], "")
                            for k in keys))
        if reusable:
            rules_out[target] = prev_rules[target]
            scope_out[target] = prev_scope.get(target) or {}
            reused += 1
        else:
            out = price_scope(repo, scope, own, limit=0)
            priced += 1
            rules_out[target] = {
                r["pattern"]: {"files": r["files"], "bytes": r["bytes"], "dirs": r["dirs"],
                               "negated": r["negated"]}
                for r in out["results"] if not r["error"]}
            scope_out[target] = {"sources": out["scope"]["sources"],
                                 "layers": out["scope"]["layers"],
                                 "breakdown": out["breakdown"]}
            log.info("policy prices repo=%s target=%s rules=%d scope=%d", repo, target,
                     len(rules_out[target]), len(scope))
        fp_scope[target] = {k: {"rules": own_h, "snaps": snapsets.get(k.split("|")[0], "")}
                            for k in keys}
        for e in scope:
            if e.get("layer") == rlayers.KIND_ROOT:
                fp_src[e["source"]] = {"own": own_h if e["source"] == target else "",
                                       "snaps": snapsets.get(e["source"], "")}

    for key in sorted(effective):
        origin, _owner = effective_ignore_origin(repo, key, defined)
        if origin == "global":
            global_scope.append(key)

    glob = ((defined.get(_GLOBAL_TARGET) or {}).get("files") or {}).get("ignore") or []
    if glob and global_scope:
        glob_h = _rules_hash(glob)
        glob_ok = prev_fp.get("global") == glob_h
        merged: dict[str, list[int]] = {p: [0, 0, 0] for p in glob}
        negated = {p: compile_pattern(p).negated for p in glob}
        prev_gc: dict = prev_fp.get("gc") or {}
        breakdown = []
        for key in global_scope:
            snaps_h = snapsets.get(key, "")
            prior = (prev_fp.get("src") or {}).get(key) or {}
            contrib = prev_gc.get(key) if (glob_ok and prior.get("snaps") == snaps_h) else None
            if contrib is not None:
                reused += 1
            else:
                out = price_patterns(repo, key, glob, limit=0)
                priced += 1
                contrib = {r["pattern"]: [r["files"], r["bytes"], r["dirs"]]
                           for r in out["results"] if not r["error"]}
                log.info("policy prices repo=%s global rollup priced %s", repo, key)
            fp_gc[key] = contrib
            fp_src[key] = {"own": "", "snaps": snaps_h}
            breakdown.append({"source": key, "anchor": None, "layer": rlayers.KIND_ROOT,
                              "results": {p: {"files": t[0], "bytes": t[1], "dirs": t[2]}
                                          for p, t in contrib.items()}})
            for p, triplet in contrib.items():
                m = merged.get(p)
                if m is not None:
                    m[0] += triplet[0]
                    m[1] += triplet[1]
                    m[2] += triplet[2]
        rules_out["global"] = {
            p: {"files": m[0], "bytes": m[1], "dirs": m[2], "negated": negated[p]}
            for p, m in merged.items()}
        scope_out["global"] = {"sources": global_scope,
                               "layers": {"root": global_scope, "folder": []},
                               "breakdown": breakdown}
        log.info("policy prices repo=%s global rollup: %d rules x %d sources",
                 repo, len(glob), len(global_scope))

    payload = {"version": _STORE_VERSION, "ts": time.time(), "rules": rules_out,
               "scope": scope_out,
               "fp": {"global": _rules_hash(glob) if glob else "", "src": fp_src, "gc": fp_gc,
                      "scope": fp_scope}}
    if not sidecache.write(stored_name(repo), payload, base=DATA_DIR):
        log.error("policy prices repo=%s store write failed", repo)
        return payload
    log.info("policy prices repo=%s stored (%d targets, %.1fs, %d fingerprint reuse, %d priced)",
             repo, len(rules_out), time.time() - t0, reused, priced)
    return payload


def stored_for_target(repo: str, target: str) -> dict | None:
    """Stored numbers for one target, shaped like the live price endpoint's response so the UI
    renders both with one path. None = nothing stored yet (fresh install / first nightly)."""
    data = stored(repo)
    if data is None:
        return None
    key = "global" if target == "global" else target
    rules = (data.get("rules") or {}).get(key)
    meta = (data.get("scope") or {}).get(key) or {}
    scope = {"sources": meta.get("sources") or [], "layers": meta.get("layers") or {}}
    breakdown = meta.get("breakdown") or []
    if rules is None:
        return {"target": target, "stored": True, "ts": data.get("ts"), "results": [],
                "scope": scope, "breakdown": breakdown}
    results = [{
        "pattern": p, "files": v.get("files", 0), "bytes": v.get("bytes", 0),
        "dirs": v.get("dirs", 0), "examples": [], "total_examples": v.get("files", 0),
        "capped": False, "error": None, "negated": bool(v.get("negated")),
    } for p, v in rules.items()]
    return {"target": target, "stored": True, "ts": data.get("ts"), "results": results,
            "scope": scope, "breakdown": breakdown}


# ---- ignore files () -------------------------------------------------------------

def stored_ignore_files(repo: str) -> dict | None:
    """The nightly's dot-ignore-file payload (rules + what they keep out), or None."""
    data = sidecache.read(_IGNORE_NAME % repo, base=DATA_DIR)
    if isinstance(data, dict) and data.get("version") == _IGNORE_VERSION:
        return data
    return None


def _name_in_effect(repo: str, source: str, folder: str, name: str, defined: dict) -> bool:
    names = rlayers.dot_names_at(repo, source, folder, defined)
    case_sensitive = pathrules.case_sensitive(source)
    return any(rlayers.name_matches(n, name or "", case_sensitive) for n in names)


def precompute_ignore_files(repo: str) -> dict:
    """Worker stage (): discover the dot-ignore files (index, plus disk for local sources),
    read the ones whose content changed, and price each file's rules over every backup whose walk
    reaches its folder. Files whose name is not in the folder's effective dot-ignore list are
    listed but priced at zero - kopia never reads them.

    A budget (``_IGNORE_PRICE_BUDGET``) stops PRICING on an outlier repo; discovery and rule text
    are always complete."""
    from ..core.policy import load_defined_policies

    t0 = time.time()
    defined = load_defined_policies(repo).get("policies") or {}
    ignore_map = rlayers.build_map(repo)
    out: list[dict] = []
    budget_hit = False
    for entry in ignore_map.get("files") or []:
        path = entry.get("path") or ""
        source = entry.get("source") or ""
        sep = "\\" if "\\" in path else "/"
        folder = path.rsplit(sep, 1)[0] if sep in path else path
        rules = list(entry.get("rules") or [])
        scope = rlayers.folder_scope(repo, folder, user_host=rlayers.user_host_of(source))
        in_effect = [e for e in scope if _name_in_effect(repo, e["source"], folder,
                                                         entry.get("name") or "", defined)]
        item = {"path": path, "name": entry.get("name"), "folder": folder, "source": source,
                "from": entry.get("from"), "readable": not entry.get("error"),
                "error": entry.get("error"), "rules": rules, "rule_count": len(rules),
                "in_effect": bool(in_effect), "files": 0, "bytes": 0, "dirs": 0,
                "by_source": [], "priced": False, "ts": time.time()}
        if rules and in_effect and not entry.get("error"):
            if time.time() - t0 > _IGNORE_PRICE_BUDGET:
                budget_hit = True
                item["priced"] = False
            else:
                res = price_scope(repo, in_effect, rules, limit=0)
                item["files"] = sum(r["files"] for r in res["results"] if not r["error"])
                item["bytes"] = sum(r["bytes"] for r in res["results"] if not r["error"])
                item["dirs"] = sum(r["dirs"] for r in res["results"] if not r["error"])
                item["by_source"] = [
                    {"source": b["source"], "anchor": b["anchor"],
                     "files": sum(v["files"] for v in (b.get("results") or {}).values()),
                     "bytes": sum(v["bytes"] for v in (b.get("results") or {}).values())}
                    for b in res["breakdown"]]
                item["priced"] = True
        out.append(item)
    payload = {"version": _IGNORE_VERSION, "ts": time.time(), "files": out,
               "budget_hit": budget_hit}
    if not sidecache.write(_IGNORE_NAME % repo, payload, base=DATA_DIR):
        log.error("ignore files repo=%s store write failed", repo)
    log.info("ignore files repo=%s: %d file(s), %d in effect, %.1fs%s", repo, len(out),
             sum(1 for i in out if i["in_effect"]), time.time() - t0,
             " (price budget hit)" if budget_hit else "")
    return payload


def ignore_files_for(repo: str, target: str | None = None) -> dict:
    """The Policies tab's read-only list of dot-ignore files, filtered to one target's folder."""
    data = stored_ignore_files(repo)
    if data is None:
        return {"repo": repo, "target": target, "stored": False, "ts": None, "count": 0,
                "files": [], "detail": "no ignore files stored yet - available after the next "
                                       "index run"}
    files = list(data.get("files") or [])
    if target in ("global", "(global)"):
        target = None                     # Global covers every source (trap 37's boundary rule)
    if target:
        folder = target.split(":", 1)[1] if ":" in target else ""
        uh = rlayers.user_host_of(target)
        sep = "\\" if "\\" in folder else "/"
        fold = sep == "\\"
        files = [f for f in files
                 if rlayers.user_host_of(f.get("source") or "") == uh
                 and rlayers.under(f.get("folder") or "", folder, sep, fold)]
    return {"repo": repo, "target": target, "stored": True, "ts": data.get("ts"),
            "count": len(files), "files": files,
            "budget_hit": bool(data.get("budget_hit"))}
