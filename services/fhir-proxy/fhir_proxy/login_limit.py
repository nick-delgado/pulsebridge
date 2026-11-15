"""The login rate limit (docs/spikes/s6.md, "Rate limiting"): failed logins per window.

Two sliding windows, both counting **failed** logins only:

- per client address (default 5 a minute): one attacker cannot try more than that, and cannot
  lock every reviewer out, because other addresses have their own count;
- in total on this instance (default 60 a minute): bounds noise and cost when many addresses
  are used.

The check happens **before** the password is looked at. Once an address has used up its
failures, every further attempt from it within the window is answered 429 with ``Retry-After``,
whether or not the password would have been right; otherwise a blocked attacker would still
learn the answer to each guess. So the sixth attempt after five failures in a minute is a 429.

A successful login does **not** clear the address's failures. Clearing them would let an
attacker who shares an address with a reviewer (a NAT, an office) reset the count each time the
reviewer logs in. Failures simply age out of the window.

Counters live in memory, per process. The service runs one gunicorn worker per instance and
T22 caps the instance count, so the real limit is at most the configured one times the number
of instances (README). The number of tracked addresses is capped, oldest first, so a flood of
fresh addresses cannot exhaust memory; the total limit still applies to them.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

MAX_TRACKED_ADDRESSES = 10_000


class LoginLimiter:
    def __init__(
        self,
        *,
        per_address: int,
        total: int,
        window_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_address = per_address
        self._total = total
        self._window = window_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._by_address: OrderedDict[str, deque[float]] = OrderedDict()
        self._all: deque[float] = deque()

    def _expire(self, times: deque[float], now: float) -> None:
        while times and times[0] <= now - self._window:
            times.popleft()

    def _wait(self, times: deque[float], limit: int, now: float) -> int | None:
        """Whole seconds until this window has room again, or None if it has room now."""
        self._expire(times, now)
        if len(times) < limit:
            return None
        # The oldest failure that keeps the count at the limit must age out.
        oldest = times[len(times) - limit]
        # RFC 9110 section 10.2.3: delay-seconds, a whole number; rounded up, at least 1.
        return max(1, math.ceil(oldest + self._window - now))

    def retry_after(self, address: str) -> int | None:
        """Seconds to wait if this address (or the instance) is over its limit, else None."""
        with self._lock:
            now = self._monotonic()
            waits = [self._wait(self._all, self._total, now)]
            times = self._by_address.get(address)
            if times is not None:
                waits.append(self._wait(times, self._per_address, now))
                if not times:
                    del self._by_address[address]
            present = [w for w in waits if w is not None]
            return max(present) if present else None

    def record_failure(self, address: str) -> None:
        with self._lock:
            now = self._monotonic()
            self._all.append(now)
            self._expire(self._all, now)
            times = self._by_address.pop(address, None) or deque()
            times.append(now)
            self._expire(times, now)
            self._by_address[address] = times  # most recently failed last
            while len(self._by_address) > MAX_TRACKED_ADDRESSES:
                self._by_address.popitem(last=False)
