"""A small in-process token bucket: enough to keep a public demo from flooding the HL7v2 store.

The bucket holds up to ``burst`` tokens and refills at ``per_minute`` tokens a minute. Each send
(a built reading or a malformed sample) takes one; with none left the request is refused with
HTTP 429 and ``Retry-After`` (RFC 9110 section 10.2.3), the whole number of seconds until a token
is back.

**Per instance.** Each Cloud Run instance has its own bucket, so the service-wide limit is this
limit times the number of instances. T22 caps the instance count (``max_instance_count``), which
makes the total bounded. A shared limit would need a shared store (Redis, Firestore), which
costs money while idle; a demo does not need it.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        *,
        per_minute: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute < 1 or burst < 1:
            raise ValueError("per_minute and burst must be at least 1")
        self._rate = per_minute / 60.0  # tokens per second
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._updated = clock()
        self._lock = threading.Lock()  # gunicorn runs several threads per instance

    def take(self) -> int | None:
        """Take one token. ``None`` if allowed; otherwise the seconds to wait (at least 1)."""
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._updated)
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return None
            return max(1, math.ceil((1.0 - self._tokens) / self._rate))
