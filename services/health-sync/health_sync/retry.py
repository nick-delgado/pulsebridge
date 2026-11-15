"""Bounded retry with exponential backoff, inside a deadline.

Used for answers that may pass if sent again shortly: HTTP 429 (Google: "Quota exceeded or rate
limited", RESOURCE_EXHAUSTED; its rate-limit guide says to back off and retry), 5xx, 408 and
network failures. Everything else fails at once.

The run stays bounded twice over: at most ``max_attempts`` tries per request, and never a sleep
that would end after the run's deadline. When either limit is reached the run fails and the next
scheduled run (15 minutes later, with an overlapping range) picks the data up. So a Google or
FHIR store outage costs at most one run's worth of retries, never a loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 16.0


@dataclass
class Deadline:
    """Wall-clock budget for one run. ``seconds=None`` means no deadline (the backfill CLI)."""

    seconds: float | None
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def remaining(self) -> float | None:
        if self.seconds is None:
            return None
        return self.seconds - (self.clock() - self.started)

    def allows(self, delay: float) -> bool:
        remaining = self.remaining()
        return remaining is None or delay < remaining


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    sleep: Callable[[float], None] = time.sleep

    def delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based): 1, 2, 4, 8, 16 s,
        or the server's ``Retry-After`` when it asks for longer (capped)."""
        backoff = min(BASE_DELAY_SECONDS * 2 ** (attempt - 1), MAX_DELAY_SECONDS)
        if retry_after is not None:
            backoff = max(backoff, min(retry_after, MAX_DELAY_SECONDS))
        return backoff


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` in delta-seconds form (RFC 9110 10.2.3). The HTTP-date form is ignored:
    the backoff applies instead."""
    if value is None or not value.strip().isdigit():
        return None
    return float(value.strip())
