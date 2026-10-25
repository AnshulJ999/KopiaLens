"""Two lanes for index reads: what someone is waiting for runs first, background fills step aside.

Every statement on a store cursor runs through ``run``:

* foreground (an API request someone is waiting for): runs at once and interrupts any background
  statement in flight, which is retried once the foreground is idle;
* background (column fills, cache refreshes, warmups): one background job at a time across the
  app, and each statement waits while foreground work runs;
* a request whose client went away is stopped at its next statement, or interrupted mid-statement.

The request middleware opens a scope per API request (``scope``); threads outside a request read
as background.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import functools
import threading
import time

import duckdb

from ..core.applog import get_logger

log = get_logger("lanes")

FG, BG, FREE = "fg", "bg", "free"   # FREE: polled status reads, neither wait nor interrupt
_MAX_PREEMPT = 3          # a background statement interrupted this often then runs alongside
_MAX_WAIT = 20.0          # seconds a background statement waits for the foreground at most


class RequestCancelled(Exception):
    """The client went away; the request's work stops."""


class _Scope:
    def __init__(self, lane: str, method: str = "", path: str = "", live: bool = False):
        self.lane = lane
        self.method, self.path, self.live = method, path, live   # the request, for the kopia rule
        self.cancelled = False
        self.holds_bg = False
        self.running: set = set()       # raw cursors executing for this scope

    def cancel(self) -> None:
        self.cancelled = True
        with _CV:
            running = list(self.running)
        for cur in running:
            _interrupt(cur, "request cancelled")


_SCOPE: contextvars.ContextVar = contextvars.ContextVar("kopialens_lane", default=None)
_HEAVY: threading.Semaphore | None = None
_HEAVY_INIT = threading.Lock()
_HEAVY_HELD = threading.local()
_CV = threading.Condition()
_BG_SLOT = threading.Semaphore(1)   # one background job at a time (they share the CPU)
_fg_running = 0
_bg_running: dict = {}              # raw cursor -> [preempted flag]
_RECENT = collections.deque(maxlen=64)   # (cursor id, time, who, thread) of recent interrupts


def _heavy_slots() -> threading.Semaphore:
    global _HEAVY
    with _HEAVY_INIT:
        if _HEAVY is None:
            from .store import memory_limit_bytes
            _HEAVY = threading.Semaphore(1 if memory_limit_bytes() <= 2 ** 30 else 2)
        return _HEAVY


@contextlib.contextmanager
def heavy():
    """Memory-hungry reads someone is waiting for take turns: one at a time up to a 1 GB limit, two
    above it, since each can need most of a small limit. Background work is already one job at a
    time and steps aside for these, so it never waits here."""
    scope = _SCOPE.get()
    if scope is None or scope.lane != FG or getattr(_HEAVY_HELD, "on", False):
        yield
        return
    slots = _heavy_slots()
    while not slots.acquire(timeout=0.5):
        if scope.cancelled:
            raise RequestCancelled()
    _HEAVY_HELD.on = True
    try:
        yield
    finally:
        _HEAVY_HELD.on = False
        slots.release()


def heavy_read(fn):
    """Decorator form of :func:`heavy`."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with heavy():
            return fn(*args, **kwargs)
    return wrapper


def _interrupt(cur, who: str) -> None:
    _RECENT.append((id(cur), time.monotonic(), who, threading.current_thread().name))
    try:
        cur.interrupt()
    except Exception:  # noqa: BLE001 - the statement may have finished already
        pass


def _who_interrupted(cur) -> str:
    now = time.monotonic()
    for cid, at, who, thread in reversed(_RECENT):
        if cid == id(cur):
            return f"{who} from thread {thread}, {now - at:.2f} s ago"
    return "nothing in this app's lanes"


@contextlib.contextmanager
def scope(lane: str):
    """Run a block in ``lane``; a background scope releases its slot at the end."""
    sc = _Scope(lane)
    token = _SCOPE.set(sc)
    try:
        yield sc
    finally:
        _SCOPE.reset(token)
        release(sc)


def background():
    """A background job outside a request (cache refresh, warmup)."""
    return scope(BG)


def open_scope(lane: str, method: str = "", path: str = "", live: bool = False):
    """For the ASGI middleware: start a scope; pair with ``close_scope``."""
    sc = _Scope(lane, method, path, live)
    return sc, _SCOPE.set(sc)


def current() -> _Scope | None:
    return _SCOPE.get()


def close_scope(sc: _Scope, token) -> None:
    _SCOPE.reset(token)
    release(sc)


def release(sc: _Scope) -> None:
    if sc.holds_bg:
        sc.holds_bg = False
        _BG_SLOT.release()


def check() -> None:
    """Raise when the current request was abandoned."""
    sc = _SCOPE.get()
    if sc is not None and sc.cancelled:
        raise RequestCancelled()


def _take_slot(sc: _Scope) -> None:
    while not _BG_SLOT.acquire(timeout=0.25):
        if sc.cancelled:
            raise RequestCancelled()
    sc.holds_bg = True


def run(cur, fn):
    """Execute one statement on raw cursor ``cur`` in the current lane."""
    global _fg_running
    sc = _SCOPE.get()
    lane = sc.lane if sc is not None else BG
    if sc is not None and sc.cancelled:
        raise RequestCancelled()
    if lane == FREE:
        return fn()
    if lane == FG:
        for attempt in (1, 2):
            with _CV:
                _fg_running += 1
                for c, flag in _bg_running.items():
                    if flag[1] < _MAX_PREEMPT:
                        flag[0] = True
                        _interrupt(c, "foreground preempt")
                if sc is not None:
                    sc.running.add(cur)
            try:
                return fn()
            except duckdb.InterruptException:
                if sc is not None and sc.cancelled:
                    raise RequestCancelled() from None
                log.debug("foreground statement interrupted without a cancel (%s %s, attempt %d); "
                          "interrupted by %s", sc.method if sc else "", sc.path if sc else "",
                          attempt, _who_interrupted(cur))
                if attempt == 2:
                    raise
            finally:
                with _CV:
                    _fg_running -= 1
                    if sc is not None:
                        sc.running.discard(cur)
                    _CV.notify_all()
    if sc is not None and not sc.holds_bg:
        _take_slot(sc)
    preempts = 0
    while True:
        deadline = time.monotonic() + _MAX_WAIT
        with _CV:
            while _fg_running and preempts < _MAX_PREEMPT and time.monotonic() < deadline:
                _CV.wait(0.25)
                if sc is not None and sc.cancelled:
                    raise RequestCancelled()
            flag = [False, preempts]
            _bg_running[cur] = flag
            if sc is not None:
                sc.running.add(cur)
        try:
            return fn()
        except duckdb.InterruptException:
            if sc is not None and sc.cancelled:
                raise RequestCancelled() from None
            if not flag[0]:
                raise
            preempts += 1
        finally:
            with _CV:
                _bg_running.pop(cur, None)
                if sc is not None:
                    sc.running.discard(cur)
