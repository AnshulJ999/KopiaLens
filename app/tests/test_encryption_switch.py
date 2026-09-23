"""The encryption switch (): the state rules, the rewrite, the caches, the key hand-off.

The load-bearing checks, in the order the risk sits:

  * The MANIFEST is the fact and the setting is the intent. A reader follows the manifest while
    a switch is pending, and a build never flips a generation on its own.
  * The round trip keeps every row, and after a switch-on no Parquet file in the data folder is
    plaintext - that is the actual promise.
  * A switch that dies before the manifest swap leaves the published generation exactly as it
    was (staged files are overwritten by the next attempt).
  * A side cache that cannot be rewritten is dropped, never left in the wrong state.
  * The worker's key hand-off works across a real process boundary, on the pipe, and the
    keychain is asked first when the app itself used it.

Everything runs against a scratch DATA_DIR and a scratch keystore; the developer's real data
folder is never touched.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import duckdb
import pytest

from app.index import artifacts, crypto, keyswitch, sidecache, store

KL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Scratch data folder + keystore, encryption ON and unlocked, repo ids fenced to one."""
    data = tmp_path / "data"
    (data / "cache").mkdir(parents=True)
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    monkeypatch.setattr(keyswitch, "DATA_DIR", str(data))
    monkeypatch.setattr(keyswitch, "CACHEDIR", str(data / "cache"))
    monkeypatch.setattr(keyswitch, "REPOS", ["3"])
    monkeypatch.setattr(keyswitch, "repo_kind", lambda repo: "kopia")
    monkeypatch.setattr(crypto, "keystore_dir", lambda: str(data))
    monkeypatch.setattr(crypto, "remember_on_this_machine", lambda on: True)
    monkeypatch.setattr("app.settings.load_settings", lambda: {"kopialens": {"encryption": True}})
    (data / "settings.json").write_text(json.dumps({"kopialens": {"encryption": True}}),
                                        encoding="utf-8")
    crypto.lock()
    crypto.setup("pw", with_recovery=False)
    yield data
    crypto.lock()


