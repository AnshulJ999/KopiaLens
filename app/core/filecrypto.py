"""Whole-file and per-line encryption for everything that is not the index.

The index stays on DuckDB's Parquet encryption - it keeps row-group skipping, which the speed
test proved matters. Everything else is encrypted here, with the ``cryptography`` library:

  * the small JSON state files (mounts, reindex queue, progress/result, size history, policy
    backups, Backblaze state),
  * the append-only history files (the delete audit, job history, run history),
  * the logs (app, debug, worker, job, server),
  * the audit/history files read back line by line.

How it works:

  * **One sub-key, derived from the data key** (HKDF-SHA256, ``kopialens:files:v1``) so this
    layer never shares a key with the index. The password, remember-me and recovery key keep
    working unchanged; a password change re-wraps the data key and nothing here changes.
  * **AES-256-GCM with a fresh random nonce** per encryption - per file for whole files, per
    LINE for the append-only ones. A crash can leave a half-written last line; readers skip it.
  * **The state is written into the bytes**: an encrypted file starts with ``KLE1`` and an
    encrypted line starts with ``kle:``. A reader therefore needs no setting to tell encrypted
    from plaintext content - which is what makes a mid-switch or half-crashed state readable,
    and what lets the switch verify a file it just wrote.
  * **Nothing here is load-bearing.** An unreadable file reads as absent and the app recreates
    it; a torn line is skipped and counted. The one thing that cannot be rebuilt is history:
    at the Full level a lost password AND recovery key make the audit and the histories
    unreadable for good. That is stated in onboarding.

The level is the switch: ``crypto.covers_files()`` says whether this layer is live.

Every writer in this module and the switch share ``_WRITE_LOCK``, so a line appended while a file
is being rewritten is never lost. Nothing may log while holding it: the logging handlers write
through this module, and a handler waiting on the lock while its own lock is held would deadlock.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .applog import get_logger
from . import atomicfile

log = get_logger("filecrypto")

MAGIC = b"KLE1"                 # encrypted whole file: MAGIC + nonce(12) + GCM ciphertext
LINE_PREFIX = "kle:"            # encrypted line: prefix + base64(nonce + GCM ciphertext)
_NONCE = 12
_HEADER = len(MAGIC) + _NONCE
_SUBKEY_INFO = b"kopialens:files:v1"

_KEY_LOCK = threading.Lock()
_WRITE_LOCK = threading.RLock()               # every writer + the switch (see the module doc)
_CACHE: tuple[str, bytes] | None = None      # (data key as handed out, derived sub-key)
_OPEN: dict[str, "CryptoStream"] = {}        # streams this process holds open, by path

# Plain on purpose: the version stamp is read before anything can unlock, the worker status
# file (pid, stage, counts - no paths) is read by the keyless tray and by Stop, and the desktop
# app (no key) reads the server and instance records (pid, port, token).
_NEVER_COVERED = re.compile(r"^(data_version\.json|index_progress_repo[^_]*\.json|server\.json|desktop-instance\.json)$")
# Config read before an unlock (sign-in, repo list, key, table layouts): plain at every level.
_PLAIN_CONFIG = ("settings.json", "repos.json", "keystore.json", "users.json", "ui_state.json",
                 "tokens.json")
_WORKER_LOG = re.compile(r"^index_worker_repo(\w+)\.log(\.\d+)?$")


# ---------------------------------------------------------------------------- key + primitives

def subkey() -> bytes | None:
    """The files sub-key, or None when there is no key to derive it from.

    NOT gated on the level on purpose: READING must work whenever the key is available, so a
    switch DOWN (full -> index) can still decrypt what it is about to rewrite. Gating
    this on ``covers_files()`` once made a down-switch read every encrypted file as empty and
    replace it with nothing. What the level decides is WRITES (see ``available``).

    Derived once per data key and cached: HKDF is cheap, but this is called per LINE for a log
    read and there is no reason to redo it.
    """
    from ..index import crypto
    raw = crypto.data_key()
    if not raw:
        return None
    global _CACHE
    with _KEY_LOCK:
        if _CACHE is not None and _CACHE[0] == raw:
            return _CACHE[1]
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                       info=_SUBKEY_INFO).derive(base64.b64decode(raw))
        _CACHE = (raw, derived)
        return derived


def forget_key() -> None:
    """Drop the derived sub-key (on Lock)."""
    global _CACHE
    with _KEY_LOCK:
        _CACHE = None


def locked_error() -> Exception:
    """The structured lock error (see crypto.StoreLocked) for this layer."""
    from ..index.crypto import StoreLocked
    return StoreLocked("the files layer is locked - unlock KopiaLens")


def available() -> bool:
    """Encrypt new content: the Full level is on and the key is unlocked."""
    from ..index import crypto
    return crypto.covers_files() and subkey() is not None


def writes_blocked() -> bool:
    """The layer is COVERED but locked, so nothing may be written in the clear.

    The app is unusable in that state anyway (every index read fails), and a plaintext line
    on an encrypted install is the one outcome the whole layer exists to prevent.
    """
    from ..index import crypto
    return crypto.covers_files() and not available()


def is_encrypted_blob(blob: bytes) -> bool:
    return blob[:len(MAGIC)] == MAGIC


def is_encrypted_line(line: str) -> bool:
    return line.startswith(LINE_PREFIX)


def file_state(path: str) -> str | None:
    """``encrypted`` | ``plain`` | ``mixed`` | None (absent or empty).

    Cheap by design: the header plus the first and last line, so a status scan never reads a
    whole log. A file that was written across a level switch is ``mixed`` and is normalized
    by the next switch in either direction.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if not size:
        return None
    try:
        with open(path, "rb") as fh:
            if fh.read(len(MAGIC)) == MAGIC:
                return "encrypted"            # a whole-file blob (JSON)
            fh.seek(0)
            first = fh.readline().decode("utf-8", "replace").strip()
            last = ""
            if size > len(first) + 1:
                fh.seek(max(0, size - 4096))
                last = fh.read().decode("utf-8", "replace").splitlines()[-1].strip()
    except OSError:
        return None
    marks = [is_encrypted_line(line) for line in {first, last or first} if line]
    if not marks:
        return None
    if all(marks):
        return "encrypted"
    if not any(marks):
        return "plain"
    return "mixed"


