"""KopiaLens in the browser (Pyodide): the real app answers the page's API calls directly.

A browser page has no threads, so thread work runs inline and job output is revealed over
time, the way the server's job thread would have produced it."""
from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
import time

DATA = "/data"
CODE = "/kl"
STATE_FILES = ("settings.json", "ui_state.json")

_client = None


def _inline_threads() -> None:
    def start(self):
        try:
            self.run()
        except Exception:  # noqa: BLE001 - a background helper must not break the request
            import logging
            logging.getLogger("kopialens").exception("inline thread %s failed", self.name)

    threading.Thread.start = start
    threading.Thread.join = lambda self, timeout=None: None
    threading.Thread.is_alive = lambda self: False

    def submit(self, fn, /, *args, **kwargs):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - delivered through the future
            fut.set_exception(exc)
        return fut

    concurrent.futures.ThreadPoolExecutor.submit = submit

    import anyio.to_thread

    async def run_sync(func, *args, **_kw):
        return func(*args)

    anyio.to_thread.run_sync = run_sync
    time.sleep = lambda _s: None


_jobs = None


def _replay_jobs() -> None:
    """Jobs run to the end at once; their lines and final state are then revealed at the
    simulated pace, so progress, the jobs chip and Stop behave as on the server."""
    global _jobs
    from app.core import demosim, jobs
    _jobs = jobs
    real_run, real_stop = jobs._run, jobs.stop_job

    def run(job, cmds):
        real_run(job, cmds)
        lines = list(job["buffer"])
        final = {k: job[k] for k in ("state", "rc", "error", "done", "finished", "errors")}
        job.update(state="running", started=time.time(), finished=None, done=0, line_count=0)
        job["buffer"].clear()
        job["_web"] = {"lines": [ln[9:] for ln in lines], "final": final, "t0": time.time(),
                       "step": min(demosim._TICK * 4, demosim._STREAM_SECONDS / max(1, len(lines)))}

    def stop_job(job_id):
        job = jobs._JOBS.get(job_id)
        web = job and job.get("_web")
        if not web:
            return real_stop(job_id)
        web["lines"] = web["lines"][:job["line_count"]] + ["Stopped."]
        web["final"]["state"] = "stopped"
        web["stopped"] = True
        job["stop_requested"] = True
        _advance_jobs()
        return {"ok": True, "how": "flagged", "state": "stopped"}

    jobs._run = run
    jobs.stop_job = stop_job


def _advance_jobs() -> None:
    if _jobs is None:
        return
    now = time.time()
    for job in list(_jobs._JOBS.values()):
        web = job.get("_web")
        if not web:
            continue
        lines = web["lines"]
        due = len(lines) if web.get("stopped") else int((now - web["t0"]) / web["step"]) + 1
        while job["line_count"] < min(due, len(lines)):
            job["buffer"].append(time.strftime("%H:%M:%S ") + lines[job["line_count"]])
            job["line_count"] += 1
        total = max(1, job["total"])
        job["done"] = min(total - 1, total * job["line_count"] // max(1, len(lines)))
        if job["line_count"] >= len(lines):
            job.update(web["final"], finished=time.time())
            del job["_web"]


def restore_state(saved: dict) -> None:
    """Put the viewer's saved settings back before the app reads them."""
    for name, text in (saved or {}).items():
        if name in STATE_FILES and isinstance(text, str):
            with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
                f.write(text)


def state() -> dict:
    out = {}
    for name in STATE_FILES:
        try:
            with open(os.path.join(DATA, name), encoding="utf-8") as f:
                out[name] = f.read()
        except OSError:
            pass
    return out


def boot() -> None:
    os.environ["KOPIALENS_DATA_DIR"] = DATA
    sys.path.insert(0, CODE)
    os.makedirs(os.path.join(CODE, "frontend"), exist_ok=True)
    _inline_threads()
    import httpx
    from app.main import app
    _replay_jobs()
    global _client
    _client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


async def handle(method: str, url: str, headers: dict, body):
    """One API call from the page: (status, headers, body bytes, state changed)."""
    from pyodide.ffi import to_js
    _advance_jobs()
    content = bytes(body.to_py()) if body is not None else None
    res = await _client.request(method, url, headers=dict(headers or {}), content=content)
    _advance_jobs()
    return to_js([res.status_code, [list(h) for h in res.headers.items()], res.content,
                  method != "GET"])
