"""HTTP to the stack's front door, and the small API client the checks use.

**One front door.** Every request goes to ``PULSEBRIDGE_BASE_URL`` (``<dashboard_url>/api``):
the fhir-proxy behind the Firebase Hosting rewrite, the same way the dashboard reaches it
(fhir-proxy README, "One front door"). Nothing here talks to Cloud Healthcare, cuff-sim,
health-sync or OpenEMR directly: what the smoke test asserts is what the public API says.

**No redirects.** A redirect would carry the ``Authorization`` header to wherever the
``Location`` points; :class:`UrllibTransport` never follows one (a 3xx comes back as a response
and fails the check that expected a 200).

**Nothing secret is logged.** ``--verbose`` prints one line per request: method, path, status
and duration. Never headers, never bodies, never the query string of a login (there is none).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from pulsebridge_smoke.redact import Redactor

DEFAULT_TIMEOUT_SECONDS = 70.0  # above the proxy's own 55 s cuff budget, so its 504 arrives
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")  # urlsplit's hostname drops the brackets
NO_BODY = object()


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)  # names in lower case
    elapsed: float = 0.0

    def json(self) -> object | None:
        """The body as JSON, or None when it is not JSON (a check then fails on the shape)."""
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


class TransportError(Exception):
    """No HTTP answer at all (DNS, connection refused, TLS, timeout). The message names the
    error class and urllib's reason; it never contains a header or body."""


class Transport(Protocol):
    def send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response: ...


def check_base_url(base_url: str) -> str:
    """The front door's address without a trailing slash. ``https`` only, except for a local
    address (a proxy started on this machine). No query, fragment or user info."""
    parts = urlsplit(base_url)
    local = parts.hostname in LOCAL_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValueError("the base URL must start with https:// (http:// only for localhost)")
    if not parts.hostname or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("the base URL must be https://<host>[/<path>] with no query or fragment")
    return base_url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None  # urllib then raises HTTPError with the 3xx, which send() returns as is


class UrllibTransport:
    """The real transport: the standard library's urllib, system CA bundle, the environment's
    HTTPS proxy settings, redirects refused."""

    def __init__(self, base_url: str) -> None:
        self.base_url = check_base_url(base_url)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:
        request = urllib.request.Request(  # noqa: S310 - the scheme is checked above
            self.base_url + path, data=body, method=method, headers=dict(headers)
        )
        try:
            with self._opener.open(request, timeout=timeout) as answer:
                return Response(
                    answer.status,
                    answer.read(),
                    {k.lower(): v for k, v in answer.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            with exc:
                return Response(
                    exc.code, exc.read(), {k.lower(): v for k, v in (exc.headers or {}).items()}
                )
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            raise TransportError(
                f"{method} {path}: no answer ({type(exc).__name__}: {reason or exc})"
            ) from None


def problem_reason(response: Response) -> str:
    """The proxy's fixed reason code from an OperationOutcome (``issue[0].details.coding[0]``),
    or the issue code, or ``"-"``. Never the body itself: a body is not shown in full."""
    body = response.json()
    if isinstance(body, dict) and isinstance(body.get("operationOutcome"), dict):
        body = body["operationOutcome"]  # the share's 502/504 envelope
    if not isinstance(body, dict) or body.get("resourceType") != "OperationOutcome":
        return "-"
    issues = body.get("issue")
    issue = issues[0] if isinstance(issues, list) and issues and isinstance(issues[0], dict) else {}
    details = issue.get("details") if isinstance(issue.get("details"), dict) else {}
    codings = details.get("coding") if isinstance(details.get("coding"), list) else []
    if codings and isinstance(codings[0], dict) and isinstance(codings[0].get("code"), str):
        return codings[0]["code"]
    return issue.get("code") if isinstance(issue.get("code"), str) else "-"


class LoginFailed(Exception):
    pass


class ApiClient:
    """Calls the proxy with the session token. ``log`` receives one line per request when
    verbose (method, path, status, milliseconds), already redacted."""

    def __init__(
        self,
        transport: Transport,
        redactor: Redactor,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._transport = transport
        self._redact = redactor
        self._timeout = timeout
        self._monotonic = monotonic
        self._log = log
        self._token: str | None = None

    def call(self, method: str, path: str, body: object = NO_BODY) -> Response:
        headers = {"Accept": "application/json"}
        data = None
        if body is not NO_BODY:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        started = self._monotonic()
        response = self._transport.send(
            method, path, headers=headers, body=data, timeout=self._timeout
        )
        elapsed = self._monotonic() - started
        if self._log is not None:
            self._log(
                self._redact(f"  {method} {path} -> {response.status} ({elapsed * 1000:.0f} ms)")
            )
        return Response(response.status, response.body, response.headers, elapsed)

    def login(self, password: str) -> Response:
        """``POST /auth/login``. The password is registered with the redactor before it is
        sent, the token as soon as it arrives; neither is returned or stored anywhere else."""
        self._redact.add(password)
        response = self.call("POST", "/auth/login", {"password": password})
        body = response.json()
        token = body.get("token") if isinstance(body, dict) else None
        if response.status != 200 or not isinstance(token, str) or not token:
            raise LoginFailed(
                f"POST /auth/login answered HTTP {response.status} "
                f"(reason {problem_reason(response)}); expected 200 with a token"
            )
        self._redact.add(token)
        self._token = token
        return response
