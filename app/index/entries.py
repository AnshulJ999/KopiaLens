"""Folder entries from the generation's tree, with a kopia fallback ( 4b/4c).

Every folder the last sync read lives in the tree, so a click reads the generation. A snapshot
created since that sync has folders the tree has never seen, and a click still has to answer - it
cannot wait for the nightly. Those come straight from kopia and are held in one small in-memory
LRU shared by every consumer (holding, the live tree, the restore listing, the folder panels);
the next nightly reads them properly and the LRU entry becomes redundant.

The LRU is deliberately NOT the walk cache. The walk cache is a writable sqlite database that is
deleted in step 7, and a read must never write store state; the price of the fallback is one
repeat kopia call after an app restart, which is the trade the plan accepts.

A read that FAILS is never cached and never returned as an empty folder - that is the rule that
kept a transient kopia failure from becoming permanent state (a folder that reads as empty today
would read as empty forever).
"""

from __future__ import annotations

import collections
import json
import sys
import threading

from ..core import repos
from ..core.applog import get_logger
from ..core.kopia import kopia
from . import resolve, tree
from .store import NoIndex, read_cursor

log = get_logger("entries")

BATCH = 800
# Click cache. Small and bounded per repo: this only has to cover the handful of folders a user
# clicks into a snapshot the nightly has not reached yet.
LRU_MAX_BYTES = 32 * 1024 * 1024

_LRU: dict[str, collections.OrderedDict] = {}   # repo -> {oid: (entries, bytes)}
_LRU_BYTES: dict[str, int] = {}
_LOCK = threading.Lock()

# t.cid_id first, then the entry columns in the order `_pack` reads them.
_SELECT = f"""SELECT t.cid_id, e.pos, e.name, e.etype, e.mode, e.size,
       {tree.ns_to_ts('e.mtime')} AS mtime, e.cid_id, e.s_maxtime,
       e.s_size, e.s_files, e.s_symlinks, e.s_dirs, e.s_incomplete, e.s_numfailed,
       e.s_numignorederrors, e.s_errors, e.extra
    FROM tdir t LEFT JOIN tent e ON e.dir_id = t.dir_id
    WHERE t.cid_id IN ({{ph}})
    ORDER BY t.cid_id, e.pos"""


def _bytes_of(entries: list[dict]) -> int:
    """Shallow size of one cached folder, for the LRU budget (folder sizes here range to 50k
    children, so an item count is not a RAM budget)."""
    total = sys.getsizeof(entries)
    for d in entries:
        total += sys.getsizeof(d)
        for k, v in d.items():
            total += sys.getsizeof(k) + sys.getsizeof(v)
    return total


