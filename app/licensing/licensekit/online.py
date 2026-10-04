"""Optional online client for the licence service. Never raises: every call returns an OnlineResult.

It never touches the stored licence; the app saves a returned licence itself (LicenseKit.save)."""
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlencode

OFFLINE_MESSAGE = "Couldn't reach the licence service. Check your connection and try again."


@dataclass(frozen=True)
class OnlineResult:
    ok: bool
    status: int = 0
    licence: str = field(default="", repr=False)
    licence_id: str = ""
    plan: str = ""
    expires: str | None = None
    activation_id: str = ""
    error: str = ""
    message: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        return self.status == 202


class LicenseService:
    def __init__(self, base_url: str, product: str, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.product = product
        self.timeout = timeout

    def activate(self, key: str, label: str = "") -> OnlineResult:
        body = {"product": self.product, "key": (key or "").strip()}
        if label:
            body["label"] = label[:100]
        return self._post("/v1/activate", body)

    def renew(self, licence: str, key: str = "", activation_id: str = "") -> OnlineResult:
        body = {"licence": (licence or "").strip()}
        if key:
            body["key"] = key.strip()
        if activation_id:
            body["activation_id"] = activation_id
        return self._post("/v1/renew", body)

    def patreon_start(self) -> tuple:
        """(url to open in the browser, session code to poll with patreon_claim)."""
        code = secrets.token_urlsafe(32)
        return f"{self.base_url}/patreon/start?{urlencode({'s': code, 'product': self.product})}", code

    def patreon_claim(self, session: str) -> OnlineResult:
        """ok once signed in; .pending while the browser step is unfinished."""
        return self._post("/v1/patreon/claim", {"session": session})

    def _post(self, path: str, body: dict) -> OnlineResult:
        try:
            req = urllib.request.Request(
                self.base_url + path,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return _result(resp.status, resp.read())
            except urllib.error.HTTPError as err:
                return _result(err.code, err.read())
        except Exception:
            return OnlineResult(ok=False, error="offline", message=OFFLINE_MESSAGE)


def _result(status: int, raw: bytes) -> OnlineResult:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return OnlineResult(ok=False, status=status, error="bad_response", message="The licence service sent an unexpected reply.")
    known = {"licence", "licence_id", "plan", "expires", "activation_id", "error", "message"}
    licence = data.get("licence") if isinstance(data.get("licence"), str) else ""
    return OnlineResult(
        ok=status == 200 and bool(licence),
        status=status,
        licence=licence,
        licence_id=str(data.get("licence_id") or ""),
        plan=str(data.get("plan") or ""),
        expires=data.get("expires") if isinstance(data.get("expires"), str) else None,
        activation_id=str(data.get("activation_id") or ""),
        error=str(data.get("error") or ""),
        message=str(data.get("message") or ""),
        extra={k: v for k, v in data.items() if k not in known},
    )
