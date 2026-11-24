"""Nightly exact top lists for the Search all-versions columns.

The on-demand columns (Stored / Frees, every version) need piece-by-piece maths per file, which is
fine for a page of rows and far too heavy for millions of paths every night. The nightly therefore
ranks the top ``rank_count`` entries exactly, using a sound upper bound to prune:

* **Stored** (per path): bound = the sum of the path's DISTINCT version objects' packed sizes
  (chunks shared between its own versions count once in the exact value, so the bound is never
  below it).
* **Frees** (per FILE - every holder path of one computer's path): bound = the sum of the
  bound above over the file's holders; removing a file can never free more than its holders store.

Descend the bounds, work out the exact value per candidate, stop when the next candidate's bound is
below the Nth exact value found. Frees is ``physical.frees_sql``, the file panel's Reclaimable; the
payload lists every holder path of a ranked file.

The result is a small payload in the side cache that Search reads (memoized per generation) to sort
by either column; rows outside the list keep their on-demand value and read "not ranked".
"""

from __future__ import annotations

import time

from config import DATA_DIR

from ..core.applog import get_logger
from . import sidecache
from .physical import _host_table, frees_sql
from .store import read_cursor

log = get_logger("rankpaths")

_NAME = "rank_paths_repo%s"
_VERSION = 2                 # 2: frees keyed by path, per file; 1 keyed it by content
DEFAULT_COUNT = 10000
_BOUND_BATCH = 2000          # paths per exact-math batch in the stored descent
_FREES_BATCH = 2000          # files per exact-math batch in the frees descent
_BUDGET_SECONDS = 900.0      # a pathological repo stops here; the payload is then marked inexact


def count() -> int:
    """How many entries to rank, from the Advanced setting (0 disables the step)."""
    try:
        from ..settings import load_settings
        n = int(((load_settings().get("kopialens") or {}).get("rank_count") or DEFAULT_COUNT))
    except Exception:  # noqa: BLE001 - a settings problem must not stop the nightly step
        return DEFAULT_COUNT
    return max(0, min(n, 200_000))


def _bounds(cur) -> None:
    """The cheap tables both descents read: a price per content, the (path, object) pairs, the
    per-path and per-content bounds, and both sorted orders."""
    cur.execute("""
        CREATE OR REPLACE TABLE _rank_price AS
        SELECT c.cid_id, coalesce(i.packed, s.packed, 0)::UBIGINT AS packed
        FROM cid c LEFT JOIN iphys i ON i.cid_id = c.cid_id
        LEFT JOIN csize s ON s.cid_id = c.cid_id""")
    cur.execute("CREATE OR REPLACE TABLE _rank_pair AS "
                "SELECT DISTINCT src_id, path_id, cid_id FROM filev")
    cur.execute("""
        CREATE OR REPLACE TABLE _rank_est AS
        SELECT p.path_id, sum(pr.packed)::UBIGINT AS est
        FROM _rank_pair p JOIN _rank_price pr ON pr.cid_id = p.cid_id
        GROUP BY p.path_id""")
    cur.execute("""
        CREATE OR REPLACE TABLE _rank_ord AS
        SELECT path_id AS key, est, row_number() OVER (ORDER BY est DESC, path_id) AS rn
        FROM _rank_est""")
    # One file = one (host, path): fk is the smallest path_id among its holders.
    cur.execute(f"""
        CREATE OR REPLACE TABLE _rank_fk AS
        SELECT p.path_id, p.src_id, min(p.path_id) OVER (PARTITION BY h.host, p.full_path) AS fk
        FROM pathx p JOIN {_host_table()} h ON h.src_id = p.src_id""")
    cur.execute("""
        CREATE OR REPLACE TABLE _rank_gest AS
        SELECT k.fk, sum(e.est)::HUGEINT AS gest
        FROM _rank_fk k JOIN _rank_est e ON e.path_id = k.path_id
        GROUP BY k.fk""")
    cur.execute("""
        CREATE OR REPLACE TABLE _rank_gord AS
        SELECT fk AS key, gest, row_number() OVER (ORDER BY gest DESC, fk) AS rn
        FROM _rank_gest""")


