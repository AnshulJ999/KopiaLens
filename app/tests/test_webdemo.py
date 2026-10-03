"""The web demo's in-page runtime (webdemo/kl_web.py): thread work runs inline, and job output
is revealed over time with Stop ending it early. Run in a child process: the runtime replaces
threading for the whole interpreter."""
import json
import os
import subprocess
import sys

_SCRIPT = """
import concurrent.futures, json, sys, threading
sys.path.insert(0, "webdemo")
import kl_web
kl_web._inline_threads()
ran = []
threading.Thread(target=ran.append, args=(1,)).start()
fut = concurrent.futures.ThreadPoolExecutor(2).submit(lambda: 42)

from app.core import jobs
kl_web._replay_jobs()

def step(job, lf):
    for i in range(10):
        jobs.say(job, lf, f"line {i}")
    return 0, ""

def start():
    job = jobs._new_job("blob_stats", "1", "test", {}, [step], [step], None)
    jobs._JOBS[job["job_id"]] = job
    jobs._run(job, [step])
    return job

a = start()
first = [a["state"], a["line_count"]]
a["_web"]["t0"] -= a["_web"]["step"] * 3.5
kl_web._advance_jobs()
mid = [a["state"], a["line_count"]]
a["_web"]["t0"] -= 100
kl_web._advance_jobs()
end = [a["state"], a["line_count"], a["finished"] is not None, "_web" in a]

b = start()
b["_web"]["t0"] -= b["_web"]["step"] * 2.5
kl_web._advance_jobs()
reply = jobs.stop_job(b["job_id"])
kl_web._advance_jobs()
stopped = [b["state"], b["line_count"], list(b["buffer"])[-1].endswith("Stopped."), reply["state"]]
print(json.dumps({"ran": ran, "fut": fut.result(), "first": first, "mid": mid, "end": end,
                  "stopped": stopped}))
"""


def test_web_runtime_runs_threads_inline_and_replays_jobs(tmp_path):
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, KOPIALENS_DATA_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    res = subprocess.run([sys.executable, "-c", _SCRIPT], cwd=here, env=env, capture_output=True,
                         text=True, encoding="utf-8", timeout=120)
    assert res.returncode == 0, res.stderr[-1500:]
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["ran"] == [1] and out["fut"] == 42
    assert out["first"] == ["running", 0]          # nothing shown the moment it starts
    assert out["mid"] == ["running", 4]            # lines arrive at the simulated pace
    assert out["end"] == ["done", 10, True, False]
    assert out["stopped"] == ["stopped", 4, True, "stopped"]
