"""Prune versions: pricing a set of ticked rows, and the per-object commands."""

from __future__ import annotations

import pytest

from app.core import kopia
from app.index import prune

S1, S2 = "me@pc:D:\\", "me@pc:D:\\Db"


def _state(rows, outside=(), chunks=None, refs=None, objrefs=None, sizes=None):
    objs = {o for r in rows for _s, o in r["pairs"]}
    chunks = chunks or {o: {o} for o in objs}
    pieces = {c for cs in chunks.values() for c in cs}
    return {"rows": rows, "outside": set(outside), "chunks": chunks,
            "refs": refs or {c: 1 for c in pieces}, "objrefs": objrefs or {},
            "sizes": sizes or {c: 10 for c in pieces}}


def _row(obj, src=S1, snaps=("s1",)):
    return {"obj": obj, "pairs": {(src, obj)},
            "snapshots": [{"snapshot_id": s, "source": src} for s in snaps]}


def test_a_version_alone_frees_its_own_pieces_not_the_shared_ones():
    # Two versions of a big file share piece p; v1 also has a, v2 has b.
    st = _state([_row("Iv1"), _row("Iv2", snaps=("s2",))],
                chunks={"Iv1": {"p", "a"}, "Iv2": {"p", "b"}}, objrefs={"p": 2, "a": 1, "b": 1})
    assert prune._price(st, {0})["frees"] == 10           # a only: v2 keeps p
    assert prune._price(st, {0, 1})["frees"] == 30        # both go: p frees too


def test_a_version_also_held_at_another_path_frees_nothing():
    st = _state([_row("v1"), _row("v2", snaps=("s2",))], outside={(S1, "v1")})
    assert prune._price(st, {0})["frees"] == 0
    assert prune._price(st, {1})["frees"] == 10


def test_nested_holders_lose_the_version_together():
    # The same path in two sources: the version is one row holding two pairs.
    row = {"obj": "v1", "pairs": {(S1, "v1"), (S2, "v1")},
           "snapshots": [{"snapshot_id": "a", "source": S1}, {"snapshot_id": "b", "source": S2}]}
    st = _state([row, _row("v2", snaps=("c",))], refs={"v1": 2, "v2": 1})
    assert prune._price(st, {0})["frees"] == 10


def test_a_folder_snapshot_frees_only_files_no_kept_snapshot_holds():
    # rows = snapshots of a folder; f1 is in both, f2 only in the old snapshot.
    old = {"obj": "d1", "pairs": {(S1, "f1"), (S1, "f2")}, "snapshots": [{"snapshot_id": "s1", "source": S1}]}
    new = {"obj": "d2", "pairs": {(S1, "f1")}, "snapshots": [{"snapshot_id": "s2", "source": S1}]}
    st = _state([old, new])
    assert prune._price(st, {0})["frees"] == 10


def test_items_for_lists_each_object_with_its_own_snapshots():
    st = _state([_row("v1", snaps=("s1", "s2")), _row("v2", snaps=("s3",))])
    assert prune.items_for(st, {0, 1}) == [{"obj": "v1", "snapshot_ids": ["s1", "s2"]},
                                           {"obj": "v2", "snapshot_ids": ["s3"]}]


def test_prune_commands_never_mix_objects_and_dry_runs_never_commit(monkeypatch):
    monkeypatch.setattr(kopia, "_repo_args", lambda repo: [])
    monkeypatch.setattr(kopia, "_kopia_exe", lambda: "kopia")
    items = [{"obj": "Iabc", "snapshot_ids": ["s1", "s2"]}, {"obj": "kdef", "snapshot_ids": ["s3"]}]
    dry = kopia.prune_commands("3", items)
    assert dry == [["kopia", "snapshot", "fix", "remove-files", "--object-id", "Iabc",
                    "--manifest-id", "s1", "--manifest-id", "s2"],
                   ["kopia", "snapshot", "fix", "remove-files", "--object-id", "kdef",
                    "--manifest-id", "s3"]]
    assert all(c[-1] == "--commit" for c in kopia.prune_commands("3", items, commit=True))


def test_prune_commands_split_long_snapshot_lists(monkeypatch):
    monkeypatch.setattr(kopia, "_repo_args", lambda repo: [])
    monkeypatch.setattr(kopia, "_kopia_exe", lambda: "kopia")
    ids = [f"{i:032x}" for i in range(400)]
    cmds = kopia.prune_commands("3", [{"obj": "Iabc", "snapshot_ids": ids}], max_chars=4000)
    assert len(cmds) > 1 and all(sum(len(a) + 1 for a in c) <= 4000 for c in cmds)
    assert [a for c in cmds for a in c if a not in ("--manifest-id",)][-1] == ids[-1]
    assert sorted(a for c in cmds for a in c[6:] if a != "--manifest-id") == sorted(ids)


@pytest.mark.parametrize("bad", [{"obj": "--commit", "snapshot_ids": ["s1"]},
                                 {"obj": "kabc", "snapshot_ids": ["s1 --commit"]},
                                 {"obj": "kabc", "snapshot_ids": []}])
def test_prune_commands_refuse_anything_but_plain_ids(monkeypatch, bad):
    monkeypatch.setattr(kopia, "_repo_args", lambda repo: [])
    with pytest.raises(ValueError):
        kopia.prune_commands("3", [bad])


def test_kept_versions_keep_their_pieces_even_before_the_map_exists():
    # A generation built before crefo has no pair counts: the plan's own kept rows still block.
    st = _state([_row("Iv1"), _row("Iv2", snaps=("s2",))], chunks={"Iv1": {"p", "a"}, "Iv2": {"p", "b"}})
    assert prune._price(st, {0})["frees"] == 10
    total = sum(st["sizes"].values())
    assert prune._price(st, {0})["frees"] + prune._price(st, {1})["frees"] <= total
