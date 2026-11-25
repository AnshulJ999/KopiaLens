"""Bulk id allocation for Parquet generations (plan section 2.4).

Ids are allocated once, monotonically, never reused or renumbered. High-water marks come
from the manifest's ``counters`` - the same atomic write that publishes the artifacts - so a
failed publish advances nothing: the next attempt re-reads the last good manifest and
re-allocates the same range. Allocation is always a bulk set operation (one anti-join plus
``row_number``), never a per-row lookup.
"""

from __future__ import annotations


class Allocator:
    """The five id-space high-water marks for one generation build."""

    SPACES = ("src_id", "dir_id", "path_id", "cid_id", "snap_id")

    def __init__(self, counters: dict[str, int]):
        self._hw = {s: int(counters.get(s) or 0) for s in self.SPACES}

    def mark(self, space: str) -> int:
        """Current high-water mark: every id ever allocated is <= this."""
        return self._hw[space]

    def counters(self) -> dict[str, int]:
        return dict(self._hw)

    def at_least(self, space: str, value: int) -> None:
        """Raise a high-water mark to ``value`` (ids a resumed build already handed out)."""
        self._hw[space] = max(self._hw[space], int(value or 0))

    def assign(self, con, out: str, candidates_sql: str, order_by: str, space: str,
               id_col: str, id_type: str = "INTEGER") -> int:
        """Materialize ``candidates_sql`` (one row per NEW key, already anti-joined against
        its dictionary) into table ``out`` with ``id_col`` = high_water+1 .. +n, assigned in
        ``order_by`` order. Returns the new high-water mark."""
        first = self._hw[space]
        con.execute(
            f"CREATE OR REPLACE TABLE {out} AS SELECT "
            f"(row_number() OVER (ORDER BY {order_by}) + {first})::{id_type} AS {id_col}, "
            f"* FROM ({candidates_sql})")
        self._hw[space] = first + con.execute(f"SELECT count(*) FROM {out}").fetchone()[0]
        return self._hw[space]


def anti_join(con, out: str, candidates_sql: str, existing_sql: str, keys: list[str]) -> None:
    """``out`` := candidates minus existing, matched on ``keys`` (NULL-safe set difference)."""
    preds = " AND ".join(f"c.{k} IS NOT DISTINCT FROM e.{k}" for k in keys)
    con.execute(
        f"CREATE OR REPLACE TABLE {out} AS SELECT c.* FROM ({candidates_sql}) c "
        f"WHERE NOT EXISTS (SELECT 1 FROM ({existing_sql}) e WHERE {preds})")
