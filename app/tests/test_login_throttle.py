"""Login throttling: an increasing wait, and NO lockout.

There is one shared password and no accounts, so the owner must never be able to lock himself out
- the guarantee under test is that the wait is bounded and always expires.
"""

from __future__ import annotations

import time

import pytest

from app.core import auth

ADDR = "203.0.113.7"


@pytest.fixture(autouse=True)
def clean_throttle():
    with auth._THROTTLE_LOCK:
        auth._THROTTLE.clear()
    yield
    with auth._THROTTLE_LOCK:
        auth._THROTTLE.clear()


def test_first_failures_are_free_then_the_wait_ramps_to_a_cap():
    for _ in range(auth._THROTTLE_AFTER):
        auth.record_login_failure(ADDR)
    assert auth.login_delay(ADDR) == 0.0                 # five failures, no wait yet
    waits = []
    for _ in range(8):
        auth.record_login_failure(ADDR)
        waits.append(auth.login_delay(ADDR))
    assert waits[0] == 2.0 and waits[1] == 4.0           # each further try waits longer
    assert waits == sorted(waits) and waits[-1] == auth._THROTTLE_MAX


def test_a_right_password_clears_the_record():
    for _ in range(7):
        auth.record_login_failure(ADDR)
    assert auth.login_delay(ADDR) > 0
    auth.record_login_success(ADDR)
    assert auth.login_delay(ADDR) == 0.0


def test_a_quiet_period_forgets_and_nothing_is_permanent():
    for _ in range(8):
        auth.record_login_failure(ADDR)
    with auth._THROTTLE_LOCK:                            # pretend the last failure was long ago
        count, _last = auth._THROTTLE[ADDR]
        auth._THROTTLE[ADDR] = (count, time.time() - auth._THROTTLE_FORGET - 1)
    assert auth.login_delay(ADDR) == 0.0


def test_a_spray_of_addresses_cannot_grow_the_table():
    for i in range(auth._THROTTLE_CAP + 50):
        auth.record_login_failure(f"198.51.100.{i % 256}:{i}")   # fresh timestamps, distinct keys
    with auth._THROTTLE_LOCK:
        assert len(auth._THROTTLE) <= auth._THROTTLE_CAP
