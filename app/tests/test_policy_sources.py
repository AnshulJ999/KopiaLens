"""Policy-cache paths: one export instead of one `policy show` per source, and a stale
cache that never blocks a request.

The nightly `policycheck` already proves the resolver output equals kopia's `policy show` for
every source and every defined target; these tests pin how the cache is FILLED and SERVED.
"""

from __future__ import annotations

import threading
import time

from app.core import policy, sources

SEP = chr(92)
SRC = f"Anshul@anshul-desktop:C:{SEP}"


def _explode(*_a, **_k):
    raise AssertionError("kopia must not be spawned on this path")


def test_refresh_policies_resolves_from_one_export(monkeypatch):
    defined = {"(global)": {"files": {"ignore": ["*.tmp"]}},
               SRC: {"retention": {"keepLatest": 5}}}
    monkeypatch.setattr(policy, "refresh_defined_policies",
                        lambda repo, force=False: ({"ts": time.time(), "policies": defined}, True))
    monkeypatch.setattr(sources, "sources_index", lambda repo, force=False: {SRC: [{"id": "x"}]})
    monkeypatch.setattr(policy, "show_policy", _explode)
    monkeypatch.setattr(policy, "load_policies", lambda repo: {})
    written: dict = {}
    monkeypatch.setattr(policy, "_write_policies", lambda repo, pols: written.update(pols))

    out = policy.refresh_policies("3", force=True)

    assert out[SRC]["retention"]["keepLatest"] == 5          # its own definition
    assert out[SRC]["files"]["ignore"] == ["*.tmp"]          # inherited from global by the resolver
    assert written == out                                    # cached


def test_refresh_policies_falls_back_to_live_spawns(monkeypatch):
    """An unreadable export must not leave the cache stale-and-silent: spawn per source instead."""
    monkeypatch.setattr(policy, "refresh_defined_policies",
                        lambda repo, force=False: ({"ts": 0, "policies": {}}, False))
    monkeypatch.setattr(sources, "sources_index", lambda repo, force=False: {SRC: [{"id": "x"}]})
    monkeypatch.setattr(policy, "load_policies", lambda repo: {})
    monkeypatch.setattr(policy, "_write_policies", lambda repo, pols: None)
    seen: list[str] = []

    def fake_show(repo, target):
        seen.append(target)
        return {"retention": {"keepLatest": 9}}

    monkeypatch.setattr(policy, "show_policy", fake_show)
    out = policy.refresh_policies("3", force=True)
    assert seen == [SRC] and out[SRC]["retention"]["keepLatest"] == 9


def test_effective_for_path_resolves_in_process(monkeypatch):
    policy._PATH_CACHE.clear()
    monkeypatch.setattr(policy, "_resolve_all",
                        lambda repo, keys: {keys[0]: {"retention": {"keepLatest": 7}}})
    monkeypatch.setattr(policy, "show_policy", _explode)
    pol = policy.effective_for_path("3", SRC, f"C:{SEP}Folder")
    policy._PATH_CACHE.clear()
    assert pol["retention"]["keepLatest"] == 7


def test_effective_for_path_falls_back_when_nothing_resolves(monkeypatch):
    policy._PATH_CACHE.clear()
    monkeypatch.setattr(policy, "_resolve_all", lambda repo, keys: None)
    monkeypatch.setattr(policy, "show_policy", lambda repo, target: {"retention": {"keepLatest": 3}})
    pol = policy.effective_for_path("3", SRC, f"C:{SEP}Folder")
    policy._PATH_CACHE.clear()
    assert pol["retention"]["keepLatest"] == 3


def test_source_summaries_serves_stale_cache_and_refreshes_in_background(monkeypatch):
    started = threading.Event()
    monkeypatch.setattr(policy, "refresh_policies",
                        lambda repo, force=False: started.set())
    monkeypatch.setattr(policy, "load_policies", lambda repo: {"ts": 0.0, "policies": {}})
    t0 = time.time()
    out = policy.source_summaries("3")
    assert out == {} and time.time() - t0 < 1.0        # never blocks on the refresh
    assert started.wait(2.0)
    for _ in range(100):                                # the single-flight flag clears afterwards
        with policy._REFRESH_LOCK:
            if "3" not in policy._REFRESHING:
                break
        threading.Event().wait(0.02)


def test_source_summaries_uses_a_fresh_cache_without_refreshing(monkeypatch):
    monkeypatch.setattr(policy, "refresh_policies", _explode)
    monkeypatch.setattr(policy, "load_policies",
                        lambda repo: {"ts": time.time(), "policies": {SRC: {"retention": {"keepLatest": 5}}}})
    out = policy.source_summaries("3")
    assert out[SRC]["retention"] == "5 latest"
