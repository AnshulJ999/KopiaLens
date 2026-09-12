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

import json
import os
import threading
import time

from config import DATA_DIR

from ..core import paths as pathrules
from ..core.applog import get_logger
from ..core.ignorematch import compile_pattern, file_match_regex
from .queries import like_literal
from .resolve import src_id as resolve_src_id
from .store import read_cursor

log = get_logger("policypricing")

_EXAMPLE_CAP = 500            # internal cache cap; requests slice below it
_CACHE_TTL = 1800.0           # numbers move only when the index or the policy moves
_CACHE_MAX = 4096
_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()

_BATCH = 40                   # patterns per FILTER-batched scan (one pathx + one dir scan each)
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


def _cache_put(key: tuple, value: list[dict]) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:_CACHE_MAX // 4]:
                _CACHE.pop(stale, None)
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


def _price_batch_with_regex(cur, src: int, entries: list[list],
                            root_len: int, sep: str) -> None:
    """Price a batch of patterns in ONE pathx scan + ONE dir scan, writing files/bytes/dirs
    into ``entries`` ([pattern, rx, fragments, [files, bytes, dirs]]) in place.

    Each pattern's condition appears TWICE in the pathx SELECT (count + sum FILTER pairs) and
    the FILTER aggregates come BEFORE the WHERE clause in the SQL text - so per-pattern params
    are passed twice and ``src_id``'s param lands LAST. Both orderings are pinned by
    test_filter_batch_matches_per_pattern."""
    rel = _rel_expr()
    drel = _dir_rel_expr()
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
    row = cur.execute(f"SELECT {sel} FROM pathx WHERE src_id = ? AND in_latest",
                      pparams + [src]).fetchone()
    dsel = ", ".join(
        f"count(*) FILTER (WHERE length({drel}) > 1 AND regexp_full_match({drel}, ?)) AS d{i}"
        for i in range(len(entries)))
    drow = cur.execute(f"SELECT {dsel} FROM dir WHERE src_id = ?", dparams + [src]).fetchone()
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
                        limit: int) -> list[dict]:
    """Slow path: one fetch of the source's in_latest paths + dirs, matched by the ported
    matcher with the full rule state machine (negated rules only mean something in sequence).
    Directory evaluations are memoized; descendants of an ignored directory are attributed to
    the rule that ignored it."""
    sep = pathrules.sep(source_key)
    root = pathrules.source_root(source_key)
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
            if len(item["examples"]) < _EXAMPLE_CAP:
                item["examples"].append((full_path, int(size or 0)))

    for item in per_rule:
        item["examples"].sort(key=lambda e: e[1], reverse=True)
        item["examples"] = item["examples"][:limit]
    return per_rule


def price_patterns(repo: str, source_key: str, patterns: list[str], limit: int = 10,
                   use_cache: bool = True) -> dict:
    """Price each pattern against one source's current index view.

    Returns ``{"source", "src_found", "slow_path", "results": [...]}`` where each result carries
    ``pattern, files, bytes, dirs, examples, total_examples, capped, error, negated``.
    ``files`` counts pathx rows (in_latest); ``bytes`` sums their latest sizes; ``dirs`` counts
    matched directories whose subtrees are therefore kept out too. Examples are biggest-first
    and capped at ``limit`` (the honest totals are always exact).
    """
    limit = max(0, min(int(limit or 0), _EXAMPLE_CAP))
    keyed = [(p, (repo, source_key, p)) for p in patterns]
    results: list = [None] * len(keyed)
    uncached: list[int] = []

    for idx, (_pat, key) in enumerate(keyed):
        hit = _cache_get(key) if use_cache else None
        if hit is not None:
            results[idx] = (idx, dict(hit))
        else:
            uncached.append(idx)

    slow = False
    if uncached:
        computed = _compute(repo, source_key, [keyed[i][0] for i in uncached])
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