def _exact_stored(cur, out: str = "_rank_bst") -> None:
    """Exact Stored per path for the paths in ``_rank_batch``: the priced union of the chunks of
    every version object. A path holding an object the tree has not reached is left out - its
    number would be understated, and an understated number must never enter the ranking."""
    cur.execute("""
        CREATE OR REPLACE TABLE _rank_bobjs AS
        SELECT DISTINCT pb.path_id, pb.cid_id AS obj FROM _rank_pair pb
        JOIN _rank_batch b ON b.key = pb.path_id""")
    cur.execute("""
        CREATE OR REPLACE TABLE _rank_bch AS
        SELECT b.path_id, coalesce(tp.part_cid, b.obj) AS chunk,
               (c.content_id LIKE 'I%' AND tp.part_cid IS NULL) AS unknown
        FROM _rank_bobjs b JOIN cid c ON c.cid_id = b.obj
        LEFT JOIN tpart tp ON tp.iobj_cid = b.obj""")
    cur.execute(f"""
        CREATE OR REPLACE TABLE {out} AS
        SELECT d.path_id AS key, sum(pr.packed)::UBIGINT AS value
        FROM (SELECT DISTINCT path_id, chunk FROM _rank_bch
              WHERE NOT unknown
                AND path_id NOT IN (SELECT path_id FROM _rank_bch WHERE unknown)) d
        JOIN _rank_price pr ON pr.cid_id = d.chunk
        GROUP BY d.path_id""")


def _exact_frees(cur, out: str = "_rank_gf") -> None:
    """Exact Frees per FILE for the files (keyed ``fk``) in ``_rank_batch`` - ``physical.frees_sql``
    over each file's holders. Files with an unreadable split object are dropped."""
    targets = ("SELECT b.key AS t, k.src_id, k.path_id FROM _rank_batch b "
               "JOIN _rank_fk k ON k.fk = b.key")
    cur.execute(f"CREATE OR REPLACE TABLE {out} AS SELECT t AS key, value FROM ({frees_sql(targets)})")


def _descend(cur, *, order: str, bound: str, batch: int, n: int, label: str,
             exact_fn, out: str) -> tuple[list, bool, int, str]:
    """Walk a bound-ordered candidate list, keeping the top ``n`` exact values. Stops as soon as
    the next candidate's bound cannot reach the Nth exact value found. Returns
    ([(value, key)] best first, exact, candidates processed, how it ended)."""
    top: list[tuple[int, int]] = []
    rn = 0
    processed = 0
    total = cur.execute(f"SELECT max(rn) FROM {order}").fetchone()[0] or 0
    ended = "exhausted"
    t0 = time.time()
    while rn < total:
        cur.execute(f"""
            CREATE OR REPLACE TABLE _rank_batch AS
            SELECT key, rn FROM {order} WHERE rn > {int(rn)} ORDER BY rn LIMIT {batch}""")
        head = cur.execute("SELECT max(rn), count(*) FROM _rank_batch").fetchone()
        if not head or not head[1]:
            break
        rn = int(head[0])
        exact_fn(cur, out)
        rows = cur.execute(f"SELECT value, key FROM {out}").fetchall()
        processed += len(rows)
        top.extend((int(v), int(k)) for v, k in rows)
        top.sort(key=lambda kv: (-kv[0], kv[1]))
        del top[n:]
        if len(top) >= n:
            nxt = cur.execute(f"SELECT {bound} FROM {order} WHERE rn = ?", [rn + 1]).fetchone()
            if nxt is None or int(nxt[0]) < top[n - 1][0]:
                ended = "pruned"            # nothing left can reach the Nth exact value
                break
        if time.time() - t0 > _BUDGET_SECONDS:
            ended = "budget"
            log.warning("rank_paths: %s descent hit the %.0fs budget after %d candidates",
                        label, _BUDGET_SECONDS, processed)
            break
    return top, ended != "budget", processed, ended


