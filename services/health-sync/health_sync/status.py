"""Is the Google Health connection working right now? Answered statelessly, by trying it.

The status route (``GET /status``) reads the three secret files and asks Google's token endpoint
for an access token, exactly as a sync run would, and reports ``googleHealth``:

- ``connected``: the refresh succeeded.
- ``reconnect-needed``: ``invalid_grant`` (the refresh token expired or was revoked) or the
  OAuth client was refused.
- ``not-configured``: a secret file is missing or empty (``secrets-missing:<names>``), or
  present but unusable (``secret-unreadable:<names>``).
- ``unknown``: the check itself could not finish (token endpoint 429/5xx/unreachable, or an
  unexpected answer).

All four answer HTTP 200, so the proxy (T14) can tell "the check could not finish" from "the
service is down" (any other status, or no answer).

``notes`` carries non-fatal facts the dashboard may show:

- ``refresh-token-rotated``: Google returned a new refresh token, which this service cannot
  store; reconnecting soon keeps the connection working.
- ``backfill-needed``: ``strandedPreliminary`` stored windows are still ``preliminary`` but
  older than any future run's range (missed runs); ``health-sync-backfill`` finalises them.

``strandedPreliminary`` is the count the most recent sync run on this instance made (the
caller passes it in); ``None`` when no run has counted since the instance started, or the last
count could not be made. The status check itself makes no FHIR request, so it stays inside its
20-second budget whatever the store is doing.

There is nothing to store and nothing to go stale: no database, no label, no flag in a bucket.
The access token obtained is thrown away; no secret material is in the response.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pulsebridge_gcp.transport import Transport

from health_sync.config import ServiceConfig
from health_sync.credentials import SecretsMissing, load_credentials
from health_sync.google_health import (
    GoogleHealthError,
    GoogleHealthUnavailable,
    HttpSender,
    ReconnectNeeded,
    TokenSource,
)
from health_sync.retry import Deadline, DeadlineReached, RetryPolicy

CONNECTED = "connected"
RECONNECT_NEEDED = "reconnect-needed"
NOT_CONFIGURED = "not-configured"
UNKNOWN = "unknown"

# The status check is a quick question from the proxy (T14), not a run: one retry at most and a
# short budget, so the dashboard's status strip never waits long.
STATUS_MAX_ATTEMPTS = 2
STATUS_DEADLINE_SECONDS = 20.0


@dataclass(frozen=True)
class ConnectionStatus:
    state: str
    reason: str | None
    checked_at: datetime
    notes: tuple[str, ...] = ()
    stranded_preliminary: int | None = None

    @property
    def http_status(self) -> int:
        """Always 200: every state is an answer. Anything else from this route means the
        service itself failed (README, "GET /status")."""
        return 200

    def as_dict(self) -> dict:
        return {
            "googleHealth": self.state,
            "reason": self.reason,
            "notes": list(self.notes),
            "strandedPreliminary": self.stranded_preliminary,
            "checkedAt": self.checked_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def check_connection(
    config: ServiceConfig,
    transport: Transport,
    *,
    stranded: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ConnectionStatus:
    checked_at = now()
    notes: list[str] = ["backfill-needed"] if stranded else []

    def result(state: str, reason: str | None) -> ConnectionStatus:
        return ConnectionStatus(state, reason, checked_at, tuple(notes), stranded)

    try:
        credentials = load_credentials(config.secrets)
    except SecretsMissing as exc:
        return result(NOT_CONFIGURED, exc.reason)
    deadline = Deadline(min(STATUS_DEADLINE_SECONDS, config.run_deadline), clock=monotonic)
    policy = RetryPolicy(max_attempts=min(STATUS_MAX_ATTEMPTS, config.max_attempts), sleep=sleep)
    http = HttpSender(transport, policy, deadline, config.http_timeout)
    tokens = TokenSource(credentials, scope=config.scope, http=http, clock=monotonic)
    try:
        tokens.refresh()
    except ReconnectNeeded as exc:
        return result(RECONNECT_NEEDED, exc.code)
    except GoogleHealthUnavailable as exc:
        return result(UNKNOWN, f"token-endpoint-unavailable:{exc.status}")
    except GoogleHealthError as exc:
        return result(UNKNOWN, f"token-endpoint-{exc.code}:{exc.status}")
    except DeadlineReached:
        return result(UNKNOWN, "status-deadline")
    if tokens.rotated_refresh_token_ignored:
        notes.insert(0, "refresh-token-rotated")
    return result(CONNECTED, None)
