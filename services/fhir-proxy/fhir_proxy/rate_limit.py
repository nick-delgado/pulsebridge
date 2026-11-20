"""The rate limits of the public demo (D16, T46): two small in-memory limiters, keyed and capped.

Why limits at all: the demo is public, so anyone with a session (or without one) can send
requests as fast as they like. Each request costs Cloud Healthcare API calls, each cuff send
writes to the HL7v2 store, and each share writes to the clinic's OpenEMR. The limits keep one
visitor, or one address, from using what is meant for everyone.

Two ways of counting, chosen by how many requests a limit allows:

- **A sliding window** (:class:`SlidingWindows`) keeps the time of each request a key made and
  forgets it ``window`` seconds later. "3 per 10 minutes" then means exactly that: in **any**
  10 minutes, never more than 3. It stores one number per counted request, so it is used where
  the counts are small: cuff sends (3), shares (2 per session, 30 an hour for everyone), and the
  session attempts of ``fhir_proxy.session_limit`` (5 an hour).
- **A token bucket** (:class:`TokenBuckets`) is a counter that refills: a key starts with
  ``capacity`` tokens, each request takes one, and tokens come back at ``capacity / window`` a
  second, up to ``capacity``. It stores two numbers per key whatever the limit, so it is used
  where counts are large: reads (120 a minute per session) and all requests (300 a minute per
  address); a sliding window there would keep up to 300 timestamps per address. The price:
  a full bucket allows a burst of ``capacity`` at once, then ``capacity`` per window steadily,
  so the most in any one window is up to twice ``capacity`` (a burst at its end and a refill).
  That is the same algorithm as cuff-sim's bucket (``services/cuff-sim/cuff_sim/ratelimit.py``).

**Retry-After.** A refusal returns the whole seconds until the next request would be allowed,
rounded up and at least 1: the ``delay-seconds`` form of RFC 9110 section 10.2.3.

**The table cap** (``max_keys``). Every key costs memory, so each table is capped. A key whose
history is spent (a sliding window with no request left in it, a bucket that has refilled to
full) holds no information and is dropped to make room; a key that still holds history is
**never** dropped, so a flood of fresh keys cannot evict an abuser's history and hand it fresh
budget (the rule of ``fhir_proxy.session_limit``). When the table is full of live keys, a new
key is refused with a :class:`TableFull` wait of :data:`OVERFLOW_RETRY_SECONDS`, which the app
answers with its own text ("the demo is busy"), not the limit's: the caller has not used its
budget. The 60 s is a ceiling on what the client is told, not a promise: room usually appears
sooner, and a client still refused then is told again. Looking for spent keys walks the whole
table, so it runs at most once a second (:data:`SWEEP_INTERVAL_SECONDS`): a flood of fresh keys
against a full table costs one walk a second, not one per request.

**Per process.** The counts live in memory: no shared store, so nothing is billed while the
demo is idle, and a restart forgets them. With one gunicorn worker per instance, the real limit
is the configured one times the number of instances (fhir-proxy's ``max_instance_count`` is 2).

Each operation checks and records under one lock, so concurrent requests of one key cannot get
past a limit together (gunicorn runs eight threads per instance).
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

MAX_TRACKED_KEYS = 10_000
OVERFLOW_RETRY_SECONDS = 60
SWEEP_INTERVAL_SECONDS = 1.0


class TableFull(int):
    """A refusal because the table is full of live keys, not because this key used its budget.
    An ``int`` (the seconds to wait), so it is used like any other wait; ``isinstance`` tells
    the two apart."""


def whole_seconds(seconds: float) -> int:
    """``Retry-After`` delay-seconds: rounded up, at least 1 (RFC 9110 section 10.2.3)."""
    return max(1, math.ceil(seconds))


class _CappedTable:
    """The shared part: an ordered table of keys, the cap and the throttled sweep."""

    def __init__(self, *, max_keys: int, monotonic: Callable[[], float]) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        self._max = max_keys
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, object] = OrderedDict()
        self._last_sweep: float | None = None

    def _spent(self, entry: object, now: float) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _room_for_new_key(self, now: float) -> bool:
        if len(self._entries) < self._max:
            return True
        if self._last_sweep is not None and now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return False
        self._last_sweep = now
        for key in [k for k, entry in self._entries.items() if self._spent(entry, now)]:
            del self._entries[key]
        return len(self._entries) < self._max

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class SlidingWindows(_CappedTable):
    """At most ``limit`` requests per key in any ``window_seconds``."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        max_keys: int = MAX_TRACKED_KEYS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be positive")
        super().__init__(max_keys=max_keys, monotonic=monotonic)
        self._limit = limit
        self._window = window_seconds

    def _expire(self, times: deque[float], now: float) -> None:
        while times and times[0] <= now - self._window:
            times.popleft()

    def _spent(self, entry: object, now: float) -> bool:
        assert isinstance(entry, deque)  # noqa: S101 - the table only holds deques here
        self._expire(entry, now)
        return not entry

    def take(self, key: str) -> int | None:
        """Count one request of ``key``: ``None`` if allowed, else whole seconds to wait (and
        nothing is counted)."""
        with self._lock:
            now = self._monotonic()
            times = self._entries.get(key)
            if times is None:
                if not self._room_for_new_key(now):
                    return TableFull(OVERFLOW_RETRY_SECONDS)
                times = deque()
                self._entries[key] = times
            assert isinstance(times, deque)  # noqa: S101
            self._expire(times, now)
            if len(times) >= self._limit:
                # The request that ages out first is the oldest of the last `limit`.
                oldest = times[len(times) - self._limit]
                return whole_seconds(oldest + self._window - now)
            times.append(now)
            self._entries.move_to_end(key)
            return None

    def give_back(self, key: str) -> None:
        """Undo ``key``'s latest counted request (when a later limit refused it after all)."""
        with self._lock:
            times = self._entries.get(key)
            if isinstance(times, deque) and times:
                times.pop()


