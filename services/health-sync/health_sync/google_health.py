"""A small client for the two Google Health API calls health-sync makes, and the OAuth refresh.

Calls (Google Health API v4, base ``https://health.googleapis.com/``; request and response field
names from the API's discovery document, confirmed against spike S3's live calls):

- ``POST v4/users/me/dataTypes/heart-rate/dataPoints:rollUp`` with a JSON body
  ``{"range": {"startTime", "endTime"}, "windowSize": "300s", "pageSize", "pageToken"?}``.
  The response is ``{"rollupDataPoints": [...], "nextPageToken"?}``, newest window first,
  windows with no data left out. ``range`` is closed-open and at most 14 days for heart rate.
- ``GET v4/users/me/dataTypes/heart-rate/dataPoints?filter=...&pageSize=1``: one raw data point,
  the newest in the range, read only for its ``dataSource`` (device name, platform, recording
  method), which roll-ups do not carry (spike S3).
- ``POST https://oauth2.googleapis.com/token`` with ``grant_type=refresh_token``: a new access
  token from the refresh token (Google Identity, "Refreshing an access token").

Everything goes through an injected transport (the ``pulsebridge_gcp`` ``Transport`` protocol: one
method, ``request(...) -> HttpResponse``), so tests script Google's answers and nothing here
touches the network.

What is never logged or put into an exception message: the access token, the refresh token, the
client id and secret, and any response body (a roll-up body is health data; a token body is a
credential). Exceptions carry the operation, the HTTP status and a short code only: an OAuth
error code such as ``invalid_grant`` or a Google ``ErrorInfo.reason`` such as
``MISSING_OAUTH_SCOPE``, both fixed vocabularies, checked against a pattern before use.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode

from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.transport import HttpResponse, Transport

from health_sync.credentials import OAuthCredentials
from health_sync.retry import Deadline, RetryPolicy, parse_retry_after

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a password
API_BASE_URL = "https://health.googleapis.com/v4"
# The data type is kebab-case in the path and snake_case in a filter (spike S3).
ROLLUP_URL = f"{API_BASE_URL}/users/me/dataTypes/heart-rate/dataPoints:rollUp"
LIST_URL = f"{API_BASE_URL}/users/me/dataTypes/heart-rate/dataPoints"
WINDOW_SIZE = "300s"  # google-duration; decision D8, five-minute windows
# 14 days of five-minute windows is 4,032, so one page holds a whole maximal request (the API
# default is 1,440 and the maximum 10,000). nextPageToken is followed anyway.
ROLLUP_PAGE_SIZE = 4032
MAX_PAGES = 50  # a misbehaving page token cannot loop for ever
# An access token is replaced this long before Google says it expires, so a token never expires
# between the check and its use.
TOKEN_EXPIRY_MARGIN_SECONDS = 60.0

_ENUM = re.compile(r"[A-Z][A-Z0-9_]{0,63}", re.ASCII)

logger = logging.getLogger("health_sync")


# ---- errors -----------------------------------------------------------------------------------


class GoogleHealthError(Exception):
    """A Google Health or OAuth call failed. ``str()`` is safe to log."""

    def __init__(self, operation: str, status: int | None, code: str) -> None:
        shown = f"HTTP {status}" if status is not None else "no HTTP response"
        super().__init__(f"{operation}: {code} ({shown})")
        self.operation = operation
        self.status = status
        self.code = code


class ReconnectNeeded(GoogleHealthError):
    """The user's authorisation no longer works; only reconnecting (T11's script) fixes it.

    ``code``:
    - ``refresh-token-invalid``: OAuth ``invalid_grant``: the refresh token expired or was
      revoked. In Testing status Google issues refresh tokens that expire after 7 days.
    - ``oauth-client-rejected``: ``invalid_client``, ``unauthorized_client`` or
      ``deleted_client``: the OAuth client id or secret is wrong, or the client was deleted.
    - ``scope-not-granted``: the API answered 403 ``MISSING_OAUTH_SCOPE`` (the user unticked the
      scope on the consent screen). The token response's ``scope`` list is only logged, not
      trusted to fail a run: an exact-string comparison could fail on a harmless difference.
    """


class GoogleHealthUnavailable(GoogleHealthError):
    """429, 408, 5xx or no response, still failing after the bounded retries."""


class GoogleHealthRejected(GoogleHealthError):
    """Any other refusal (a 4xx, 401 again after a fresh token) or a malformed response. Sending
    the same request again will not help."""


# ---- one HTTP exchange, with bounded retries -------------------------------------------------


def _is_retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


@dataclass
class HttpSender:
    transport: Transport
    policy: RetryPolicy
    deadline: Deadline
    timeout: float

    def send(
        self,
        operation: str,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        headers: dict[str, str],
    ) -> HttpResponse:
        """Send once, and again on a retryable answer while attempts and the deadline allow."""
        attempt = 0
        while True:
            attempt += 1
            # Cut to the time left; raises DeadlineReached instead of starting a request that
            # could not finish before the run's deadline.
            timeout = self.deadline.timeout_for(self.timeout, operation)
            retry_after = None
            try:
                response = self.transport.request(
                    method, url, params=params, body=body, headers=headers, timeout=timeout
                )
            except RetryableError:  # pulsebridge_gcp's transports: no HTTP response at all
                status = None
            else:
                if not _is_retryable(response.status):
                    return response
                status = response.status
                retry_after = parse_retry_after(response.headers.get("retry-after"))
            delay = self.policy.delay(attempt, retry_after)
            if attempt >= self.policy.max_attempts or not self.deadline.allows(delay):
                raise GoogleHealthUnavailable(operation, status, "retries-exhausted")
            logger.warning(
                "google health request retried",
                extra={
                    "fields": {
                        "event": "google-retry",
                        "operation": operation,
                        "http_status": status,
                        "attempt": attempt,
                        "delay_seconds": delay,
                    }
                },
            )
            self.policy.sleep(delay)


def _json_object(operation: str, response: HttpResponse) -> dict:
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        raise GoogleHealthRejected(operation, response.status, "malformed-response")
    return body


def _error_body(response: HttpResponse) -> dict:
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _google_reason(response: HttpResponse) -> str | None:
    """``error.details[].reason`` of Google's error envelope (``google.rpc.ErrorInfo``), for
    example ``MISSING_OAUTH_SCOPE``; else ``error.status`` such as ``PERMISSION_DENIED``."""
    error = _error_body(response).get("error")
    if not isinstance(error, dict):
        return None
    for detail in error.get("details") or []:
        reason = detail.get("reason") if isinstance(detail, dict) else None
        if isinstance(reason, str) and _ENUM.fullmatch(reason):
            return reason
    status = error.get("status")
    return status if isinstance(status, str) and _ENUM.fullmatch(status) else None


# ---- OAuth: access tokens from the refresh token ---------------------------------------------


@dataclass
class _AccessToken:
    value: str = field(repr=False)
    expires_at: float  # monotonic seconds


# OAuth 2.0 error codes that may be echoed into a reason: RFC 6749 section 5.2 (token endpoint)
# and 4.1.2.1 (server_error, temporarily_unavailable), plus the codes Google documents for its
# token endpoint. Anything else, including a value that looks like a token, is shown as
# "refused", so nothing a server sends can reach a log line unchecked.
KNOWN_OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "access_denied",
        "server_error",
        "temporarily_unavailable",
        "deleted_client",
        "disabled_client",
        "admin_policy_enforced",
        "invalid_rapt",
    }
)

# The subset that means the stored authorisation itself is unusable: retrying cannot help,
# reconnecting can.
_RECONNECT_CODES = {
    "invalid_grant": "refresh-token-invalid",
    "invalid_client": "oauth-client-rejected",
    "unauthorized_client": "oauth-client-rejected",
    "deleted_client": "oauth-client-rejected",
}


class TokenSource:
    """Access tokens for one run. Refreshes on first use and when the token is about to expire,
    and reuses it otherwise. Nothing is kept between runs: each run reads the secret files again,
    so a new refresh token is used at once."""

    OPERATION = "POST oauth2 token"

    def __init__(
        self,
        credentials: OAuthCredentials,
        *,
        scope: str,
        http: HttpSender,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._scope = scope
        self._http = http
        self._clock = clock
        self._token: _AccessToken | None = None
        self.refreshes = 0
        self.rotated_refresh_token_ignored = False
        self.scope_not_listed = False

    def token(self) -> str:
        if self._token is None or self._clock() >= self._token.expires_at:
            self._token = self.refresh()
        return self._token.value

    def invalidate(self) -> None:
        self._token = None

    def refresh(self) -> _AccessToken:
        """``POST https://oauth2.googleapis.com/token`` (form-encoded): ``client_id``,
        ``client_secret``, ``refresh_token``, ``grant_type=refresh_token``. The answer holds
        ``access_token``, ``expires_in`` (seconds), ``scope`` and ``token_type`` ``Bearer``."""
        creds = self._credentials
        form = urlencode(
            {
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "refresh_token": creds.refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("ascii")
        response = self._http.send(
            self.OPERATION,
            "POST",
            TOKEN_URL,
            body=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        self.refreshes += 1
        if response.status in (400, 401):
            # RFC 6749 5.2: {"error": "<code>", "error_description": "..."}. Only the code is
            # used; the description is free text and is dropped.
            code = _error_body(response).get("error")
            if isinstance(code, str) and code in _RECONNECT_CODES:
                raise ReconnectNeeded(self.OPERATION, response.status, _RECONNECT_CODES[code])
            shown = code if isinstance(code, str) and code in KNOWN_OAUTH_ERRORS else "refused"
            raise GoogleHealthRejected(self.OPERATION, response.status, shown)
        if not 200 <= response.status < 300:
            raise GoogleHealthRejected(self.OPERATION, response.status, "unexpected-status")
        body = _json_object(self.OPERATION, response)
        value = body.get("access_token")
        expires_in = body.get("expires_in")
        token_type = body.get("token_type")
        if (
            not isinstance(value, str)
            or not value
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
            or not (isinstance(token_type, str) and token_type.lower() == "bearer")
        ):
            raise GoogleHealthRejected(self.OPERATION, response.status, "malformed-response")
        granted = body.get("scope")
        if isinstance(granted, str) and self._scope not in granted.split():
            # Not fatal: the API itself answers 403 MISSING_OAUTH_SCOPE if the scope is really
            # missing, which is reconnect-needed. Only the fact is logged, never the list.
            self.scope_not_listed = True
            logger.warning(
                "configured scope not listed in the token response",
                extra={"fields": {"event": "scope-not-listed"}},
            )
        if "refresh_token" in body:
            # Google normally keeps the refresh token on a refresh (identity README). health-sync
            # can only read the secret, so a rotated one cannot be stored here; it is noted (as a
            # flag, never the value) so the deployer can reconnect before the old one stops.
            self.rotated_refresh_token_ignored = True
            logger.warning(
                "token response carried a new refresh token; it was not stored",
                extra={"fields": {"event": "refresh-token-rotated"}},
            )
        margin = min(TOKEN_EXPIRY_MARGIN_SECONDS, expires_in / 2)
        return _AccessToken(value=value, expires_at=self._clock() + expires_in - margin)


# ---- the Google Health API -------------------------------------------------------------------


def google_time(value: datetime) -> str:
    """RFC 3339 in UTC, whole seconds, ``Z``: the format the API returns and accepts."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RollupResult:
    items: list[dict]  # rollupDataPoints, every page, in the order received
    pages: int


