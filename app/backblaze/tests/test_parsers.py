"""Unit tests for the bzdata parsers using tiny temp fixtures (no live bzdata needed).

Run with pytest or directly (`python app/backblaze/tests/test_parsers.py`).
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from app.backblaze import parsers  # noqa: E402

TAB = "\t"


def _write(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".dat")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_iter_filelist_skips_non_f_lines():
    text = (
        "# GmtMillis...\n"
        "# Dir: C:\\\n"
        f"f{TAB}00000177cdb08e20{TAB}285478{TAB}C:\\foo\\bar.txt\n"
        f"s{TAB}0000019c486b2ac9{TAB}0{TAB}C:\\sym\\link.json\n"   # symlink -> skipped
        f"f{TAB}00000161b9613110{TAB}33{TAB}C:\\Autorun.inf\n"
    )
    path = _write(text)
    try:
        rows = list(parsers.iter_filelist(path))
    finally:
        os.remove(path)
    assert len(rows) == 2
    assert rows[0] == ("00000177cdb08e20", 285478, "C:\\foo\\bar.txt")
    assert rows[1][1] == 33


def test_parse_queue():
    text = (
        f"1{TAB}+{TAB}000000000034fcee{TAB}0000019edfcf9d4b{TAB}0{TAB}G:\\Playnite\\cef.log\n"
        f"1{TAB}+{TAB}00000000003ddf4a{TAB}0000019ee005e345{TAB}4096{TAB}E:\\Kopia\\xe205.f\n"
    )
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "bz_todo_20260620_0.dat"), "w", encoding="utf-8") as fh:
        fh.write(text)
    rows = parsers.parse_queue(d)
    assert (0, "G:\\Playnite\\cef.log") in rows
    assert (4096, "E:\\Kopia\\xe205.f") in rows


def test_parse_eventlog():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "20.log"), "w", encoding="utf-8") as fh:
        fh.write("2026-06-20 00:25:54 - STARTBACKUP\n"
                 "2026-06-20 02:43:27 - END_BACKUP: Backed up 6659 FILES / 7586 MB\n")
    evs = parsers.parse_eventlog(d)
    kinds = [e["kind"] for e in evs]
    assert "session_start" in kinds and "session_end" in kinds
    end = next(e for e in evs if e["kind"] == "session_end")
    assert end["files"] == 6659
    assert end["bytes"] == 7586 * 1_000_000


def test_folder_key():
    assert parsers.folder_key("C:\\a\\b\\c\\d.txt", 2) == "C:\\a\\b"
    assert parsers.folder_key("C:\\a", 5) == "C:\\a"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} parser tests passed.")


if __name__ == "__main__":
    _run_all()