def is_encrypted_file(path: str) -> bool:
    """Whether the file's content is encrypted (a whole blob, or every sampled line)."""
    return file_state(path) == "encrypted"


def encrypt_bytes(data: bytes) -> bytes:
    key = subkey()
    if key is None:
        raise locked_error()
    nonce = secrets.token_bytes(_NONCE)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt_bytes(blob: bytes) -> bytes:
    """Raises when the blob is not ours or the key is wrong; callers treat that as unreadable."""
    key = subkey()
    if key is None:
        raise locked_error()
    return AESGCM(key).decrypt(blob[len(MAGIC):_HEADER], blob[_HEADER:], None)


def encrypt_line(text: str) -> str:
    key = subkey()
    if key is None:
        raise locked_error()
    nonce = secrets.token_bytes(_NONCE)
    return LINE_PREFIX + base64.b64encode(nonce + AESGCM(key).encrypt(nonce, text.encode(), None)).decode()


def decrypt_line(line: str) -> str:
    key = subkey()
    if key is None:
        raise PermissionError("the files layer is locked")
    blob = base64.b64decode(line[len(LINE_PREFIX):])
    return AESGCM(key).decrypt(blob[:_NONCE], blob[_NONCE:], None).decode()


# ---------------------------------------------------------------------------- whole files

