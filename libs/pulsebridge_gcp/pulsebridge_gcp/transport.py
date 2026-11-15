"""The HTTP layer under :class:`pulsebridge_gcp.HealthcareClient`, kept behind a tiny interface.

The client never talks to the network itself. It hands each request to a :class:`Transport`,
which returns an :class:`HttpResponse`. In production that is :class:`RequestsTransport` around
``google.auth.transport.requests.AuthorizedSession``, which adds the OAuth access token and
refreshes it. Tests pass :class:`pulsebridge_gcp.testing.FakeTransport` instead, so no test needs
credentials or makes a network call.

Why ``requests``: ``google-auth`` ships its authorised session for ``requests``
(``AuthorizedSession``), which handles token refresh and the retry-once-on-401 dance. Any other
client would need that code written again. It is synchronous, which suits a Cloud Run handler
that makes three calls in a row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pulsebridge_gcp.errors import RetryableError

# Query parameters: a mapping, or (name, value) pairs when a name repeats (FHIR search).
Params = Mapping[str, str] | Sequence[tuple[str, str]] | None

# The only OAuth scope the Cloud Healthcare API needs; IAM roles decide what is allowed.
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


@dataclass(frozen=True)
class HttpResponse:
    """What came back: status, raw body bytes and headers (names in lower case)."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> object:
        """The body parsed as JSON. Raises ``ValueError`` if it is not JSON."""
        return json.loads(self.body.decode("utf-8"))


class Transport(Protocol):
    """Sends one HTTP request. Raises :class:`RetryableError` if no response arrived at all."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Params,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class RequestsTransport:
    """A :class:`Transport` over a ``requests.Session`` (normally an ``AuthorizedSession``)."""

    def __init__(self, session: object) -> None:
        self._session = session

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Params,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        # Imported here so that importing the library (and every test) does not need them.
        import google.auth.exceptions
        import requests

        operation = f"{method} {url.split('?', 1)[0]}"
        try:
            response = self._session.request(  # type: ignore[attr-defined]
                method, url, params=params, data=body, headers=dict(headers), timeout=timeout
            )
        except requests.RequestException as exc:
            # Connection refused, reset, DNS failure, read timeout: no HTTP status to judge by.
            raise RetryableError(operation, None, server_message=type(exc).__name__) from exc
        except google.auth.exceptions.GoogleAuthError as exc:
            # The access token could not be refreshed (for example the metadata server did not
            # answer). Nothing was sent; the same call may work in a moment.
            raise RetryableError(operation, None, server_message=type(exc).__name__) from exc
        return HttpResponse(
            status=response.status_code,
            body=response.content,
            headers={k.lower(): v for k, v in response.headers.items()},
        )


def default_transport() -> RequestsTransport:
    """A transport using Application Default Credentials.

    On Cloud Run these are the service's own service account, fetched from the metadata server;
    no key file is involved. Locally, ``gcloud auth application-default login`` provides them.
    """
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    return RequestsTransport(AuthorizedSession(credentials))