def precompute(repo: str, n: int | None = None) -> dict:
    """Worker stage: build and store the ranked lists. Returns the payload."""
    n = count() if n is None else max(0, int(n))
    from .store import read_manifest
    gen = (read_manifest(repo) or {}).get("generation")
    if n <= 0:
        payload = {"version": _VERSION, "ts": time.time(), "n": 0, "exact": False,
                   "generation": gen, "stored": [], "frees": []}
        sidecache.write(_NAME % repo, payload, base=DATA_DIR)
        return payload
    t0 = time.time()
    cur = read_cursor(repo)
    try:
        _bounds(cur)
        stored, exact_s, done_s, end_s = _descend(cur, order="_rank_ord", bound="est",
                                                  batch=_BOUND_BATCH, n=n, label="stored",
                                                  exact_fn=_exact_stored, out="_rank_bst")
        frees, exact_f, done_f, end_f = _descend(cur, order="_rank_gord", bound="gest",
                                                 batch=_FREES_BATCH, n=n, label="frees",
                                                 exact_fn=_exact_frees, out="_rank_gf")
        # Every holder path of a ranked file carries its value (Search lists one row per source).
        holders: dict[int, list[int]] = {}
        if frees:
            import pyarrow as pa
            cur.register("_rank_top", pa.table({"fk": pa.array([k for _v, k in frees], pa.int64())}))
            for fk, pid in cur.execute("SELECT k.fk, k.path_id FROM _rank_fk k "
                                       "JOIN _rank_top t ON t.fk = k.fk").fetchall():
                holders.setdefault(int(fk), []).append(int(pid))
            cur.unregister("_rank_top")
    finally:
        cur.close()
    payload = {"version": _VERSION, "ts": time.time(), "n": n, "generation": gen,
               "exact": bool(exact_s and exact_f),
               "stored": [[k, v] for v, k in stored],
               "frees": [[pid, v] for v, k in frees for pid in sorted(holders.get(k, [k]))]}
    if not sidecache.write(_NAME % repo, payload, base=DATA_DIR):
        log.error("rank_paths repo=%s store write failed", repo)
    log.info("rank_paths repo=%s: %d stored (%s) + %d frees (%s) in %.1fs, exact=%s",
             repo, len(stored), end_s, len(frees), end_f, time.time() - t0, payload["exact"])
    return payload


def stored(repo: str) -> dict | None:
    """The stored payload, or None when the nightly has not built one (fresh install, first run)."""
    data = sidecache.read(_NAME % repo, base=DATA_DIR)
    if isinstance(data, dict) and data.get("version") == _VERSION:
        return data
    return None


_MEMO: dict[str, tuple] = {}


def current(repo: str) -> dict | None:
    """The payload for the CURRENT generation, memoized - the Search route asks per request.

    A payload whose generation is ABOVE the current one is dropped: a from-scratch rebuild
    (``data/repoN/`` deleted) restarts the id counters, so the old keys would name other paths.
    A payload from an earlier generation in the same chain stays valid - ids only ever grow."""
    from .store import read_manifest
    gen = (read_manifest(repo) or {}).get("generation")
    hit = _MEMO.get(repo)
    if hit is not None and hit[0] == gen:
        return hit[1]
    data = stored(repo)
    built = data.get("generation") if isinstance(data, dict) else None
    if isinstance(built, int) and isinstance(gen, int) and gen < built:
        data = None
    _MEMO[repo] = (gen, data)
    return data


def forget(repo: str) -> None:
    """Drop the stored lists (the index switched to an older copy, whose ids they may not match)."""
    sidecache.drop(_NAME % repo, base=DATA_DIR)
    _MEMO.pop(repo, None)


def ranked_values(repo: str, kind: str) -> dict[int, int]:
    """{key: bytes} for one ranked list, or {} when there is none (or it is not exact)."""
    data = current(repo)
    if not data or not data.get("exact"):
        return {}
    return {int(k): int(v) for k, v in data.get("stored" if kind == "stored" else "frees") or []}