def _atomic_write(path: str, payload: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    atomicfile.replace(tmp, path, quiet=True)


def read_text(path: str) -> str | None:
    """The file's text, decrypting when it is encrypted. None when absent or unreadable."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    if not is_encrypted_blob(blob):
        return blob.decode("utf-8", "replace")
    try:
        return decrypt_bytes(blob).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - locked, wrong key, damaged: reads as absent
        log.debug("%s: cannot decrypt (%s)", os.path.basename(path), exc)
        return None


def write_text(path: str, text: str) -> bool:
    """Write the text, encrypted when the layer is live. True when it landed."""
    if writes_blocked():
        log.warning("refusing to write %s in the clear: the files layer is locked",
                    os.path.basename(path))
        return False
    try:
        payload = encrypt_bytes(text.encode()) if available() else text.encode()
        with _WRITE_LOCK:
            _atomic_write(path, payload)
        return True
    except Exception as exc:  # noqa: BLE001 - callers treat a failed write as non-fatal
        log.warning("write failed for %s: %s", os.path.basename(path), exc)
        return False


def read_json(path: str, default=None):
    text = read_text(path)
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError:
        log.warning("%s: not valid JSON (%d bytes)", os.path.basename(path), len(text))
        return default


def write_json(path: str, obj) -> bool:
    return write_text(path, json.dumps(obj, indent=2))


# ---------------------------------------------------------------------------- line files

def read_lines(path: str) -> list[str]:
    """Every line, decrypted, with torn or undecryptable ones SKIPPED (counted in the log).

    A crash during an append can leave half a line; the reader must not lose the rest of the
    file over it, and a line written before a switch is plaintext and still reads.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    out: list[str] = []
    broken = 0
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        if not is_encrypted_line(line):
            out.append(line)
            continue
        try:
            out.append(decrypt_line(line))
        except Exception:  # noqa: BLE001 - torn tail, wrong key, or damage: skip this line
            broken += 1
    if broken:
        log.warning("%s: skipped %d unreadable line(s)", os.path.basename(path), broken)
    return out


def read_jsonl(path: str, limit: int | None = None) -> list[dict]:
    """JSON objects, oldest first. ``limit`` reads only a bounded tail window (history files
    only ever grow); the window's first partial line fails to decode and is dropped."""
    out = []
    for line in (read_lines(path) if not limit else tail_lines(path, limit)):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def tail_lines(path: str, limit: int) -> list[str]:
    """The last ``limit`` lines without reading the whole file, decoded per line.

    Callers that flag a damaged line to the user (the audit, the run history) build their own
    placeholder on top of this; it never hides a line it cannot decode without saying so.
    """
    window = max(65536, limit * 512)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > window:
                fh.seek(size - window)
                fh.readline()                    # discard the partial line the seek landed in
            raw = fh.read()
    except OSError:
        return []
    out: list[str] = []
    broken = 0
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        if not is_encrypted_line(line):
            out.append(line)
            continue
        try:
            out.append(decrypt_line(line))
        except Exception:  # noqa: BLE001 - a torn or foreign line reads as absent
            broken += 1
    if broken:
        log.warning("%s: skipped %d unreadable line(s)", os.path.basename(path), broken)
    return out[-limit:]


def write_lines(path: str, lines: list[str]) -> bool:
    """Rewrite a line file from scratch (the history-trim path). Atomic; encrypted when live."""
    if writes_blocked():
        return False
    try:
        payload = _render_lines(lines, available())
        with _WRITE_LOCK:
            _atomic_write(path, payload)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("write failed for %s: %s", os.path.basename(path), exc)
        return False


def append_line(path: str, text: str) -> bool:
    """Append one line. Encrypted when the layer is live, plaintext otherwise - the reader
    tells the two apart per line, so a file crossing a switch stays readable throughout."""
    try:
        if writes_blocked():
            log.warning("refusing to append to %s in the clear: the files layer is locked",
                        os.path.basename(path))
            return False
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = encrypt_line(text) if available() else text
        with _WRITE_LOCK, open(path, "a", encoding="utf-8") as fh:
            fh.write(payload.rstrip("\n") + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 - history is best-effort, like it always was
        log.warning("append failed for %s: %s", os.path.basename(path), exc)
        return False


# ---------------------------------------------------------------------------- log files

class CryptoStream:
    """A text-stream wrapper that encrypts each LINE on the way out.

    Log writers hand us whole records (which may contain a traceback's newlines) or plain
    fragments; splitting on newlines and encrypting each part round-trips the text exactly.

    The decision is made PER WRITE, not at open time: a level switch therefore never has to
    reopen a live logging handler, and a log crossing the switch ends up mixed - which every
    reader here understands.
    """

    def __init__(self, path: str | None = None, fh=None):
        if fh is not None:
            self._fh = fh
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8", errors="replace")
        self._path = path or getattr(self._fh, "name", "") or ""
        if self._path:
            _OPEN[self._path] = self

    def reopen(self) -> None:
        """Reopen after a close (the switch closes held streams to replace their file)."""
        if self._path:
            self._fh = open(self._path, "a", encoding="utf-8", errors="replace")
            _OPEN[self._path] = self

    def write(self, text: str) -> int:
        if not text:
            return 0
        if writes_blocked():
            return len(text)           # covered but locked: dropping beats leaking
        on = available()
        out = "".join((encrypt_line(line) if on else line) + "\n" for line in text.splitlines())
        with _WRITE_LOCK:
            if getattr(self._fh, "closed", True):
                self.reopen()              # the switch closed it to replace the file
            self._fh.write(out)
        return len(text)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        if self._path:
            _OPEN.pop(self._path, None)
        try:
            self._fh.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def seek(self, *args):                       # logging's rollover touches the stream
        return self._fh.seek(*args)

    def tell(self) -> int:
        return self._fh.tell()

    def truncate(self, *args):
        return self._fh.truncate(*args)

    @property
    def closed(self) -> bool:
        return self._fh.closed

    @property
    def name(self):
        return getattr(self._fh, "name", "")

    def fileno(self) -> int:                     # callers that hand the stream to a subprocess
        return self._fh.fileno()


def wrap_stream(fh):
    """Wrap an already-open text file so each line is encrypted when the layer is live."""
    return CryptoStream(fh=fh)


def open_text(path: str):
    """Open a log/append target for text writing; each line is encrypted when the layer is live.

    Never hand this to a subprocess as its ``stderr``: the child writes to the raw descriptor
    and would bypass the wrapper (the index worker owns its log file for that reason)."""
    return CryptoStream(path)


def read_log_tail(path: str, limit: int = 500) -> list[str]:
    """The last ``limit`` lines, decrypted. Reads the whole file (logs are size-rolled)."""
    return read_lines(path)[-limit:]


def read_tail_text(path: str, max_bytes: int) -> str:
    """A byte-bounded tail of a text file, decrypted per line - the Logs tab's reader.

    Takes a slightly larger window than the plaintext equivalent would need, because an
    encrypted line carries its nonce and tag.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()                    # discard the partial line the seek landed in
            raw = fh.read()
    except OSError:
        return ""
    out: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        if not is_encrypted_line(line):
            out.append(line)
            continue
        try:
            out.append(decrypt_line(line))
        except Exception:  # noqa: BLE001 - a torn or foreign line is skipped
            continue
    return "\n".join(out)


# ---------------------------------------------------------------------------- the file switch

def covered_paths() -> dict[str, str]:
    """Every non-index file the Full level covers: ``path -> kind``.

    kind is one of ``json``, ``lines`` (whole-file rewrite, per-line format), ``text``
    (whole-file rewrite, plain text) or ``parquet`` (handled by the generation switch).
    """
    from config import DATA_DIR
    from . import applog

    out: dict[str, str] = {}
    try:
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if not os.path.isfile(path) or _NEVER_COVERED.match(name):
                continue
            if name.endswith(".jsonl"):
                out[path] = "lines"
            elif name.endswith(".json") and name not in _PLAIN_CONFIG:
                out[path] = "json"
    except OSError:
        pass
    for base in (applog.LOG_DIR, applog.APP_LOG_DIR, applog.INDEX_LOG_DIR, applog.JOB_LOG_DIR):
        try:
            for name in os.listdir(base):
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    out[path] = "lines"      # every log is per-line encrypted
        except OSError:
            pass
    meta = os.path.join(DATA_DIR, "licence", "online.json")   # the store key where no keyring exists
    if os.path.isfile(meta):
        out[meta] = "json"
    try:
        runs = os.path.join(DATA_DIR, "runs")
        for name in os.listdir(runs):
            out[os.path.join(runs, name)] = "lines"
    except OSError:
        pass
    try:
        backups = os.path.join(DATA_DIR, "policy-backups")
        for repo in os.listdir(backups):
            here = os.path.join(backups, repo)
            for name in os.listdir(here):
                out[os.path.join(here, name)] = "json"
    except OSError:
        pass
    return out


def switch_files(on: bool, say=None) -> dict:
    """Rewrite every covered file between plaintext and encrypted, in place.

    Same discipline as the generation switch: read the old content, write a new copy, read it
    back and compare, then replace - so a crash anywhere leaves the original untouched, and an
    interrupted run is simply re-run. A file that is open elsewhere (the running job's own log,
    for instance) is reported and left for the next run rather than half-written.
    """
    say = say or (lambda text: None)
    if on and not available():
        raise locked_error()
    done = skipped = failed = 0
    for path, kind in sorted(covered_paths().items()):
        if not os.path.exists(path):
            continue
        # Skip only a file that is fully and CLEANLY in the target state; `mixed` (written
        # across a switch, or a crash mid-append) is normalized by rewriting it.
        if file_state(path) == ("encrypted" if on else "plain"):
            continue
        if _written_by_running_worker(path):
            skipped += 1
            log.info("%s belongs to a running index pass and was left for the next run",
                     os.path.basename(path))
            continue
        try:
            broken = _switch_one(path, kind, on)
            done += 1
            if broken:
                log.warning("%s: %d unreadable line(s) dropped by the rewrite",
                            os.path.basename(path), broken)
        except PermissionError as exc:  # a file another process holds open: next run gets it
            skipped += 1
            log.warning("%s is in use and was left alone (%s)", os.path.basename(path), exc)
        except Exception as exc:  # noqa: BLE001 - one file must not stop the rest
            failed += 1
            log.error("could not rewrite %s: %s", path, exc)
    if done or skipped or failed:
        say(f"files: {done} rewritten, {skipped} skipped, {failed} failed")
    return {"files": done, "skipped": skipped, "failed": failed}


def _written_by_running_worker(path: str) -> bool:
    """A worker log another process is appending to: replacing it would strand that writer."""
    m = _WORKER_LOG.match(os.path.basename(path))
    if not m:
        return False
    try:
        from . import nightly
        return nightly.is_running(m.group(1))
    except Exception:  # noqa: BLE001
        return False


def decode_lines(raw: bytes) -> tuple[list[str], int]:
    """(decrypted lines, unreadable count). Never logs - it runs under ``_WRITE_LOCK``."""
    out: list[str] = []
    broken = 0
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        if not is_encrypted_line(line):
            out.append(line)
            continue
        try:
            out.append(decrypt_line(line))
        except Exception:  # noqa: BLE001
            broken += 1
    return out, broken


def _switch_one(path: str, kind: str, on: bool) -> int:
    """Rewrite one file under ``_WRITE_LOCK``, so no in-process append lands between the read and
    the replace. Returns the unreadable-line count; raises on failure. Never logs (see the module
    doc)."""
    with _WRITE_LOCK:
        held = _OPEN.get(path)
        if held is not None:
            # Windows refuses to replace a file this process holds open; the stream reopens
            # itself on its next line, which is written in the new state.
            held.close()
        with open(path, "rb") as fh:
            raw = fh.read()
        if kind == "json":
            try:
                text = decrypt_bytes(raw).decode("utf-8") if is_encrypted_blob(raw) \
                    else raw.decode("utf-8")
                obj = json.loads(text)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"not readable as JSON ({type(exc).__name__})") from None
            text = json.dumps(obj, indent=2)
            payload = encrypt_bytes(text.encode()) if on else text.encode()
            _replace_verified(path, payload, lambda blob: _same_json(blob, obj, on))
            return 0
        lines, broken = decode_lines(raw)
        if raw.strip() and not lines:
            # The 2026-09-23 loss: every line unreadable (a foreign key, or damage) reads as zero
            # lines, and the rewrite would empty the file - whose read-back check then passed,
            # because empty matched empty. Never replace a non-empty file with nothing.
            raise ValueError("reads as empty although the file is not - refusing to replace it")
        payload = _render_lines(lines, on)
        _replace_verified(path, payload, lambda blob: _same_lines(blob, lines, on))
        return broken


def _replace_verified(path: str, payload: bytes, verify) -> None:
    tmp = f"{path}.{os.getpid()}.switch.tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        with open(tmp, "rb") as fh:
            if not verify(fh.read()):
                raise RuntimeError("the rewritten copy does not match the original")
        atomicfile.replace(tmp, path, quiet=True)
    finally:
        for stale in (tmp,):
            try:
                if os.path.exists(stale):
                    os.remove(stale)
            except OSError:
                pass


def _same_json(blob: bytes, obj, encrypted: bool) -> bool:
    try:
        if encrypted and not is_encrypted_blob(blob):
            return False
        text = decrypt_bytes(blob).decode() if encrypted else blob.decode()
        return json.loads(text) == obj
    except Exception:  # noqa: BLE001
        return False


def _same_lines(blob: bytes, lines: list[str], encrypted: bool) -> bool:
    """A line file is read back LINE BY LINE - it is never a whole-file blob."""
    try:
        text = blob.decode("utf-8", "replace")
        raw = [l for l in text.splitlines() if l.strip()]
        if encrypted and any(not is_encrypted_line(l) for l in raw):
            return False
        got = [decrypt_line(l) if is_encrypted_line(l) else l for l in raw]
        return got == lines
    except Exception:  # noqa: BLE001
        return False


def _render_lines(lines: list[str], on: bool) -> bytes:
    return "".join((encrypt_line(l) if on else l) + "\n" for l in lines).encode()


def encrypt_line_file(path: str) -> bool:
    """Encrypt one line file another program wrote in the clear (kopia's own job log), now.
    False when the layer isn't live, the file is already encrypted, or the rewrite failed."""
    if not available() or not os.path.isfile(path) or file_state(path) in (None, "encrypted"):
        return False
    try:
        broken = _switch_one(path, "lines", True)
    except Exception as exc:  # noqa: BLE001 - the next sweep tries again
        log.warning("could not encrypt %s: %s", os.path.basename(path), exc)
        return False
    if broken:
        log.warning("%s: %d unreadable line(s) dropped by the rewrite", os.path.basename(path), broken)
    return True


def pending_files() -> list[str]:
    """Covered files that are not in the state the level asks for (the file-layer 'pending')."""
    from ..index import crypto
    want = "encrypted" if crypto.covers_files() else "plain"
    return [p for p in covered_paths() if os.path.exists(p) and file_state(p) != want]


# ---------------------------------------------------------------------------- conversion

def upgrade_step() -> dict:
    """The data-folder step for this layer: convert the encryption settings, report files.

    ``encryption`` (the old on/off boolean) becomes ``encryption_level``: on read as index-level
    from now on, written once here. Files the level covers but that are still plaintext are
    REPORTED, never rewritten - a data rewrite is the user's explicit switch, not an upgrade.

    The data-folder upgrade registers this; it is safe to call twice and safe to call at level off.
    """
    from ..index import crypto
    from ..settings import mutate_settings

    changed: list[str] = []
    notes: list[str] = []

    def _mutate(settings: dict) -> None:
        kl = settings.setdefault("kopialens", {})
        if not kl.get("encryption_level"):
            legacy = bool(kl.get("encryption"))
            kl["encryption_level"] = crypto.LEVEL_INDEX if legacy else crypto.LEVEL_OFF
            changed.append(f"encryption -> encryption_level={kl['encryption_level']}")
        if "encryption" in kl:
            kl.pop("encryption")           # the old boolean: read as a level from now on
            changed.append("removed the legacy encryption key")

    try:
        mutate_settings(_mutate)
    except Exception as exc:  # noqa: BLE001 - an unwritable settings file must not stop an upgrade
        notes.append(f"could not write the encryption level: {exc}")

    try:
        pending = pending_files()
    except Exception as exc:  # noqa: BLE001
        pending = []
        notes.append(f"could not scan the covered files: {exc}")
    if pending:
        notes.append(f"{len(pending)} file(s) are not in the state level {crypto.level()} asks "
                     f"for - the encryption switch rewrites them")
        log.debug("files pending the switch: %s", pending)    # paths: debug log only
    return {"changed": changed, "pending": len(pending), "notes": notes}
