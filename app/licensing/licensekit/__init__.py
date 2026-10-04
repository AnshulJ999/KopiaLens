"""LicenseKit: offline licence verification for apps. Verifies only; never signs."""
from .kit import LICENCES_DIR, LICENSE_FILE, LicenseKit, best
from .model import NO_LICENCE, Licence, State
from .online import LicenseService, OnlineResult
from .verify import GRACE, RENEW_WINDOW, normalise, verify

__version__ = "1.2.0"

__all__ = [
    "LicenseKit", "Licence", "State", "NO_LICENCE", "LICENSE_FILE", "LICENCES_DIR", "best",
    "verify", "normalise", "GRACE", "RENEW_WINDOW", "LicenseService", "OnlineResult", "__version__",
]