def _compute(repo: str, source_key: str, patterns: list[str]) -> dict:
    """Uncached computation, always at the internal example cap (responses slice down).

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
        got = _price_with_matcher(repo, source_key, [p for p, _ in valid_pats], _EXAMPLE_CAP)
        for pos, i in enumerate(valid_idx):
            item = dict(got[pos])
            item["error"] = None
            item["negated"] = valid_pats[pos][1].negated
            results[i] = item
        return {"results": results, "slow_path": True}

    sep = pathrules.sep(source_key)
    root = pathrules.source_root(source_key)
    cur = read_cursor(repo)
    try:
        src = resolve_src_id(cur, source_key)
        if src is None:
            log.warning("pricing: source %r not found in repo %s index", source_key, repo)
            for i, (pat, compiled) in zip(valid_idx, valid_pats):
                results[i] = {"pattern": pat, "files": 0, "bytes": 0, "dirs": 0, "examples": [],
                              "error": None, "negated": compiled.negated}
            return {"results": results, "slow_path": False}
        for start in range(0, len(entries), _BATCH):
            _price_batch_with_regex(cur, src, entries[start:start + _BATCH], len(root), sep)
        for i, (pat, compiled), entry in zip(valid_idx, valid_pats, entries):
            files, total, dirs = entry[3]
            examples: list = []
            if files and _EXAMPLE_CAP:  # examples only where the rule actually matches
                cond, cparams = _cond_for_inner(_rel_expr(), entry[1], entry[2], len(root), sep)
                examples = cur.execute(
                    f"SELECT full_path, size FROM pathx WHERE src_id = ? AND in_latest AND {cond} "
                    f"ORDER BY size DESC LIMIT ?", [src] + cparams + [_EXAMPLE_CAP]).fetchall()
            results[i] = {"pattern": pat, "files": files, "bytes": total, "dirs": dirs,
                          "examples": examples, "error": None, "negated": compiled.negated}
        return {"results": results, "slow_path": False}
    finally:
        cur.close()


# ---- nightly-stored pricing ( half 2) -------------------------------------------
# Whole-policy pricing runs in the worker as a stored stage; the tab reads the numbers
# instantly (the serve-stale-recompute-in-background rule). A stored number ages gracefully:
# after a policy save the file keeps serving with its old ts until the next nightly recomputes.
# LIVE pricing remains for the one case no cache can cover - a rule the user is about to add
# (and small rule sets generally, capped at _LIVE_PRICE_CAP per request).

_STORE_VERSION = 1


def stored_path(repo: str) -> str:
    return os.path.join(DATA_DIR, f"policy_prices_repo{repo}.json")


def stored(repo: str) -> dict | None:
    """The parsed stored-prices file, or None when the worker has not produced one yet."""
    try:
        with open(stored_path(repo), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("version") == _STORE_VERSION else None
    except Exception:  # noqa: BLE001 - absent/corrupt store = "not priced yet", never an error
        return None


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def precompute(repo: str) -> dict:
    """ worker stage: price EVERY policy's own rules plus the global rollup and store
    the result for instant tab reads. Per source it is one batched scan set (the FILTER batch);
    the global rollup prices global's rules against each inheriting source and sums.
    Returns the stored payload."""
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

    rules_out: dict[str, dict] = {}
    global_scope: list[str] = []
    for key in sorted(effective):
        origin, _owner = effective_ignore_origin(repo, key, defined)
        if origin == "global":
            global_scope.append(key)
            continue
        own = ((defined.get(key) or {}).get("files") or {}).get("ignore") or []
        if not own:
            continue
        out = price_patterns(repo, key, own, limit=0)
        rules_out[key] = {
            r["pattern"]: {"files": r["files"], "bytes": r["bytes"], "dirs": r["dirs"],
                           "negated": r["negated"]}
            for r in out["results"] if not r["error"]}
        log.info("policy prices repo=%s source=%s rules=%d", repo, key, len(rules_out[key]))

    glob = ((defined.get(_GLOBAL_TARGET) or {}).get("files") or {}).get("ignore") or []
    if glob and global_scope:
        merged: dict[str, list[int]] = {p: [0, 0, 0] for p in glob}
        negated = {p: compile_pattern(p).negated for p in glob}
        for key in global_scope:
            out = price_patterns(repo, key, glob, limit=0)
            for r in out["results"]:
                if r["error"]:
                    continue
                m = merged[r["pattern"]]
                m[0] += r["files"]
                m[1] += r["bytes"]
                m[2] += r["dirs"]
        rules_out["global"] = {
            p: {"files": m[0], "bytes": m[1], "dirs": m[2], "negated": negated[p]}
            for p, m in merged.items()}
        log.info("policy prices repo=%s global rollup: %d rules x %d sources",
                 repo, len(glob), len(global_scope))

    payload = {"version": _STORE_VERSION, "ts": time.time(), "rules": rules_out}
    try:
        _atomic_write_json(stored_path(repo), payload)
    except Exception as exc:  # noqa: BLE001 - a store write must never fail the worker stage
        log.error("policy prices repo=%s store write failed: %s", repo, exc)
        return payload
    log.info("policy prices repo=%s stored (%d targets, %.1fs)", repo, len(rules_out),
             time.time() - t0)
    return payload


def stored_for_target(repo: str, target: str) -> dict | None:
    """Stored numbers for one target, shaped like the live price endpoint's response so the UI
    renders both with one path. None = nothing stored yet (fresh install / first nightly)."""
    data = stored(repo)
    if data is None:
        return None
    key = "global" if target == "global" else target
    rules = (data.get("rules") or {}).get(key)
    if rules is None:
        return {"target": target, "stored": True, "ts": data.get("ts"), "results": []}
    results = [{
        "pattern": p, "files": v.get("files", 0), "bytes": v.get("bytes", 0),
        "dirs": v.get("dirs", 0), "examples": [], "total_examples": v.get("files", 0),
        "capped": False, "error": None, "negated": bool(v.get("negated")),
    } for p, v in rules.items()]
    return {"target": target, "stored": True, "ts": data.get("ts"), "results": results}
