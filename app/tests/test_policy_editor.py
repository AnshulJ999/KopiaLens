"""The full policy editor's backend: the inheritance port, kopia's validation, cron + next
snapshot times, field edits through diff/save, rule Preview from disk, and the new job kinds.

The resolver was checked live against plain `kopia policy show` for every policy in repos 1-3
(198 targets, zero differences, 2026-09-15); these pin the quirks that check relies on.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.api import policies as api
from app.core import policyresolve as pr
from app.core import schedule

SEP = chr(92)
UH = "Anshul@anshul-desktop"
ROOT_C = f"{UH}:C:{SEP}"
PD = f"{UH}:C:{SEP}ProgramData"
PD_BZ = f"{UH}:C:{SEP}ProgramData{SEP}Backblaze"

# kopia v0.23.1 `policy show --global` for a repository whose Global holds kopia's defaults.
GLOBAL_SHOW = """Policy for (global):

Retention:
  Annual snapshots:                        3   (defined for this target)
  Monthly snapshots:                      24   (defined for this target)
  Weekly snapshots:                        4   (defined for this target)
  Daily snapshots:                         7   (defined for this target)
  Hourly snapshots:                       48   (defined for this target)
  Latest snapshots:                       10   (defined for this target)
  Ignore identical snapshots:          false   (defined for this target)

Files policy:
  Ignore cache directories:             true   (defined for this target)
  No ignore rules:
  Read ignore rules from files:                (defined for this target)
    .kopiaignore
  Scan one filesystem only:            false   (defined for this target)

Error handling policy:
  Ignore file read errors:             false   (defined for this target)
  Ignore directory read errors:        false   (defined for this target)
  Ignore unknown types:                 true   (defined for this target)

Scheduling policy:
  Scheduled snapshots:
    None.
  Manual snapshot:                     false   (defined for this target)

Uploads:
  Max parallel snapshots (server/UI):      1   (defined for this target)
  Max parallel file reads:                 -   (defined for this target)
  Parallel upload above size:         2.1 GB   (defined for this target)

Compression disabled.

Metadata compression:
  Compressor:                   zstd-fastest   (defined for this target)

Splitter:
  Algorithm override:   (repository default)   (defined for this target)

No actions defined.

OS-level snapshot support:
  Volume Shadow Copy:                  never   (defined for this target)

Logging details (0-none, 10-maximum):
  Directory snapshotted:                   5   (defined for this target)
  Directory ignored:                       5   (defined for this target)
  Entry snapshotted:                       0   (defined for this target)
  Entry ignored:                           5   (defined for this target)
  Entry cache hit:                         0   (defined for this target)
  Entry cache miss:                        0   (defined for this target)
