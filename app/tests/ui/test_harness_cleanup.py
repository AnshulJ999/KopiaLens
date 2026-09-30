"""The harness ties every process it starts to the test run, so a crash or the watchdog's hard
exit (no fixture teardown) cannot leave a KopiaLens copy or a kopia server running."""

from __future__ import annotations

import subprocess
import sys
import time

import psutil

from . import _harness as h

_CHILD = """
import os, subprocess, sys
sys.path.insert(0, {root!r})
from app.tests.ui import _harness as h
p = h.contain(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]))
print(p.pid, flush=True)
os._exit(1)
"""


def test_a_hard_exit_takes_the_started_processes_with_it():
    out = subprocess.run([sys.executable, "-c", _CHILD.format(root=str(h.APP_ROOT))],
                         capture_output=True, text=True, timeout=60)
    pid = int(out.stdout.split()[0])
    deadline = time.monotonic() + 10
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    alive = psutil.pid_exists(pid)
    if alive:
        psutil.Process(pid).kill()
    assert not alive
