"""The login rate limit (docs/spikes/s6.md, "Rate limiting"): failed logins per window.

Two sliding windows (``LOGIN_WINDOW_SECONDS``, default 60 s):

**Per client address (default 5), checked before the password.** :meth:`LoginLimiter.begin`
reserves one of the address's slots before the password is looked at; a correct password gives
the slot back (:meth:`succeeded`), a wrong one keeps it as a failure (:meth:`failed`). Checking and
reserving happen under one lock, so concurrent attempts from one address cannot get past the
limit together: attempts in flight count. Once an address has used its slots, further attempts
are answered 429 with ``Retry-After`` without the password being checked, so a blocked attacker
learns nothing more from that address. A success does not clear earlier failures (a NAT-mate
attacker could otherwise reset the count whenever a reviewer logs in); they age out.

**In total on this instance (default 60), applied to failures only.** Checking it before the
password would let anyone lock every reviewer out: about one wrong login a second, spread over
twelve addresses, would keep the total full for good. So the total never stops a correct
password: once it is exceeded the password is still checked (a digest comparison and a cached
key, cheap), a correct one gets in, and a wrong one is answered 429 and is not counted again.

**What that means for guessing.** The total no longer bounds how many passwords can be tried.
Once it is exceeded, verifications are bounded only per address:

- one address (an IPv6 /64 counts as one): 5 guesses a minute;
- N addresses: 5 x N a minute;
- a wrong ``TRUSTED_PROXY_HOPS``, so the key is a value the caller writes: as many as the
  service can answer, bounded only by throughput (instances x threads).

What protects the login then is the password itself: a random 16-character password (T27's
enforced minimum; 24+ is recommended) has far more values than any of these rates could try,
and T22 must set the hop count right for the one path that reaches the service. The costs of a
flood are log volume (bounded by :class:`FloodLogSampler`: about one line per address per
window) and Cloud Run usage (instances, CPU, requests), which ``max_instance_count`` caps. No
hard ceiling on verifications is applied: it would bring the lockout back.

The number of tracked addresses is capped (``MAX_TRACKED_ADDRESSES``). Only addresses whose
window has expired are dropped to make room, so a flood of fresh addresses cannot evict an
attacker's history and hand it fresh guesses. When the table is full of live entries, a new
address is treated as already at its limit for the window ("overflow"): its password is still
checked, so a correct one gets in, and a wrong one is answered 429.

IPv6 clients are counted per /64 (``client_address``). Counters live in memory, per process:
with one gunicorn worker per instance and T22's instance cap, the real limit is at most the
configured one times the instance count (README).
"""

from __future__ import annotations

import itertools
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

MAX_TRACKED_ADDRESSES = 10_000


@dataclass(frozen=True)
class Slot:
    """A reservation made by :meth:`LoginLimiter.begin`. ``overflow``: the address table was
    full of live entries, so nothing was reserved; the attempt counts as at its limit."""

    address: str
    serial: int
    overflow: bool = False


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
        self._serials = itertools.count()
        # Entries are (time, serial) so one reservation can be given back exactly.
        self._by_address: OrderedDict[str, deque[tuple[float, int]]] = OrderedDict()
        self._all: deque[tuple[float, int]] = deque()

    def _expire(self, times: deque, now: float) -> None:
        while times and times[0][0] <= now - self._window:
            times.popleft()

    def _wait(self, times: deque, limit: int, now: float) -> int | None:
        """Whole seconds until this window has room again, or None if it has room now."""
        self._expire(times, now)
        if len(times) < limit:
            return None
        oldest = times[len(times) - limit][0]
        # RFC 9110 section 10.2.3: delay-seconds, a whole number; rounded up, at least 1.
        return max(1, math.ceil(oldest + self._window - now))

    def begin(self, address: str) -> tuple[Slot | None, int | None]:
        """Reserve one of ``address``'s slots: ``(slot, None)``, or ``(None, retry_after)``."""
        with self._lock:
            now = self._monotonic()
            times = self._by_address.pop(address, None) or deque()
            wait = self._wait(times, self._per_address, now)
            if wait is not None:
                if times:
                    self._by_address[address] = times
                return None, wait
            if not times and len(self._by_address) >= MAX_TRACKED_ADDRESSES:
                # Make room only from entries whose window has expired; never drop live
                # history (that would hand an evicted address fresh guesses).
                for key, entries in list(self._by_address.items()):
                    self._expire(entries, now)
                    if not entries:
                        del self._by_address[key]
                if len(self._by_address) >= MAX_TRACKED_ADDRESSES:
                    return Slot(address, -1, overflow=True), None
            serial = next(self._serials)
            times.append((now, serial))
            self._by_address[address] = times  # most recently used last
            return Slot(address, serial), None

    def succeeded(self, slot: Slot) -> None:
        """A correct password: the reservation is given back (earlier failures stay)."""
        if slot.overflow:
            return
        with self._lock:
            times = self._by_address.get(slot.address)
            if times is None:
                return
            for entry in list(times):
                if entry[1] == slot.serial:
                    times.remove(entry)
            if not times:
                del self._by_address[slot.address]

    def failed(self, slot: Slot) -> int | None:
        """A wrong password: the reservation stays as this address's failure. Returns the
        seconds to wait when the instance-wide total is already exceeded (the failure is then
        not counted in the total again), else None after counting it."""
        with self._lock:
            now = self._monotonic()
            if slot.overflow:
                # Treated as already at its limit for the window: refused, not counted.
                return max(1, math.ceil(self._window))
            wait = self._wait(self._all, self._total, now)
            if wait is not None:
                return wait
            self._all.append((now, -1))
            return None

    def retry_after(self, address: str) -> int | None:
        """Seconds this address must wait before its next attempt, or None (for tests/logs)."""
        with self._lock:
            times = self._by_address.get(address)
            return (
                None if times is None else self._wait(times, self._per_address, self._monotonic())
            )


class FloodLogSampler:
    """Keeps a flood of failed or refused logins from writing one log line per guess.

    Within one window, the first event from each address is logged (up to ``max_addresses``
    of them) and the rest are only counted; when the window has passed, the next event first
    reports how many were not logged (``login-events-suppressed``). So a flood costs about one
    line per address per window, plus one summary, whatever its rate.
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        max_addresses: int = 100,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._max = max_addresses
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._started: float | None = None
        self._logged: set[str] = set()
        self._suppressed = 0

    def _roll(self, now: float) -> int | None:
        report = None
        if self._started is None or now - self._started >= self._window:
            report = self._suppressed or None
            self._started, self._logged, self._suppressed = now, set(), 0
        return report

    def due(self) -> int | None:
        """The suppressed count of a window that has ended, once (for any later login event)."""
        with self._lock:
            return self._roll(self._monotonic())

    def record(self, address: str) -> tuple[bool, int | None]:
        """``(log this event, suppressed count to report first or None)``."""
        with self._lock:
            now = self._monotonic()
            report = self._roll(now)
            if address in self._logged or len(self._logged) >= self._max:
                self._suppressed += 1
                return False, report
            self._logged.add(address)
            return True, report
