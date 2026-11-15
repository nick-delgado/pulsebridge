"""Calls to the two private Cloud Run services: cuff-sim (D10) and health-sync.

Neither allows unauthenticated calls. Cloud Run checks an OIDC **identity token** before a
request reaches the container: a Google-signed JWT whose ``aud`` is the receiving service's URL,
issued to the proxy's own service account, which T22 gives ``roles/run.invoker`` on both. The
proxy never forwards the reviewer's session token; it attaches its own identity.

Token minting is behind :class:`IdTokenSource` so tests use a fake: no metadata server, no
credentials. In production :class:`GoogleIdTokens` asks the metadata server through
``google-auth`` (``fetch_id_token_credentials``), keeps one credential per audience and refreshes
it when it expires (Google's ID tokens last an hour).

**Forwarding a cuff request** (D10, cuff-sim's README):

- only after the proxy's own login check (the route is behind the token check like every other);
- the body bytes go **unchanged** (cuff-sim refuses unknown fields itself, so nothing is
  stripped or added here), with ``Content-Type`` only when the caller sent a JSON type;
- cuff-sim's status, body and ``Retry-After`` come back unchanged for 200, 400, 413, 422, 429 and
  500, the statuses its README documents;
- any other status from cuff-sim's side (Cloud Run's 401/403 when the invoker binding is
  missing, 404, 502, 503) is a ``502``: the simulator could not be used; Cloud Run's own ``504``
  (cuff-sim's request timeout) is a ``504``, because the attempt may have been sent;
- no connection is a ``502``; no answer within the timeout (``CUFF_FORWARD_TIMEOUT_SECONDS``,
  default 55 s: below Firebase Hosting's 60 s, README "For T22") is a ``504``. Each with an
  OperationOutcome from the proxy, never cuff-sim's text or URL.

No redirect is followed (a redirect could carry the identity token elsewhere), and an answer
larger than 256 KiB is refused.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fhir_proxy import deadline

PASSTHROUGH_STATUSES = (200, 400, 413, 422, 429, 500)
MAX_RESPONSE_BYTES = 256 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
JSON_TYPES = ("application/json", "application/fhir+json")
TOKEN_FETCH_TIMEOUT_SECONDS = 10.0


class UpstreamError(Exception):
    """The service could not be used. ``reason`` is a fixed code; ``status`` the HTTP status
    when there was an answer."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class UpstreamTimeout(UpstreamError):
    def __init__(self) -> None:
        super().__init__("timeout")


class IdTokenSource(Protocol):
    def token(self, audience: str) -> str: ...


