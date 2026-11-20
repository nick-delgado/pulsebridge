"""The per-address limit on ``POST /auth/session`` (D16): ``SESSIONS_PER_ADDRESS_PER_HOUR``.

It runs before anything costs money: before the body is parsed, before the daily counter is
touched and before an assessment is made. So a flood of junk from one address costs nothing
beyond the proxy's own CPU once that address has used its attempts.

- **Every attempt counts**, a pass included: a visitor needs one session a day (the cookie lasts
  24 hours), so five attempts an hour leave room for a few failed checks and a second browser.
  An attempt that ends in 503 (Fraud Defense or the counter unavailable) counts too: giving it
  back would let a bot hammer freely for as long as an outage lasts.
  Counting only failures, as the old login limit did, would let a bot that passes the check
  mint sessions without limit.
- **A sliding window of one hour.** An attempt is forgotten one hour after it was made; when an
  address is at its limit, ``Retry-After`` is the whole seconds until its oldest attempt ages
  out (RFC 9110 section 10.2.3: delay-seconds, rounded up, at least 1).
- **The address** is ``fhir_proxy.client_address``'s key, the same rules as the D9 login limit:
  the ``TRUSTED_PROXY_HOPS``-th ``X-Forwarded-For`` entry from the right, an IPv6 /64 as one.
- **Checked and recorded under one lock**, so concurrent attempts from one address cannot get
  past the limit together.
- **The table is capped** (``MAX_TRACKED_ADDRESSES``). Only addresses whose attempts have all aged
  out are dropped to make room, so a flood of fresh addresses cannot evict an abuser's history
  and hand it fresh attempts. When the table is full of live entries, a new address is refused
  (``Retry-After`` :data:`OVERFLOW_RETRY_SECONDS`) until room is made: that is the cost of a
  flood of more than 10,000 addresses in an hour on one instance, and it costs no assessment.

The counting itself is ``fhir_proxy.rate_limit.SlidingWindows``, which T46's other limits use
too (T46 moved it there; the rules above are unchanged, and the search for aged-out entries now
runs at most once a second).

Counters live in memory, per process: with one gunicorn worker per instance, the real limit is
at most the configured one times the instance count. That is acceptable here because the
**daily cap** (``fhir_proxy.demo_counter``), which does span instances, bounds the cost.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fhir_proxy.rate_limit import OVERFLOW_RETRY_SECONDS, SlidingWindows

WINDOW_SECONDS = 3600
MAX_TRACKED_ADDRESSES = 10_000

__all__ = ["MAX_TRACKED_ADDRESSES", "OVERFLOW_RETRY_SECONDS", "WINDOW_SECONDS", "SessionLimiter"]


class SessionLimiter:
    def __init__(
        self,
        *,
        per_address: int,
        window_seconds: float = WINDOW_SECONDS,
        max_addresses: int = MAX_TRACKED_ADDRESSES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._windows = SlidingWindows(
            limit=per_address,
            window_seconds=window_seconds,
            max_keys=max_addresses,
            monotonic=monotonic,
        )

    def attempt(self, address: str) -> int | None:
        """Record one attempt from ``address``: ``None`` if allowed, else whole seconds to wait
        (and nothing is recorded)."""
        return self._windows.take(address)
