"""Nightly warm passes: the churn prewarm depths and pricing's temp tables."""

from __future__ import annotations


def test_churn_prewarm_covers_the_tab_depth_and_the_panel_depth(monkeypatch):
    """The tab rolls up at the user's setting while the folder panel reads depth 4 - building only
    one of them was the reason a first open after the nightly recomputed from scratch."""
    from app.index import churnview

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(churnview, "churn_rollup",
                        lambda repo, days=0, depth=0, refresh=False, **kw: calls.append((days, depth)))
    monkeypatch.setattr(churnview, "ui_depth", lambda: 8)
    churnview.prewarm("3", windows=(1, 30))
    assert {depth for _days, depth in calls} == {8, 4}
    assert (30, 4) in calls and (30, 8) in calls


def test_churn_prewarm_honours_an_explicit_depth(monkeypatch):
    from app.index import churnview

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(churnview, "churn_rollup",
                        lambda repo, days=0, depth=0, refresh=False, **kw: calls.append((days, depth)))
    monkeypatch.setattr(churnview, "ui_depth", lambda: 8)
    churnview.prewarm("3", depth=4, windows=(30,))
    assert calls == [(30, 4)]


def test_pricing_materializes_before_batching(monkeypatch):
    """ More than one batch must read the per-source temp tables, not the Parquet view: the
    FILTER list's planning cost is superlinear in rule count and was the 193 s stage."""
    from app.index import policypricing as pp

    seen: list[str] = []

    class _Cur:
        def execute(self, sql, params=None):
            seen.append(" ".join(str(sql).split())[:40])

            class _R:
                def fetchone(self):
                    return (None,)
            return _R()

        def close(self):
            pass

    monkeypatch.setattr(pp, "read_cursor", lambda repo: _Cur())
    monkeypatch.setattr(pp, "resolve_src_id", lambda cur, key: 5)
    monkeypatch.setattr(pp, "_price_batch_with_regex",
                        lambda cur, src, entries, root_len, sep, px_table="pathx", dir_table="dir":
                        seen.append(f"batch:{px_table}"))

    out = pp._compute("3", "u@h:C:" + chr(92), [f"*.tmp{i}" for i in range(pp._BATCH + 1)])

    assert out["slow_path"] is False
    assert any(pp._MAT_PX in s for s in seen), "the source was not materialized"
    assert f"batch:{pp._MAT_PX}" in seen, "the batches ran against the Parquet view"


def test_a_single_batch_stays_on_the_view(monkeypatch):
    """One batch is one scan either way; materializing first would only add a copy."""
    from app.index import policypricing as pp

    seen: list[str] = []

    class _Cur:
        def execute(self, sql, params=None):
            seen.append(" ".join(str(sql).split())[:40])

            class _R:
                def fetchone(self):
                    return (None,)
            return _R()

        def close(self):
            pass

    monkeypatch.setattr(pp, "read_cursor", lambda repo: _Cur())
    monkeypatch.setattr(pp, "resolve_src_id", lambda cur, key: 5)
    monkeypatch.setattr(pp, "_price_batch_with_regex",
                        lambda cur, src, entries, root_len, sep, px_table="pathx", dir_table="dir":
                        seen.append(f"batch:{px_table}"))

    pp._compute("3", "u@h:C:" + chr(92), [f"*.x{i}" for i in range(3)])
    assert f"batch:{pp._MAT_PX}" not in seen and "batch:pathx" in seen