@dataclass(frozen=True)
class DataSourceInfo:
    """What the newest raw data point in a range says about its source (spike S3). Each part is
    ``None`` when the API did not report it, so nothing is asserted that it did not say."""

    device_name: str | None
    platform: str | None
    recording_method: str | None


_NO_SOURCE = DataSourceInfo(None, None, None)
MAX_DEVICE_NAME = 200


def _enum_value(value: object) -> str | None:
    """A Google enum such as ``FITBIT`` or ``PASSIVELY_MEASURED``; ``*_UNSPECIFIED`` and
    ``UNKNOWN`` say nothing, so they become ``None``."""
    if not isinstance(value, str) or not _ENUM.fullmatch(value):
        return None
    if value.endswith("_UNSPECIFIED") or value == "UNKNOWN":
        return None
    return value


def _device_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > MAX_DEVICE_NAME or any(ch < " " or ch == "\x7f" for ch in name):
        return None
    return name


class GoogleHealthClient:
    def __init__(self, tokens: TokenSource, http: HttpSender) -> None:
        self._tokens = tokens
        self._http = http
        self.statuses: list[int] = []  # every final HTTP status, for the run's log line

    def _call(
        self,
        operation: str,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> dict:
        """One API call with the access token. A 401 means the token was not accepted: get a
        fresh one and try once more; a second 401 fails (the token endpoint just vouched for the
        grant, so something else is wrong and repeating would only loop)."""
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self._tokens.token()}",
                "Accept": "application/json",
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"
            response = self._http.send(
                operation, method, url, params=params, body=payload, headers=headers
            )
            self.statuses.append(response.status)
            if response.status == 401 and attempt == 1:
                self._tokens.invalidate()
                continue
            if 200 <= response.status < 300:
                return _json_object(operation, response)
            reason = _google_reason(response)
            if response.status == 403 and reason == "MISSING_OAUTH_SCOPE":
                raise ReconnectNeeded(operation, 403, "scope-not-granted")
            if response.status == 401:
                raise GoogleHealthRejected(operation, 401, "unauthorized-after-refresh")
            raise GoogleHealthRejected(operation, response.status, reason or "refused")
        raise AssertionError("unreachable")  # pragma: no cover

    def rollup(self, start: datetime, end: datetime) -> RollupResult:
        """Every five-minute window in ``[start, end)``, following ``nextPageToken``.

        The page token goes in the request **body** with every other field unchanged
        (discovery document: "All other request fields need to be the same as in the initial
        request when the page token is specified")."""
        operation = "POST heart-rate dataPoints:rollUp"
        request: dict = {
            "range": {"startTime": google_time(start), "endTime": google_time(end)},
            "windowSize": WINDOW_SIZE,
            "pageSize": ROLLUP_PAGE_SIZE,
        }
        items: list[dict] = []
        seen_tokens: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            body = self._call(operation, "POST", ROLLUP_URL, body=request)
            page_items = body.get("rollupDataPoints", [])
            if not isinstance(page_items, list):
                raise GoogleHealthRejected(operation, 200, "malformed-response")
            items.extend(page_items)
            token = body.get("nextPageToken")
            if not token:  # absent or "": the last page
                return RollupResult(items=items, pages=page)
            if not isinstance(token, str) or token in seen_tokens:
                raise GoogleHealthRejected(operation, 200, "bad-page-token")
            seen_tokens.add(token)
            request = {**request, "pageToken": token}
        raise GoogleHealthRejected(operation, 200, "too-many-pages")

    def latest_data_source(self, start: datetime | None, end: datetime) -> DataSourceInfo:
        """The ``dataSource`` of the newest raw heart-rate point in ``[start, end)``, or before
        ``end`` when ``start`` is ``None`` (the list method returns newest first; ``pageSize=1``
        keeps it to one point)."""
        operation = "GET heart-rate dataPoints"
        field_name = "heart_rate.sample_time.physical_time"
        upper = f'{field_name} < "{google_time(end)}"'
        lower = f'{field_name} >= "{google_time(start)}" AND ' if start is not None else ""
        params = {"filter": lower + upper, "pageSize": "1"}
        body = self._call(operation, "GET", LIST_URL, params=params)
        points = body.get("dataPoints") or []
        if not isinstance(points, list) or not points or not isinstance(points[0], dict):
            return _NO_SOURCE
        source = points[0].get("dataSource")
        if not isinstance(source, dict):
            return _NO_SOURCE
        device = source.get("device")
        return DataSourceInfo(
            device_name=_device_name(device.get("displayName"))
            if isinstance(device, dict)
            else None,
            platform=_enum_value(source.get("platform")),
            recording_method=_enum_value(source.get("recordingMethod")),
        )
