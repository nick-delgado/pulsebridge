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
The trade-off: while the total is exceeded, a 429 after a guess tells the guesser it was wrong.
That is no worse than the 401 it replaces, each address is still held to its own 5 a minute,
and the password itself (24+ random characters) is the real defence.

IPv6 clients are counted per /64 (``client_address``). Counters live in memory, per process:
with one gunicorn worker per instance and T22's instance cap, the real limit is at most the
configured one times the instance count (README). The number of tracked addresses is capped,
oldest first, so a flood of fresh addresses cannot exhaust memory.
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
    """A reservation made by :meth:`LoginLimiter.begin`."""

    address: str
    serial: int


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
            serial = next(self._serials)
            times.append((now, serial))
            self._by_address[address] = times  # most recently used last
            while len(self._by_address) > MAX_TRACKED_ADDRESSES:
                self._by_address.popitem(last=False)
            return Slot(address, serial), None

    def succeeded(self, slot: Slot) -> None:
        """A correct password: the reservation is given back (earlier failures stay)."""
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
        del slot  # the address's failure is the reservation itself
        with self._lock:
            now = self._monotonic()
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
