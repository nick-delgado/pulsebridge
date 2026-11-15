"""Is the Google Health connection working right now? Answered statelessly, by trying it.

The status route (``GET /status``) reads the three secret files and asks Google's token endpoint
for an access token, exactly as a sync run would, and reports one of:

- ``connected`` (HTTP 200): the refresh succeeded and the token carries the configured scope.
- ``reconnect-needed`` (200): ``invalid_grant`` (the refresh token expired or was revoked), the
  OAuth client was refused, or the scope is missing.
- ``not-configured`` (200): a secret file is missing, empty or unreadable (no connect yet).
- ``unknown`` (503): the check itself could not finish: the token endpoint answered 429 or 5xx,
  could not be reached, or answered something unexpected.

There is nothing to store and nothing to go stale: no database, no label, no flag in a bucket
(the identity README's open question "where health-sync records reconnect needed" is answered
by "nowhere"). The access token obtained is thrown away; no secret material is in the response.
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
from health_sync.retry import Deadline, RetryPolicy

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

    @property
    def http_status(self) -> int:
        return 503 if self.state == UNKNOWN else 200

    @property
    def cacheable(self) -> bool:
        return self.state != UNKNOWN

    def as_dict(self) -> dict:
        return {
            "googleHealth": self.state,
            "reason": self.reason,
            "checkedAt": self.checked_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def check_connection(
    config: ServiceConfig,
    transport: Transport,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ConnectionStatus:
    checked_at = now()
    try:
        credentials = load_credentials(config.secrets)
    except SecretsMissing as exc:
        return ConnectionStatus(
            NOT_CONFIGURED, "secrets-missing:" + ",".join(exc.names), checked_at
        )
    deadline = Deadline(min(STATUS_DEADLINE_SECONDS, config.run_deadline), clock=monotonic)
    policy = RetryPolicy(max_attempts=min(STATUS_MAX_ATTEMPTS, config.max_attempts), sleep=sleep)
    http = HttpSender(transport, policy, deadline, config.http_timeout)
    tokens = TokenSource(credentials, scope=config.scope, http=http, clock=monotonic)
    try:
        tokens.refresh()
    except ReconnectNeeded as exc:
        return ConnectionStatus(RECONNECT_NEEDED, exc.code, checked_at)
    except GoogleHealthUnavailable as exc:
        return ConnectionStatus(UNKNOWN, f"token-endpoint-unavailable:{exc.status}", checked_at)
    except GoogleHealthError as exc:
        return ConnectionStatus(UNKNOWN, f"token-endpoint-{exc.code}:{exc.status}", checked_at)
    return ConnectionStatus(CONNECTED, None, checked_at)
