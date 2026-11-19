"""The per-address limit on ``POST /auth/session`` (D16): ``SESSIONS_PER_ADDRESS_PER_HOUR``.

It runs before anything costs money: before the body is parsed, before the daily counter is
touched and before an assessment is made. So a flood of junk from one address costs nothing
beyond the proxy's own CPU once that address has used its attempts.

- **Every attempt counts**, a pass included: a visitor needs one session a day (the cookie lasts
  24 hours), so five attempts an hour leave room for a few failed checks and a second browser.
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

Counters live in memory, per process: with one gunicorn worker per instance, the real limit is
at most the configured one times the instance count. That is acceptable here because the
**daily cap** (``fhir_proxy.demo_counter``), which does span instances, bounds the cost.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

WINDOW_SECONDS = 3600
MAX_TRACKED_ADDRESSES = 10_000
OVERFLOW_RETRY_SECONDS = 60


class SessionLimiter:
    def __init__(
        self,
        *,
        per_address: int,
        window_seconds: float = WINDOW_SECONDS,
        max_addresses: int = MAX_TRACKED_ADDRESSES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_address = per_address
        self._window = window_seconds
        self._max = max_addresses
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._by_address: OrderedDict[str, deque[float]] = OrderedDict()

    def _expire(self, times: deque[float], now: float) -> None:
        while times and times[0] <= now - self._window:
            times.popleft()

    def attempt(self, address: str) -> int | None:
        """Record one attempt from ``address``: ``None`` if allowed, else whole seconds to wait
        (and nothing is recorded)."""
        with self._lock:
            now = self._monotonic()
            times = self._by_address.get(address)
            if times is not None:
                self._expire(times, now)
                if len(times) >= self._per_address:
                    oldest = times[len(times) - self._per_address]
                    return max(1, math.ceil(oldest + self._window - now))
            elif len(self._by_address) >= self._max:
                # Make room only from entries whose attempts have all aged out.
                for key, entries in list(self._by_address.items()):
                    self._expire(entries, now)
                    if not entries:
                        del self._by_address[key]
                if len(self._by_address) >= self._max:
                    return OVERFLOW_RETRY_SECONDS
            if times is None:
                times = deque()
                self._by_address[address] = times
            times.append(now)
            self._by_address.move_to_end(address)
            return None
