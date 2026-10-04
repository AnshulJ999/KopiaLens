"""App-facing entry point: finds the stored licences and verifies them."""
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .model import NO_LICENCE, RANK, Licence, State
from .verify import load_public_keys, normalise, verify

LICENSE_FILE = "license.key"     # single licence: shared folder, and app folders from before 1.2
LICENCES_DIR = "licences"        # app folder: one <id>.license per stored licence
SHARED_DIR = Path.home() / ".licensekit"
MAX_FILE_BYTES = 64 * 1024
MAX_STORED = 50
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class LicenseKit:
    def __init__(self, product, public_keys, banned_ids=(), app_major=None, app_dir=None,
                 env_prefix=None, shared_dir=SHARED_DIR, clock=None):
        self.product = product
        self.keys = load_public_keys(public_keys)
        self.banned = frozenset(banned_ids)
        self.app_major = app_major
        self.app_dir = Path(app_dir) if app_dir else None
        self.env_prefix = (env_prefix or product).upper()
        self.shared_dir = Path(shared_dir) if shared_dir else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(self, text, origin="pasted") -> Licence:
        return verify(text, product=self.product, keys=self.keys, banned=self.banned,
                      app_major=self.app_major, now=self._clock(), origin=origin)

    def load(self) -> Licence:
        """Best licence among every stored source; NONE when there is none. Never raises."""
        try:
            found = [self.verify(text, origin) for origin, text in self._candidates()]
            return best(found)
        except Exception:
            return NO_LICENCE

    def stored(self) -> list:
        """Every licence kept in the app folder, verified. Never raises."""
        try:
            return [lic for lic in (self.verify(text, "app folder") for _, text in self._app_files())
                    if lic.state is not State.NONE]
        except Exception:
            return []

    def save(self, text) -> Path:
        """Store a licence in the app folder, one file per licence id (atomic; same id replaces)."""
        if self.app_dir is None:
            raise ValueError("no app_dir configured")
        token = normalise(text)
        lic_id = self.verify(token).id
        if not _SAFE_ID.match(lic_id or ""):
            raise ValueError("not a readable licence")
        self._migrate_single()
        return self._write(lic_id, token)

    def remove(self, lic_id) -> None:
        """Remove one licence from the app folder (other sources are not touched)."""
        if self.app_dir is None or not _SAFE_ID.match(lic_id or ""):
            return
        self._migrate_single()
        (self.app_dir / LICENCES_DIR / f"{lic_id}.license").unlink(missing_ok=True)

    def clear(self) -> None:
        """Remove every licence stored in the app folder (other sources are not touched)."""
        if self.app_dir is None:
            return
        (self.app_dir / LICENSE_FILE).unlink(missing_ok=True)
        folder = self.app_dir / LICENCES_DIR
        if folder.is_dir():
            for f in folder.glob("*.license"):
                f.unlink(missing_ok=True)

    def _write(self, lic_id: str, token: str) -> Path:
        folder = self.app_dir / LICENCES_DIR
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{lic_id}.license"
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".license-")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(token + "\n")
        os.replace(tmp, target)
        return target

    def _migrate_single(self) -> None:
        """Move an app folder's old single license.key into the per-id folder."""
        single = self.app_dir / LICENSE_FILE
        text = _read(single)
        if not text:
            return
        lic_id = self.verify(text).id
        if _SAFE_ID.match(lic_id or ""):
            self._write(lic_id, normalise(text))
        single.unlink(missing_ok=True)

    def _app_files(self):
        if self.app_dir is None:
            return
        text = _read(self.app_dir / LICENSE_FILE)
        if text:
            yield LICENSE_FILE, text
        folder = self.app_dir / LICENCES_DIR
        if folder.is_dir():
            for f in sorted(folder.glob("*.license"))[:MAX_STORED]:
                text = _read(f)
                if text:
                    yield f.name, text

    def _candidates(self):
        env_text = os.environ.get(f"{self.env_prefix}_LICENSE")
        if env_text:
            yield f"env {self.env_prefix}_LICENSE", env_text
        env_file = os.environ.get(f"{self.env_prefix}_LICENSE_FILE")
        if env_file:
            text = _read(Path(env_file))
            if text:
                yield f"env {self.env_prefix}_LICENSE_FILE", text
        for _, text in self._app_files():
            yield "app folder", text
        if self.shared_dir is not None:
            text = _read(self.shared_dir / LICENSE_FILE)
            if text:
                yield "shared folder", text


def best(licences) -> Licence:
    """The most useful licence: best state, then the latest end (never ending counts as latest)."""
    found = [lic for lic in licences if lic.state is not State.NONE]
    if not found:
        return NO_LICENCE
    far = datetime.max.replace(tzinfo=timezone.utc)
    return max(found, key=lambda lic: (RANK[lic.state], lic.expires or far))


def _read(path: Path) -> str:
    try:
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""