class GoogleIdTokens:
    """Identity tokens from the metadata server, one cached credential per audience."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._credentials: dict[str, object] = {}

    def token(self, audience: str) -> str:
        import google.auth.exceptions
        import google.auth.transport.requests
        import google.oauth2.id_token

        class _BoundedRequest(google.auth.transport.requests.Request):
            def __call__(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
                kwargs["timeout"] = TOKEN_FETCH_TIMEOUT_SECONDS
                return super().__call__(*args, **kwargs)

        request = _BoundedRequest()
        try:
            with self._lock:
                credentials = self._credentials.get(audience)
                if credentials is None:
                    credentials = google.oauth2.id_token.fetch_id_token_credentials(
                        audience, request=request
                    )
                    self._credentials[audience] = credentials
                if not credentials.valid:  # type: ignore[attr-defined]
                    credentials.refresh(request)  # type: ignore[attr-defined]
                return credentials.token  # type: ignore[attr-defined]
        except google.auth.exceptions.GoogleAuthError:
            raise UpstreamError("identity-token-unavailable") from None


@dataclass(frozen=True)
class Forwarded:
    status: int
    body: bytes
    content_type: str
    retry_after: str | None


class HttpSender(Protocol):
    """One HTTP request; production uses ``requests``. Raises :class:`UpstreamTimeout` or
    :class:`UpstreamError`."""

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]: ...


def requests_sender() -> HttpSender:
    import requests

    session = requests.Session()
    session.trust_env = False  # no proxy settings or .netrc credentials from the environment

    def send(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        deadline = time.monotonic() + timeout
        try:
            response = session.request(
                method,
                url,
                data=body,
                headers=headers,
                timeout=(min(CONNECT_TIMEOUT_SECONDS, timeout), timeout),
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout:
            raise UpstreamTimeout() from None
        except requests.RequestException:
            raise UpstreamError("unreachable") from None
        try:
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=16384):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise UpstreamError("answer-too-large", response.status_code)
                if time.monotonic() > deadline:
                    raise UpstreamTimeout()
                chunks.append(chunk)
        except requests.Timeout:
            raise UpstreamTimeout() from None
        except requests.RequestException:
            raise UpstreamError("unreachable") from None
        finally:
            response.close()
        return (
            response.status_code,
            {k.lower(): v for k, v in response.headers.items()},
            b"".join(chunks),
        )

    return send


def _json_type(value: str | None) -> str | None:
    """The media type if it is a JSON one (parameters such as charset dropped), else None."""
    if not value:
        return None
    media = value.split(";", 1)[0].strip().lower()
    return media if media in JSON_TYPES else None


class Upstream:
    def __init__(
        self,
        *,
        tokens: IdTokenSource,
        send: HttpSender,
        cuff_sim_url: str,
        health_sync_url: str | None,
        cuff_timeout: float,
        samples_timeout: float,
        health_sync_timeout: float,
    ) -> None:
        self._tokens = tokens
        self._send = send
        self._cuff = cuff_sim_url
        self._health = health_sync_url
        self._cuff_timeout = cuff_timeout
        self._samples_timeout = samples_timeout
        self._health_timeout = health_sync_timeout

    def _call(
        self,
        method: str,
        base: str,
        path: str,
        *,
        body: bytes | None,
        content_type: str | None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        # Every call ends within the request's budget (fhir_proxy.deadline): the cuff route's
        # budget is the forward timeout; every other route's is REQUEST_BUDGET_SECONDS.
        try:
            timeout = deadline.capped(timeout)
        except deadline.BudgetExhausted:
            raise UpstreamTimeout() from None
        # Audience: the receiving service's URL, which Cloud Run accepts for its invoker check.
        headers = {
            "Authorization": f"Bearer {self._tokens.token(base)}",
            "Accept": "application/json",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        return self._send(method, base + path, headers=headers, body=body, timeout=timeout)

    def _forward(
        self, method: str, path: str, body: bytes | None, content_type: str | None, timeout: float
    ) -> Forwarded:
        status, headers, data = self._call(
            method, self._cuff, path, body=body, content_type=content_type, timeout=timeout
        )
        if status == 504:
            raise UpstreamError("cut-off", status)
        media = _json_type(headers.get("content-type"))
        if status not in PASSTHROUGH_STATUSES or media is None:
            raise UpstreamError("bad-status", status)
        retry_after = headers.get("retry-after")
        if retry_after is not None and not (retry_after.isascii() and retry_after.isdigit()):
            retry_after = None  # only delay-seconds (RFC 9110 10.2.3) is passed on
        return Forwarded(status, data, media, retry_after if status == 429 else None)

    def cuff_reading(self, body: bytes, content_type: str | None) -> Forwarded:
        return self._forward(
            "POST", "/cuff/readings", body, _json_type(content_type), self._cuff_timeout
        )

    def cuff_samples(self) -> Forwarded:
        return self._forward("GET", "/cuff/samples", None, None, self._samples_timeout)

    def health_sync_status(self) -> object:
        """health-sync's ``/status`` body. Raises :class:`UpstreamError` for anything but a
        200 with JSON (any other answer means health-sync failed, its README says)."""
        if self._health is None:
            raise UpstreamError("not-deployed")
        status, headers, data = self._call(
            "GET",
            self._health,
            "/status",
            body=None,
            content_type=None,
            timeout=self._health_timeout,
        )
        if status != 200:
            raise UpstreamError("error", status)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UpstreamError("bad-answer", status) from None

    def health_sync_caller(self) -> Callable[[], object] | None:
        return None if self._health is None else self.health_sync_status