"""


def test_show_rendering_matches_kopia_text_for_global():
    eff, df = pr.resolve({pr.GLOBAL: pr.DEFAULTS}, "global")
    assert pr.show_lines(eff, df, "global") == pr.parse_show_text(GLOBAL_SHOW)


def test_hierarchy_walks_path_parents_then_user_host_then_global():
    defined = {pr.GLOBAL: {}, ROOT_C: {}, f"{UH}": {}, PD_BZ: {}}
    assert [k for k, _ in pr.hierarchy(defined, PD_BZ)] == [PD_BZ, ROOT_C, UH, pr.GLOBAL]
    assert [k for k, _ in pr.hierarchy(defined, PD)][0] == PD, "an undefined target gets an empty policy first"


def test_lists_replace_but_compression_lists_concat_sorted():
    defined = {pr.GLOBAL: {"files": {"ignore": ["*.log"]}, "compression": {"onlyCompress": [".txt"]}},
               ROOT_C: {"files": {"ignore": ["/Temp"]}, "compression": {"onlyCompress": [".csv"]}}}
    eff, df = pr.resolve(defined, ROOT_C)
    assert eff["files"]["ignore"] == ["/Temp"] and df["files.ignore"] == ROOT_C
    assert eff["compression"]["onlyCompress"] == [".csv", ".txt"]
    assert df["compression.onlyCompress"] == pr.GLOBAL, "the last policy with values names the definition"


def test_plain_fields_cannot_be_cleared_by_a_child():
    defined = {pr.GLOBAL: {"files": {"maxFileSize": 1_000_000_000}},
               ROOT_C: {"files": {"maxFileSize": 0}}}
    eff, df = pr.resolve(defined, ROOT_C)
    assert eff["files"]["maxFileSize"] == 1_000_000_000 and df["files.maxFileSize"] == pr.GLOBAL


def test_times_of_day_concat_unless_the_child_stops_it():
    parent = {"scheduling": {"timeOfDay": [{"hour": 22, "min": 0}]}}
    child = {"scheduling": {"timeOfDay": [{"hour": 6, "min": 30}]}}
    eff, _ = pr.resolve({pr.GLOBAL: parent, ROOT_C: child}, ROOT_C)
    assert eff["scheduling"]["timeOfDay"] == [{"hour": 6, "min": 30}, {"hour": 22, "min": 0}]
    child["scheduling"]["noParentTimeOfDay"] = True
    eff, df = pr.resolve({pr.GLOBAL: parent, ROOT_C: child}, ROOT_C)
    assert eff["scheduling"]["timeOfDay"] == [{"hour": 6, "min": 30}]
    assert df["scheduling.timeOfDay"] == pr.GLOBAL, "kopia's quirk: the definition still moves up"


def test_no_parent_stops_parents_and_kopia_defaults():
    eff, _ = pr.resolve({pr.GLOBAL: pr.DEFAULTS, ROOT_C: {"noParent": True}}, ROOT_C)
    assert "retention" not in eff or not eff["retention"]


def test_validation_mirrors_kopia():
    errs = pr.validate(PD, {"scheduling": {"manual": True, "intervalSeconds": 3600, "cron": ["61 * * * *"]},
                            "upload": {"maxParallelSnapshots": 2}})
    assert len(errs) == 3
    assert pr.validate(UH, {"upload": {"maxParallelSnapshots": 2}}) == []
    assert pr.validate(PD, {"scheduling": {"manual": True}}) == []


def test_apply_fields_sets_and_clears():
    pol = pr.apply_fields({"retention": {"keepDaily": 7}}, {"retention.keepDaily": None, "retention.keepWeekly": 9,
                                                            "scheduling.manual": False})
    assert pol == {"retention": {"keepWeekly": 9}}
    with pytest.raises(ValueError):
        pr.apply_fields({}, {"retention.bogus": 1})


@pytest.mark.parametrize("line,after,expected", [
    ("0 19 1 * * *", dt.datetime(2026, 9, 15, 23, 0), dt.datetime(2026, 10, 1, 19, 0)),
    ("0 22 * * *", dt.datetime(2026, 9, 15, 23, 0), dt.datetime(2026, 9, 16, 22, 0)),
    ("30 8 * * 1-5", dt.datetime(2026, 9, 19, 9, 0), dt.datetime(2026, 9, 21, 8, 30)),
    ("@daily", dt.datetime(2026, 9, 15, 23, 0), dt.datetime(2026, 9, 16, 0, 0)),
    ("0 0 L * *", dt.datetime(2026, 9, 15, 0, 0), dt.datetime(2026, 9, 30, 0, 0)),
    ("15 30 6 * * * *", dt.datetime(2026, 9, 15, 7, 0), dt.datetime(2026, 9, 16, 6, 30, 15)),
    ("0 12 * * FRI#2", dt.datetime(2026, 9, 1, 0, 0), dt.datetime(2026, 9, 4, 12, 0)),  # kopia strips "#..." as a comment
])
def test_cron_matches_cronexpr(line, after, expected):
    assert schedule.cron_next(line, after) == expected


def test_invalid_cron_and_meaning():
    assert schedule.cron_next("61 * * * *", dt.datetime(2026, 1, 1)) is None
    assert schedule.describe_cron("0 19 1 * * *") == "Day 1 of every month, at 19:00"
    assert schedule.describe_cron("0 22 * * 1-5") == "Weekdays, at 22:00"


def test_next_time_interval_truncates_like_go():
    """Go truncates absolute time, not local time: in IST an hourly interval lands on :30."""
    last = dt.datetime(2026, 9, 15, 10, 17)
    nt = schedule.next_time({"intervalSeconds": 3600}, last, dt.datetime(2026, 9, 15, 10, 20))
    assert int(nt.timestamp()) % 3600 == 0
    assert last < nt <= last + dt.timedelta(hours=1)


# ---- API: field edits ---------------------------------------------------------------------

DEFINED = {pr.GLOBAL: {"retention": {"keepDaily": 7}, "files": {"ignore": ["*.log"]}},
           PD: {"files": {"ignore": ["/Temp/**", "Cache/"]}}}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(api, "kopia_repos", lambda: ["3"])
    monkeypatch.setattr(api, "repo_kind", lambda r: "kopia")
    monkeypatch.setattr(api, "load_defined_policies", lambda repo: {"policies": DEFINED})
    monkeypatch.setattr(api, "refresh_defined_policies", lambda repo, force=False, max_age=None: ({"policies": DEFINED}, True))
    monkeypatch.setattr(api, "load_policies", lambda repo: {"policies": {}})
    monkeypatch.setattr(api, "_last_snapshot", lambda repo, key: None)
    monkeypatch.setattr("app.core.sources.sources_index", lambda repo, force=False: {PD: []})


def test_resolve_previews_a_draft(wired):
    out = api.resolve_draft(api.ResolveBody(repo="3", target=PD, changes=api.Changes(
        fields={"retention.keepDaily": 30, "scheduling.cron": ["0 19 1 * * *"]})))
    assert out["effective_before"]["retention"]["keepDaily"] == 7
    assert out["effective"]["retention"]["keepDaily"] == 30 and out["definition"]["retention.keepDaily"] == PD
    assert out["cron"][0]["meaning"] == "Day 1 of every month, at 19:00" and out["errors"] == []


def test_diff_lists_field_changes_and_keeps_rule_order(wired):
    d = api._build_diff("3", PD, api.Changes(add=["x.tmp"], fields={"retention.keepDaily": 30}), price=False)
    assert d["field_changes"][0]["before"] is None and d["field_changes"][0]["after"] == 30
    assert d["resulting_rules"] == ["/Temp/**", "Cache/", "x.tmp"] and d["command"] == ""
    pol = api._draft_policy(DEFINED, PD, api.Changes(add=["x.tmp"], fields={"retention.keepDaily": 30}))
    assert pol == {"files": {"ignore": ["/Temp/**", "Cache/", "x.tmp"]}, "retention": {"keepDaily": 30}}


def test_save_refuses_what_kopia_would_refuse(wired):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        api.write_rules("3", PD, api.Changes(fields={"upload.maxParallelSnapshots": 2}))
    assert err.value.status_code == 400


def test_save_writes_fields_with_the_rules(wired, monkeypatch):
    written = {}
    monkeypatch.setattr(api.policybackup, "create_backup", lambda repo, trigger: {"name": "b.json"})
    monkeypatch.setattr(api, "invalidate_after_write", lambda repo, targets, ignore_only: written.setdefault("ignore_only", ignore_only))

    def fake_write(repo, target, global_, policy, add=0, remove=0, backup=""):
        written["policy"] = policy
        DEFINED_AFTER = dict(DEFINED)
        DEFINED_AFTER[PD] = policy
        monkeypatch.setattr(api, "load_defined_policies", lambda r: {"policies": DEFINED_AFTER})
        return {"returncode": 0, "stdout": "", "stderr": "", "command": ["kopia"]}

    monkeypatch.setattr(api, "policy_write_rules_execute", fake_write)
    out = api.write_rules("3", PD, api.Changes(fields={"retention.keepDaily": 30}))
    assert written["policy"]["retention"] == {"keepDaily": 30}
    assert written["policy"]["files"]["ignore"] == ["/Temp/**", "Cache/"]
    assert written["ignore_only"] is False, "a field write must refresh the effective cache fully"
    assert out["ok"] and out["stored_matches"]


def test_multi_plan_finds_the_shared_parent(wired):
    other = f"{UH}:C:{SEP}Other"
    out = api.multi_plan(api.MultiBody(repo="3", targets=[PD, other], fields={"retention.keepDaily": 14}))
    plan = out["plans"][0]
    assert plan["common_parent"] == pr.GLOBAL and all(r["inherits"] for r in plan["rows"])
    assert [r["creates_policy"] for r in plan["rows"]] == [False, True]
    with pytest.raises(Exception):
        api.multi_plan(api.MultiBody(repo="3", targets=[PD], fields={"files.ignore": ["x"]}))


# ---- rule Preview from disk -----------------------------------------------------------------

def test_search_parts_narrow_anchored_rules():
    from app.core import disklookup
    assert disklookup._search_parts("/Archive/abc.jsonl") == ("Archive", "abc.jsonl", False)
    assert disklookup._search_parts("node_modules/") == ("", "node_modules", True)
    assert disklookup._search_parts("/Games/**") == ("Games", "*", False)


def test_preview_lists_what_the_rule_keeps_out_in_rule_order(monkeypatch, tmp_path):
    from app.core import disklookup
    root = str(tmp_path)
    (tmp_path / "Cache").mkdir()
    rows = [{"path": root + SEP + "Cache", "size": 500, "is_dir": True},
            {"path": root + SEP + "keep.tmp", "size": 5, "is_dir": False},
            {"path": root + SEP + "drop.tmp", "size": 7, "is_dir": False},
            {"path": root + SEP + "Cache" + SEP + "inner.tmp", "size": 9, "is_dir": False}]
    monkeypatch.setattr(disklookup, "status", lambda refresh=False: {"provider": "scan", "available": True, "note": ""})
    monkeypatch.setattr(disklookup, "find", lambda r, name, folders_only=False, limit=0: (
        [x for x in rows if not folders_only or x["is_dir"]], True))
    monkeypatch.setattr("app.core.paths.source_root", lambda key: root)
    monkeypatch.setattr("app.core.paths.sep", lambda key: SEP)
    monkeypatch.setattr("app.core.paths.case_sensitive", lambda key: False)
    rules = ["Cache/", "*.tmp", "!keep.tmp"]
    tmp_rule = disklookup.excluded_by_rule(f"{UH}:{root}", rules, 1)
    assert [i["path"].rsplit(SEP, 1)[1] for i in tmp_rule["items"]] == ["drop.tmp"], \
        "keep.tmp is brought back by the later ! rule; inner.tmp sits under an excluded folder"
    cache_rule = disklookup.excluded_by_rule(f"{UH}:{root}", rules, 0)
    assert cache_rule["total"] == 1 and cache_rule["bytes"] == 500


# ---- job kinds ------------------------------------------------------------------------------

def test_pin_and_snapshot_commands():
    from app.core import kopia
    assert kopia.snapshot_pin_command("1", ["abc"])[-2:] == ["--add", "do-not-delete"]
    assert kopia.snapshot_pin_command("1", ["abc"], remove=True)[-2:] == ["--remove", "do-not-delete"]
    cmd = kopia.snapshot_create_command("1", "D:" + SEP + "Data", "before upgrade")
    assert cmd[-4:] == ["create", "D:" + SEP + "Data", "--description", "before upgrade"]


def test_snapshot_now_refuses_another_computer(monkeypatch):
    from app.api import jobs as jobs_api
    from app.core import jobs
    monkeypatch.setattr(api, "client_options", lambda repo: {"user": "Anshul", "host": "anshul-desktop", "enable_actions": False})
    with pytest.raises(jobs.JobRefused):
        jobs_api._resolve_params("snapshot_create", "3", {"source": "bob@laptop:C:" + SEP + "x"})


def test_everything_find_parses_csv(monkeypatch):
    from app.core import everything
    out = 'Filename,Size,Attributes\n"C:\\\\A\\\\b.tmp",12,A\n"C:\\\\A\\\\Cache",900,D\n'.replace("\\\\", SEP)
    monkeypatch.setattr(everything, "_run", lambda args: out)
    rows = everything.find("C:" + SEP + "A", "*.tmp")
    assert rows == [{"path": "C:" + SEP + "A" + SEP + "b.tmp", "size": 12, "is_dir": False},
                    {"path": "C:" + SEP + "A" + SEP + "Cache", "size": 900, "is_dir": True}]
