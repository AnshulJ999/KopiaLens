"""An interrupt that lands as a statement returns surfaces on the fetch; the read runs again instead of failing."""
import duckdb

from app.index.store import _PinnedCursor


class _Gen:
    def release(self):
        pass


class _Cursor:
    """Raises one interrupt on the first fetch, like a preempt landing between execute and fetch."""

    def __init__(self):
        self.executes = 0
        self.interrupts = 1

    def execute(self, sql, params=None):
        self.executes += 1
        return self

    def fetchall(self):
        if self.interrupts:
            self.interrupts -= 1
            raise duckdb.InterruptException("INTERRUPT Error: Interrupted!")
        return [(1,)]


def test_a_fetch_interrupted_without_a_cancel_runs_the_statement_again():
    raw = _Cursor()
    cur = _PinnedCursor(_Gen(), raw)
    assert cur.execute("SELECT 1").fetchall() == [(1,)]
    assert raw.executes == 2
