"""The files layer ( part 2): whole-file and per-line encryption, the level model, the
switch over the non-index files, the log handler, and the REL30 conversion step.

The load-bearing checks: a file and a line round-trip byte-for-byte; the state is knowable from
the bytes (no key needed) so a half-switched folder stays readable; a torn line is skipped and
the rest of the history survives; the level reads the FILE's setting, never the merged defaults
(that mistake would read a part-1 install's encrypted index as plaintext); the switch moves every
covered file both ways with nothing lost.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import pytest

from app.core import applog, filecrypto
from app.index import crypto


@pytest.fixture
def layer(tmp_path, monkeypatch):
    """Scratch data folder + keystore, Everything level, unlocked, log dirs redirected."""
    data = tmp_path / "data"
    (data / "cache").mkdir(parents=True)
    (data / "logs").mkdir(parents=True)
    monkeypatch.setattr("config.DATA_DIR", str(data))
    monkeypatch.setattr(crypto, "keystore_dir", lambda: str(data))
    monkeypatch.setattr("app.settings.load_settings",
                        lambda: {"kopialens": {"encryption_level": "full"}})
    monkeypatch.setattr("app.settings.raw_kopialens_settings",
                        lambda: {"encryption_level": "full"})
    for name, value in (("LOG_DIR", str(data / "logs")),
                        ("APP_LOG_DIR", str(data / "logs" / "app")),
                        ("INDEX_LOG_DIR", str(data / "logs" / "indexing")),
                        ("JOB_LOG_DIR", str(data / "logs" / "jobs"))):
        monkeypatch.setattr(applog, name, value)
    # the per-dir scan in covered_paths uses module constants, so create them
    for sub in ("app", "indexing", "jobs"):
        os.makedirs(data / "logs" / sub, exist_ok=True)
    crypto.lock()
    crypto.setup("pw", with_recovery=False)
    yield data
    crypto.lock()


# ---------------------------------------------------------------------------- primitives

def test_a_json_file_round_trips_and_reads_as_absent_when_locked(layer):
    path = str(layer / "mounts.json")
    assert filecrypto.write_json(path, {"a": 1, "path": "C:\\Users\\Anshul\\secret"}) is True
    assert filecrypto.is_encrypted_file(path) is True
    assert b"secret" not in open(path, "rb").read()
    assert filecrypto.read_json(path) == {"a": 1, "path": "C:\\Users\\Anshul\\secret"}

    crypto.lock()
    assert filecrypto.read_json(path) is None          # locked: reads as absent, never garbage


def test_a_plaintext_json_file_is_still_read_when_the_layer_is_live(layer):
    # mid-switch, or a file written before the level changed: the reader must not care
    path = str(layer / "reindex_pending.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"3": {"requested": 1, "due": 2}}))
    assert filecrypto.is_encrypted_file(path) is False
    assert filecrypto.read_json(path) == {"3": {"requested": 1, "due": 2}}


def test_lines_round_trip_and_a_torn_last_line_is_skipped(layer):
    path = str(layer / "audit.jsonl")
    for i in range(3):
        assert filecrypto.append_line(path, json.dumps({"n": i})) is True
    with open(path, "a", encoding="utf-8") as fh:      # a crash mid-append
        fh.write("kle:!!!not-valid-base64!!!\n")
    assert filecrypto.read_jsonl(path) == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert filecrypto.tail_lines(path, 2) == [json.dumps({"n": 1}), json.dumps({"n": 2})]


def test_the_state_is_in_the_bytes_not_the_setting(layer):
    path = str(layer / "audit.jsonl")
    with open(path, "w", encoding="utf-8") as fh:     # a line from before the switch
        fh.write("plain-before\n")
    filecrypto.append_line(path, "encrypted-after")
    raw = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    assert not filecrypto.is_encrypted_line(raw[0])    # old line stays readable
    assert filecrypto.is_encrypted_line(raw[1])
    assert filecrypto.read_lines(path) == ["plain-before", "encrypted-after"]
    with_filecrypto = filecrypto.encrypt_line("x")
    assert filecrypto.is_encrypted_line(with_filecrypto)
    assert filecrypto.decrypt_line(with_filecrypto) == "x"


def test_a_locked_layer_refuses_to_write_in_the_clear(layer):
    # Locked at Everything: the line is DROPPED with a warning (audit is best-effort by
    # contract) - writing it in the clear is the one thing that must not happen.
    crypto.lock()
    path = str(layer / "audit.jsonl")
    assert filecrypto.append_line(path, "nope") is False
    assert not os.path.exists(path)
    assert filecrypto.available() is False


# ---------------------------------------------------------------------------- level model

def test_the_level_reads_the_file_not_the_merged_defaults(monkeypatch):
    # The defaults say {encryption_level: "off"}; a part-1 install's file says {encryption: true}.
    # Taking the default as the answer would read its encrypted index as plaintext.
    monkeypatch.setattr("app.settings.raw_kopialens_settings", lambda: {"encryption": True})
    monkeypatch.setattr("app.settings.load_settings",
                        lambda: {"kopialens": {"encryption": True, "encryption_level": "off"}})
    assert crypto.level() == crypto.LEVEL_INDEX
    monkeypatch.setattr("app.settings.raw_kopialens_settings", lambda: {"encryption": False})
    assert crypto.level() == crypto.LEVEL_OFF
    monkeypatch.setattr("app.settings.raw_kopialens_settings", lambda: {"encryption_level": "full"})
    assert crypto.level() == crypto.LEVEL_FULL


# ---------------------------------------------------------------------------- the file switch

def test_the_switch_moves_every_covered_file_both_ways(layer):
    (layer / "policy-backups" / "repo1").mkdir(parents=True)
    covered = {
        "mounts.json": ("json", {"mount": "X:"}),
        "audit.jsonl": ("lines", [{"event": "executed"}]),
        "policy-backups/repo1/20260101_000000_manual.json": ("json", {"(global)": {}}),
        "logs/app/kopialens.log": ("lines", ["a log line"]),
        "runs/repo1.jsonl": ("lines", [{"ok": True}]),
    }
    for rel, (kind, payload) in covered.items():
        path = layer / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:              # pre-switch plaintext
            if kind == "json":
                fh.write(json.dumps(payload))
            else:
                for line in payload:
                    fh.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")

    out = filecrypto.switch_files(True)
    assert out == {"files": 5, "skipped": 0, "failed": 0}
    assert len(filecrypto.pending_files()) == 0
    for rel in covered:
        assert filecrypto.is_encrypted_file(str(layer / rel)) is True
    assert filecrypto.read_json(str(layer / "mounts.json")) == {"mount": "X:"}
    assert filecrypto.read_jsonl(str(layer / "audit.jsonl")) == [{"event": "executed"}]
    assert filecrypto.read_lines(str(layer / "logs/app/kopialens.log")) == ["a log line"]

    back = filecrypto.switch_files(False)
    assert back == {"files": 5, "skipped": 0, "failed": 0}
    for rel in covered:
        assert filecrypto.is_encrypted_file(str(layer / rel)) is False
    assert json.loads((layer / "mounts.json").read_text(encoding="utf-8")) == {"mount": "X:"}


def test_the_switch_rewrites_a_file_this_process_holds_open(layer):
    path = str(layer / "logs/jobs/job_x.log")
    with open(path, "w", encoding="utf-8") as fh:          # a plaintext log from before
        fh.write("before the switch\n")
    stream = filecrypto.open_text(path)                    # the app holds it open

    out = filecrypto.switch_files(True)
    assert out["files"] == 1 and out["failed"] == 0
    assert filecrypto.is_encrypted_file(path) is True
    stream.write("after the switch\n")                 # the stream reopens by itself
    stream.close()
    assert filecrypto.read_lines(path) == ["before the switch", "after the switch"]


def test_a_switch_that_dies_mid_file_keeps_the_original(layer, monkeypatch):
    path = str(layer / "mounts.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"keep": "me"}))
    original = open(path, "rb").read()

    import app.core.filecrypto as fc

    def boom(src, dst):
        raise OSError("simulated crash")

    with monkeypatch.context() as m:
        m.setattr(fc.os, "replace", boom)
        out = filecrypto.switch_files(True)
    assert out["failed"] == 1
    assert open(path, "rb").read() == original         # untouched
    assert [n for n in os.listdir(str(layer)) if n.endswith(".tmp")] == []


def test_the_switch_refuses_to_empty_a_file_it_cannot_read(layer):
    """The 2026-09-23 loss: every line unreadable (a key from another install) reads as zero
    lines, and the old switch wrote the file back EMPTY - the read-back check passed because
    empty matched empty. A non-empty file that reads as nothing must be left alone, loudly."""
    path = str(layer / "audit.jsonl")
    foreign = "kle:" + base64.b64encode(b"a line from another keystore, not ours").decode() + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(foreign * 3)
    original = open(path, "rb").read()
    sibling = str(layer / "mounts.json")
    assert filecrypto.write_json(sibling, {"x": 1}) is True     # encrypted, so it CAN be rewritten

    out = filecrypto.switch_files(False)
    assert out["failed"] == 1 and out["files"] == 1 and out["skipped"] == 0
    assert open(path, "rb").read() == original          # untouched, not emptied
    assert filecrypto.is_encrypted_file(sibling) is False


# ---------------------------------------------------------------------------- logs

def test_the_log_handler_encrypts_every_line_and_the_reader_gets_it_back(layer):
    path = str(layer / "logs/app/kopialens.log")
    handler = applog._CryptoRotatingFileHandler(path, maxBytes=100_000, backupCount=1,
                                                encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("kl-test-filecrypto")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info("indexed C:\\Users\\Anshul\\secret.txt")
        logger.info("second\nwith a continuation")
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()

    raw = open(path, encoding="utf-8").read()
    assert "secret" not in raw
    assert all(line.startswith("kle:") for line in raw.splitlines() if line.strip())
    assert filecrypto.read_lines(path) == ["indexed C:\\Users\\Anshul\\secret.txt",
                                           "second", "with a continuation"]


def test_the_logs_tab_reader_decrypts_a_bounded_tail(layer):
    path = str(layer / "logs/app/kopialens.log")
    for i in range(40):
        filecrypto.append_line(path, f"line {i} with a path C:\\x\\{i}.bin")
    tail = filecrypto.read_tail_text(path, 2048)
    assert "line 39" in tail and "line 0 " not in tail
    assert "C:\\x\\39.bin" in tail


# ---------------------------------------------------------------------------- REL30 conversion

def test_the_upgrade_step_folds_the_old_boolean_into_the_level(layer, monkeypatch):
    written = {}

    def fake_mutate(mutator):
        settings = {"kopialens": {"encryption": True}}
        mutator(settings)
        written.update(settings["kopialens"])
        return settings

    monkeypatch.setattr("app.settings.mutate_settings", fake_mutate)
    monkeypatch.setattr("app.settings.raw_kopialens_settings", lambda: {"encryption": True})

    # one covered file that the level covers but that is still plaintext
    with open(str(layer / "mounts.json"), "w", encoding="utf-8") as fh:
        fh.write("[]")

    out = filecrypto.upgrade_step()
    assert written["encryption_level"] == "index" and "encryption" not in written
    assert out["changed"]
    # at the INDEX level a plaintext mounts.json is correct, so nothing is pending
    assert out["pending"] == []
    monkeypatch.setattr("app.settings.raw_kopialens_settings",
                        lambda: {"encryption_level": "full"})
    assert "mounts.json" in filecrypto.upgrade_step()["pending"][0]
