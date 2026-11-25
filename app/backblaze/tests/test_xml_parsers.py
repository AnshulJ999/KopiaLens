"""Tests for the bzdata report-XML parsers using inline fixtures (no live bzdata needed).

Run with pytest or directly (`python app/backblaze/tests/test_xml_parsers.py`).
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from app.backblaze import parsers  # noqa: E402


def _tmp(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_read_totalbackup():
    xml = (
        '<contents>\n'
        '<bzstat_total totnumfilesforbackup="1100886" totnumbytesforbackup="12000000000" />\n'
        '<bzvol bzVolumeGuid="v0031f2f3f1f9fef5fef9f9f0f1f" pervol_sel_for_backup_numfiles="465149" '
        'pervol_sel_for_backup_numbytes="2842000000000" />\n'
        '</contents>'
    )
    p = _tmp(xml)
    try:
        grand, per = parsers.read_totalbackup(p)
    finally:
        os.remove(p)
    assert grand == (1100886, 12000000000)
    assert per["v0031f2f3f1f9fef5fef9f9f0f1f"] == (465149, 2842000000000)


def test_read_filestats():
    xml = ('<filestats><info docsnum="36518" docstotbytes="38032940814" '
           'videonum="56459" videototbytes="1723763800260" '
           'everythingnum="1100954" everythingtotbytes="10372000344904" /></filestats>')
    p = _tmp(xml)
    try:
        fs = parsers.read_filestats(p)
    finally:
        os.remove(p)
    assert fs["docs"] == {"num": 36518, "bytes": 38032940814}
    assert fs["video"]["num"] == 56459
    assert fs["everything"]["num"] == 1100954


def test_read_upload_success():
    xml = ('<upload_success_history>'
           '<one_upload_success_stat date_gmt="20260508" num_upload_success="3311" '
           'num_upload_fail_CvtTooBusy="0" num_upload_fail_CvtNoRoom="0" num_upload_fail_UnknownReason="1" />'
           '</upload_success_history>')
    p = _tmp(xml)
    try:
        rows = parsers.read_upload_success(p)
    finally:
        os.remove(p)
    assert rows[0]["date"] == "20260508"
    assert rows[0]["success"] == 3311
    assert rows[0]["fail"] == 1


def test_read_zbytes_history():
    xml = ('<zbytes_history>'
           '<one_zbyte_stat date_time_written_gmt="20260619125414" last_rollback_instr="uxe_rb365d" '
           'num_z_lines="5" tot_bytes_on_z_lines="123456" />'
           '</zbytes_history>')
    p = _tmp(xml)
    try:
        rows = parsers.read_zbytes_history(p)
    finally:
        os.remove(p)
    assert rows[0]["z_lines"] == 5 and rows[0]["z_bytes"] == 123456


def test_read_simple_xml():
    p = _tmp('<contents><lastbackupcompleted localdatetime="20260620024327" gmt_millis="1781903607576" /></contents>')
    try:
        out = parsers.read_simple_xml(p, {"localdatetime": r'localdatetime="(\d+)"'})
    finally:
        os.remove(p)
    assert out["localdatetime"] == "20260620024327"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} xml-parser tests passed.")


if __name__ == "__main__":
    _run_all()