class TokenBuckets(_CappedTable):
    """Per key, a bucket of ``capacity`` tokens refilled at ``capacity / window_seconds`` a
    second: a burst of ``capacity``, then ``capacity`` per window."""

    def __init__(
        self,
        *,
        capacity: int,
        window_seconds: float,
        max_keys: int = MAX_TRACKED_KEYS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or window_seconds <= 0:
            raise ValueError("capacity and window_seconds must be positive")
        super().__init__(max_keys=max_keys, monotonic=monotonic)
        self._capacity = float(capacity)
        self._rate = capacity / window_seconds  # tokens per second

    def _refilled(self, entry: list[float], now: float) -> float:
        tokens, updated = entry
        # The clock is monotonic, so the elapsed time is never negative.
        return min(self._capacity, tokens + (now - updated) * self._rate)

    def _spent(self, entry: object, now: float) -> bool:
        assert isinstance(entry, list)  # noqa: S101 - the table only holds [tokens, time] here
        # A full bucket is the same as no bucket: dropping it forgets nothing.
        return self._refilled(entry, now) >= self._capacity

    def take(self, key: str) -> int | None:
        """Take one token of ``key``: ``None`` if allowed, else whole seconds until a token is
        back (and nothing is taken)."""
        with self._lock:
            now = self._monotonic()
            entry = self._entries.get(key)
            if entry is None:
                if not self._room_for_new_key(now):
                    return TableFull(OVERFLOW_RETRY_SECONDS)
                entry = [self._capacity, now]
                self._entries[key] = entry
            assert isinstance(entry, list)  # noqa: S101
            tokens = self._refilled(entry, now)
            if tokens < 1.0:
                entry[0], entry[1] = tokens, now
                return whole_seconds((1.0 - tokens) / self._rate)
            entry[0], entry[1] = tokens - 1.0, now
            self._entries.move_to_end(key)
            return None
