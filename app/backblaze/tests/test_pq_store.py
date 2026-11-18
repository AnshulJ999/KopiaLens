"""Tests for the BZ Parquet store machinery: publish/carry-forward, read-after-swap
(retire-on-last-reader), per-drive counts, and GC.

Run with pytest or directly (`python app/backblaze/tests/test_pq_store.py`).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import pytest

from app.backblaze import pq  # noqa: E402


ROWS_C = [
    ("C", r"C:\a.txt", r"c:\a.txt", "a.txt", "txt", 10, "0", 0, None),
    ("C", r"C:\sub\b.dll", r"c:\sub\b.dll", "b.dll", "dll", 20, "0", 1, "folder:\\sub\\"),
]
ROWS_E = [
    ("E", r"E:\big.mp4", r"e:\big.mp4", "big.mp4", "mp4", 5000, "0", 0, None),
]
ROWS_E_V2 = [
    ("E", r"E:\big.mp4", r"e:\big.mp4", "big.mp4", "mp4", 5000, "0", 0, None),
    ("E", r"E:\new.jpg", r"e:\new.jpg", "new.jpg", "jpg", 100, "0", 0, None),
]


@pytest.fixture()
def bzroot(tmp_path, monkeypatch):
    monkeypatch.setattr(pq, "ROOT", str(tmp_path / "bz"))
    return str(tmp_path / "bz")


def _publish(drives: dict) -> tuple[int, dict]:
    """drives = {letter: rows}. Stages each drive, publishes one generation. Returns (gen, rels)."""
    m = pq.read_manifest()
    gen = int((m or {}).get("generation") or 0) + 1
    rels = dict((m or {}).get("drives", {}))
    moves = []
    for drive, rows in drives.items():
        staged = pq.staged_path(drive, gen)
        writer = pq.open_writer(staged)
        try:
            for i in range(0, len(rows), 2):
                writer.write_table(pq.table_from_rows(rows[i:i + 2]))
        finally:
            writer.close()
        rel = pq.pool_rel(drive, gen)
        moves.append((staged, rel))
        rels[drive] = rel
    with pq.WriterLock():
        pq.publish(gen, rels, moves)
    return gen, rels


def test_publish_and_read(bzroot):
    _publish({"C": ROWS_C, "E": ROWS_E})
    assert pq.available()
    assert pq.has_drive("C") and pq.has_drive("E") and not pq.has_drive("D")
    with pq.files_cursor() as cur:
        assert cur.execute("SELECT count(*) FROM files").fetchone()[0] == 3
        assert cur.execute("SELECT sum(size) FROM files WHERE excluded=0").fetchone()[0] == 5010


def test_reindex_one_drive_carries_the_other(bzroot):
    _publish({"C": ROWS_C, "E": ROWS_E})
    first = pq.read_manifest()
    _publish({"E": ROWS_E_V2})
    second = pq.read_manifest()
    assert second["generation"] == first["generation"] + 1
    # unchanged drive carried by reference: same rel path in the new manifest
    assert second["drives"]["C"] == first["drives"]["C"]
    assert os.path.exists(os.path.join(bzroot, first["drives"]["C"].replace("/", os.sep)))
    with pq.files_cursor() as cur:
        assert cur.execute("SELECT count(*) FROM files WHERE drive='C'").fetchone()[0] == 2
        assert cur.execute("SELECT count(*) FROM files WHERE drive='E'").fetchone()[0] == 2


def test_reader_keeps_its_generation_across_a_publish(bzroot):
    """The core safety property: an open cursor keeps reading ITS generation while the writer
    publishes a newer one (the file is never swapped out from under a reader)."""
    _publish({"C": ROWS_C, "E": ROWS_E})
    with pq.files_cursor() as cur:
        before = cur.execute("SELECT count(*) FROM files").fetchone()[0]
        _publish({"E": ROWS_E_V2})
        assert before == 3
        assert cur.execute("SELECT count(*) FROM files").fetchone()[0] == 3
    with pq.files_cursor() as cur:
        assert cur.execute("SELECT count(*) FROM files").fetchone()[0] == 4


def test_drive_counts(bzroot):
    _publish({"C": ROWS_C})
    assert pq.drive_counts("C") == (2, 1, 1)
    assert pq.drive_counts("E") == (0, 0, 0)


def test_gc_removes_unreferenced_old_generation(bzroot, monkeypatch):
    gen1, rels1 = _publish({"C": ROWS_C})
    old_file = os.path.join(bzroot, rels1["C"].replace("/", os.sep))
    assert os.path.exists(old_file)
    _publish({"C": ROWS_C})  # gen 2 rewrites the same drive; gen 1 stays inside the keep window
    assert os.path.exists(old_file)
    monkeypatch.setattr(pq, "_GC_GRACE_S", 0)
    _publish({"C": ROWS_C})  # gen 3 runs GC again; gen 1 is now unreferenced and past grace
    assert not os.path.exists(old_file)
    assert gen1 >= 1


def test_writer_lock_is_exclusive(bzroot):
    with pq.WriterLock():
        with pytest.raises(RuntimeError):
            pq.WriterLock().__enter__()


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        try:
            import inspect
            sig = inspect.signature(fn)
            if "bzroot" in sig.parameters:
                import tempfile
                root = os.path.join(tempfile.mkdtemp(prefix="bz3_pq_"), "bz")
                pq.ROOT = root
                fn(root)
                pq.ROOT = None
            else:
                fn()
        except AssertionError:
            print(f"FAIL {fn.__name__}")
            raise
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} pq-store tests passed.")


if __name__ == "__main__":
    _run_all()