def make_generation(data, rows: int = 137, encrypted: bool = False) -> str:
    """A tiny two-artifact generation written the way the build writes one."""
    root = os.path.join(str(data), "repo3")
    os.makedirs(os.path.join(root, "pool"), exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t AS SELECT range AS cid_id, "
                    "'path/' || range || '/file.bin' AS full_path FROM range(%d)" % rows)
        arts = {}
        for name in ("cid", "tent"):
            rel = f"pool/{name}/g1.parquet"
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            con.execute("COPY (SELECT * FROM t) TO '%s' (%s)"
                        % (dst.replace("\\", "/"), crypto.copy_options(encrypted=encrypted)))
            arts[name] = [rel]
    finally:
        con.close()
    manifest = {"generation": 1, "repo": "3", "schema_version": 3, "counters": {"cid": rows},
                "artifacts": arts, "sources": {}, "stats": {"paths": rows},
                "encrypted": encrypted}
    with open(store.manifest_path("3"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return root


def manifest_json(root: str) -> dict:
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def artifact_files(root: str) -> list[str]:
    rels = [r for files in manifest_json(root)["artifacts"].values() for r in files]
    return [os.path.join(root, r) for r in rels]


def read_artifacts(root: str) -> dict[str, list[int]]:
    """Row count per artifact file, read in whatever state the manifest declares."""
    out: dict[str, list[int]] = {}
    for name, files in manifest_json(root)["artifacts"].items():
        out[name] = []
        for rel in files:
            con = duckdb.connect()
            try:
                args = crypto.parquet_read_list([os.path.join(root, rel).replace("\\", "/")],
                                                encrypted=bool(manifest_json(root).get("encrypted")))
                crypto.register_key(con, encrypted=bool(manifest_json(root).get("encrypted")))
                out[name].append(con.execute("SELECT count(*) FROM read_parquet(%s)" % args).fetchone()[0])
            finally:
                con.close()
    return out


# ---------------------------------------------------------------------------- state rules

def test_the_manifest_is_the_fact_and_the_setting_is_only_the_intent(sandbox):
    make_generation(sandbox, rows=200)                 # plaintext data, setting already ON
    assert crypto.enabled() is True
    assert store.generation_state("3") is False        # the manifest wins
    assert keyswitch.pending() == ["3"]

    gen = store.open_generation("3")                   # reads fine while the setting disagrees
    try:
        assert gen.cursor().execute("SELECT count(*) FROM cid").fetchone()[0] == 200
    finally:
        gen.close()

    keyswitch.switch_repo("3", True)
    assert store.generation_state("3") is True
    assert keyswitch.pending() == []
    gen = store.open_generation("3")
    try:
        assert gen.cursor().execute("SELECT count(*) FROM cid").fetchone()[0] == 200
    finally:
        gen.close()


def test_a_build_never_flips_a_generation_on_its_own(sandbox, monkeypatch):
    root = make_generation(sandbox, rows=10)           # plaintext manifest, setting ON
    seen = {}

    def fake_build(repo, root_, prev, gen, job, force):
        seen["state"] = crypto.writes_encrypted()
        return {"generation": gen, "repo": repo, "artifacts": {}, "sources": {}, "stats": {}}

    monkeypatch.setattr(artifacts, "_build_incremental", fake_build)
    body = artifacts.build_repo("3", root, gen=2)
    assert seen["state"] is False
    assert body["encrypted"] is False                  # the manifest keeps saying plaintext

    keyswitch.switch_repo("3", True)
    monkeypatch.setattr("app.settings.load_settings", lambda: {"kopialens": {"encryption": False}})
    body = artifacts.build_repo("3", root, gen=3)
    assert seen["state"] is True                       # data encrypted, setting now OFF
    assert body["encrypted"] is True


# ---------------------------------------------------------------------------- the switch

def test_switch_on_then_off_keeps_every_row(sandbox):
    root = make_generation(sandbox, rows=137)
    before = read_artifacts(root)

    on = keyswitch.switch_repo("3", True)
    assert on["files"] == 2 and on["rows"] == 274
    assert manifest_json(root)["encrypted"] is True
    assert all(crypto.file_is_encrypted(f) for f in artifact_files(root))
    assert read_artifacts(root) == before

    off = keyswitch.switch_repo("3", False)
    assert off["files"] == 2
    assert manifest_json(root)["encrypted"] is False
    assert not any(crypto.file_is_encrypted(f) for f in artifact_files(root))
    assert read_artifacts(root) == before


def test_no_plaintext_parquet_survives_a_switch_on(sandbox):
    root = make_generation(sandbox, rows=64)
    keyswitch.switch_repo("3", True)
    leftovers = [os.path.join(dp, fn)
                 for dp, _dirs, files in os.walk(root)
                 for fn in files
                 if fn.endswith(".parquet") and not crypto.file_is_encrypted(os.path.join(dp, fn))]
    assert leftovers == []


def test_a_switch_that_dies_before_the_swap_leaves_the_generation_untouched(sandbox, monkeypatch):
    root = make_generation(sandbox, rows=50)
    before = manifest_json(root)

    def crash(root_, manifest):
        raise RuntimeError("simulated crash before the manifest swap")

    with monkeypatch.context() as m:
        m.setattr(keyswitch.publish, "publish", crash)
        with pytest.raises(RuntimeError):
            keyswitch.switch_repo("3", True)

    assert manifest_json(root) == before               # nothing was published
    assert not any(crypto.file_is_encrypted(f) for f in artifact_files(root))
    assert read_artifacts(root) == {"cid": [50], "tent": [50]}

    out = keyswitch.switch_repo("3", True)             # the retry overwrites the staged files
    assert out["files"] == 2 and manifest_json(root)["encrypted"] is True


def test_switching_twice_changes_nothing_the_second_time(sandbox):
    make_generation(sandbox, rows=5)
    keyswitch.switch_repo("3", True)
    again = keyswitch.switch_repo("3", True)
    assert again["files"] == 0 and "already" in again["note"]


def test_a_repo_with_no_generation_reports_that_there_is_nothing_to_rewrite(sandbox):
    out = keyswitch.switch_repo("3", True)
    assert out["files"] == 0 and "next index" in out["note"]


# ---------------------------------------------------------------------------- side caches

def test_caches_are_rewritten_in_place_and_a_broken_one_is_dropped(sandbox):
    cache = os.path.join(str(sandbox), "cache")
    plain = os.path.join(cache, "churn_repo3_30d_d4.parquet")
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE _p AS SELECT '{\"x\": 1}' AS payload, now() AS written_at")
        con.execute("COPY (SELECT * FROM _p) TO '%s' (%s)"
                    % (plain.replace("\\", "/"), crypto.copy_options(encrypted=False)))
    finally:
        con.close()
    broken = os.path.join(cache, "broken.parquet")
    with open(broken, "wb") as fh:
        fh.write(b"not a parquet file at all")

    out = keyswitch.switch_caches(True)
    assert out["files"] == 1 and out["dropped"] == 1
    assert crypto.file_is_encrypted(plain) is True
    assert sidecache.read("churn_repo3_30d_d4", base=cache) == {"x": 1}
    assert not os.path.exists(broken)                  # dropped so it rebuilds in the right state


def test_a_cache_already_in_the_target_state_is_left_alone(sandbox):
    cache = str(sandbox / "cache")
    assert sidecache.write("churn_repo3_30d_d4", {"x": 1}, base=cache) is True
    assert crypto.file_is_encrypted(os.path.join(cache, "churn_repo3_30d_d4.parquet")) is True
    out = keyswitch.switch_caches(True)
    assert out["files"] == 0 and out["dropped"] == 0


# ---------------------------------------------------------------------------- worker key handoff

def test_the_worker_installs_the_key_it_is_handed_on_stdin(sandbox):
    key = crypto.data_key()
    payload = crypto.worker_payload()
    assert json.loads(payload) == {"source": "password", "key": key}

    crypto.lock()
    assert crypto.unlocked() is False
    assert crypto.worker_key(crypto.read_key_payload(True, stream=io.StringIO(payload + "\n"))) is True
    assert crypto.data_key() == key


def test_the_worker_never_reads_an_unflagged_stdin(sandbox):
    # A manual run has the console on stdin; reading it unasked would hang the worker forever.
    assert crypto.read_key_payload(False, stream=io.StringIO("typing into a terminal\n")) is None
    assert crypto.read_key_payload(True, stream=io.StringIO("")) is None
    assert crypto.read_key_payload(True, stream=io.StringIO("not json\n")) is None


def test_the_keychain_is_asked_first_when_the_app_unlocked_from_it(sandbox, monkeypatch):
    asked = []
    monkeypatch.setattr(crypto, "unlock_from_keychain",
                        lambda: (asked.append("keychain"), False)[1])
    crypto.lock()
    assert crypto.worker_key({"source": "keychain", "key": None}) is False
    assert asked == ["keychain"]


def test_a_locked_worker_with_no_key_anywhere_refuses_to_run(sandbox, monkeypatch):
    monkeypatch.setattr(crypto, "unlock_from_keychain", lambda: False)
    crypto.lock()
    assert crypto.worker_key({"source": "password", "key": None}) is False
    assert crypto.worker_key(None) is False


def test_the_handoff_works_across_a_real_process_boundary(sandbox):
    """The pipe, the flag and the env var, proven where they actually run: another process."""
    script = ("from app.index import crypto\n"
              "ok = crypto.worker_key(crypto.read_key_payload(True))\n"
              "print('OK' if ok and crypto.unlocked() else 'FAIL')\n")
    env = {**os.environ, "KOPIALENS_DATA_DIR": str(sandbox), "KOPIALENS_CONFIG_DIR": str(sandbox),
           "KOPIALENS_DUCKDB_MEMORY": "512MB"}
    payload = json.dumps({"source": "pipe", "key": crypto.data_key()})
    proc = subprocess.run([sys.executable, "-c", script], input=payload + "\n", text=True,
                          capture_output=True, cwd=KL_ROOT, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK", proc.stdout + proc.stderr
