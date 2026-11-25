"""Start KopiaLens as ``main.py`` does, with LicenseKit's public test key trusted.

Test runs only: the shipped app trusts its own keys alone. With the test key trusted, a test can
activate LicenseKit's known-answer licence through the real licence code and drive Pro flows. The
shared licence folder moves under the isolated APPDATA so nothing reaches the real home folder.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(APP_ROOT))

from _harness import known_answer_licence  # noqa: E402  (this folder is sys.path[0])

from app.licensing import licence  # noqa: E402

_lic = known_answer_licence()
licence.PUBLIC_KEYS = {**licence.PUBLIC_KEYS, _lic["kid"]: _lic["public_key"]}
licence.SHARED_DIR = Path(os.environ["APPDATA"]) / ".licensekit"

sys.argv = [str(APP_ROOT / "main.py")]
runpy.run_path(str(APP_ROOT / "main.py"), run_name="__main__")
