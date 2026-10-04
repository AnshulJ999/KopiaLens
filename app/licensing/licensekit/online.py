"""Optional online client for the licence service. Never raises: every call returns an OnlineResult.

It never touches the stored licence; the app saves a returned licence itself (LicenseKit.save)."""
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlencode

OFFLINE_MESSAGE = "Couldn't reach the licence service. Check your connection and try again."
UNAVAILABLE_MESSAGE = "The licence service isn't available right now. Try again later."
# Replies that mean the service itself is missing or blocked (Cloudflare edge errors, outages),
# not a judgement on the key.
UNAVAILABLE_STATUS = frozenset({403, 404, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530})


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
    def __init__(self, base_url: str, product: str, timeout: float = 15, user_agent: str = ""):
        self.base_url = base_url.rstrip("/")
        self.product = product
        self.timeout = timeout
        # Cloudflare refuses Python's default "Python-urllib" agent (error 1010).
        self.user_agent = user_agent or f"LicenseKit ({product})"

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
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "User-Agent": self.user_agent},
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
        message = UNAVAILABLE_MESSAGE if status in UNAVAILABLE_STATUS else "The licence service sent an unexpected reply."
        return OnlineResult(ok=False, status=status, error="bad_response", message=message)
    known = {"licence", "licence_id", "plan", "expires", "activation_id", "error", "message"}
    licence = data.get("licence") if isinstance(data.get("licence"), str) else ""
    ok = status == 200 and bool(licence)
    message = str(data.get("message") or "")
    if not ok and not message and status != 202:
        message = UNAVAILABLE_MESSAGE if status in UNAVAILABLE_STATUS else f"The licence service refused the request (error {status})."
    return OnlineResult(
        ok=ok,
        status=status,
        licence=licence,
        licence_id=str(data.get("licence_id") or ""),
        plan=str(data.get("plan") or ""),
        expires=data.get("expires") if isinstance(data.get("expires"), str) else None,
        activation_id=str(data.get("activation_id") or ""),
        error=str(data.get("error") or ""),
        message=message,
        extra={k: v for k, v in data.items() if k not in known},
    )
