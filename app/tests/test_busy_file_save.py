"""A save survives another program briefly holding the file open (Windows refuses the swap then)."""
import json
import sys
import threading

import pytest

from app import settings as st

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace an open file")


def test_settings_save_waits_for_a_briefly_open_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(st, "SETTINGS_FILE", str(path))
    holder = open(path, "rb")   # no delete sharing, like a virus scanner or a backup tool reading it
    threading.Timer(0.3, holder.close).start()
    try:
        st.save_settings({"kopialens": {"theme": "dark"}})
    finally:
        holder.close()
    assert json.loads(path.read_text(encoding="utf-8"))["kopialens"]["theme"] == "dark"