def _lru_get(repo: str, oids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with _LOCK:
        lru = _LRU.get(repo)
        if not lru:
            return out
        for oid in oids:
            hit = lru.get(oid)
            if hit is not None:
                lru.move_to_end(oid)
                out[oid] = hit[0]
    return out


def _lru_put(repo: str, oid: str, entries: list[dict]) -> None:
    """Store the folder with its measured size.

    The size is kept beside the entries rather than recomputed on replace and on every eviction:
    `_bytes_of` walks every entry and every key, a folder here runs to 50k children, and the old
    shape did that walk inside the global lock - so one eviction stalled every repo's lookups.
    Same rule the byte-budget cache this replaces had to learn.
    """
    size = _bytes_of(entries)
    with _LOCK:
        lru = _LRU.setdefault(repo, collections.OrderedDict())
        old = lru.pop(oid, None)
        if old is not None:
            _LRU_BYTES[repo] = _LRU_BYTES.get(repo, 0) - old[1]
        lru[oid] = (entries, size)
        _LRU_BYTES[repo] = _LRU_BYTES.get(repo, 0) + size
        while _LRU_BYTES.get(repo, 0) > LRU_MAX_BYTES and lru:
            _, evicted = lru.popitem(last=False)
            _LRU_BYTES[repo] -= evicted[1]


def _parse(text: str) -> list[dict] | None:
    try:
        return json.loads(text).get("entries") or []
    except ValueError:          # never an empty folder: a parse failure is not an answer
        return None


def _from_tree(cur, id_of: dict[str, int]) -> dict[str, list[dict]]:
    """tent rows for the folder objects that resolved to a tree folder, keyed by object id."""
    rev = {cid: oid for oid, cid in id_of.items()}
    ids = sorted(set(id_of.values()))
    rows: list[tuple] = []
    for i in range(0, len(ids), 900):
        grp = ids[i:i + 900]
        rows += cur.execute(_SELECT.format(ph=",".join("?" * len(grp))), grp).fetchall()
    if not rows:
        return {}
    names = resolve.content_ids(cur, sorted({r[7] for r in rows if r[7] is not None}))
    out: dict[str, list[dict]] = {}
    for r in rows:
        oid = rev.get(r[0])
        if oid is None:
            continue
        # A row with no entry columns (the LEFT JOIN side) is a folder the tree holds that has
        # nothing in it. It must still appear: an absent object reads as "not in the tree" and
        # would send every click on an empty folder to kopia.
        bucket = out.setdefault(oid, [])
        if r[2] is None:
            continue
        etype = r[3]
        typ = tree.TYPES[etype] if etype is not None and 0 <= etype < len(tree.TYPES) else "?"
        entry = {
            "name": r[2],
            "type": typ,
            "mode": tree.MODES[r[4]] if r[4] is not None and 0 <= r[4] < len(tree.MODES) else "",
            "obj": names.get(r[7]) or "",
            "size": r[5],
            "mtime": r[6],
        }
        if typ == "d":
            # Folder sizes come from the entry's own summary - no deep walk, exactly as kopia's
            # `summ` did.
            entry["summ"] = {"maxtime": r[8], "size": r[9], "files": r[10], "symlinks": r[11],
                             "dirs": r[12], "incomplete": r[13], "numfailed": r[14],
                             "numignorederrors": r[15], "errors": r[16]}
        if r[17]:
            entry["extra"] = r[17]
        bucket.append(entry)
    return out


def _from_kopia(repo: str, oids: list[str]) -> dict[str, list[dict]]:
    """Objects the tree does not hold, straight from kopia. Reads nothing into the store."""
    out: dict[str, list[dict]] = {}
    if repos.via_server(repo):
        # `content show` is direct-only; `kopia show` reads any object through a server too.
        normal, indirect = [], list(oids)
    else:
        normal = [o for o in oids if not o.startswith("I")]
        indirect = [o for o in oids if o.startswith("I")]
    for i in range(0, len(normal), BATCH):
        group = normal[i:i + BATCH]
        res = kopia(repo, ["content", "show"] + group)
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        if res.returncode != 0:
            log.warning("entries: kopia content show answered %d of %d (rc=%d) - %d folder(s) "
                        "left unread and uncached", len(lines), len(group), res.returncode,
                        len(group) - len(lines))
        for oid, line in zip(group, lines):   # positional pairing, the documented rule
            entries = _parse(line)
            if entries is None:
                log.warning("entries: unparseable content show output for %s", oid)
                continue
            out[oid] = entries
            _lru_put(repo, oid, entries)
    for oid in indirect:
        # An indirect DIRECTORY needs `kopia show`; `content show` errors on it.
        res = kopia(repo, ["show", oid])
        entries = _parse(res.stdout) if res.returncode == 0 else None
        if entries is None:
            log.warning("entries: kopia show failed for %s (rc=%d)", oid, res.returncode)
            continue
        out[oid] = entries
        _lru_put(repo, oid, entries)
    return out


def children_of(repo: str, oids: list[str], *, cursor=None) -> dict[str, list[dict]]:
    """Entries for each folder object, keyed by the object id it was asked with.

    Tree first; anything the tree does not hold comes from kopia and is held in the shared LRU.
    An object that could not be read is ABSENT from the result, never present as an empty folder.
    """
    wanted = [o for o in dict.fromkeys(oids or []) if o]
    if not wanted:
        return {}
    out = _lru_get(repo, wanted)
    missing = [o for o in wanted if o not in out]
    if not missing:
        return out
    own = cursor is None
    try:
        cur = read_cursor(repo) if own else cursor
    except NoIndex:
        cur = None                 # never indexed (or reached through a server): kopia only
    try:
        if cur is not None:
            id_of = resolve.cid_ids(cur, missing)
            if id_of:
                out.update(_from_tree(cur, id_of))
        unresolved = [o for o in missing if o not in out]
        if unresolved:
            out.update(_from_kopia(repo, unresolved))
    finally:
        if own and cur is not None:
            cur.close()
    return out
