"""Keeps a flood of refused requests from writing one log line each.

Within one window, the first event from each key (a client address) is logged, up to
``max_keys`` of them, and the rest are only counted; when the window has passed, the next event
first reports how many were not logged. So a flood costs about one line per address per window,
plus one summary, whatever its rate. (Moved here from the D9 login limiter, which is gone; T46's
rate limits use it too.)
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class FloodLogSampler:
    def __init__(
        self,
        *,
        window_seconds: float,
        max_keys: int = 100,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._max = max_keys
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._started: float | None = None
        self._logged: set[str] = set()
        self._suppressed = 0

    @property
    def window_seconds(self) -> float:
        return self._window

    def _roll(self, now: float) -> int | None:
        report = None
        if self._started is None or now - self._started >= self._window:
            report = self._suppressed or None
            self._started, self._logged, self._suppressed = now, set(), 0
        return report

    def record(self, key: str) -> tuple[bool, int | None]:
        """``(log this event, suppressed count to report first or None)``."""
        with self._lock:
            now = self._monotonic()
            report = self._roll(now)
            if key in self._logged or len(self._logged) >= self._max:
                self._suppressed += 1
                return False, report
            self._logged.add(key)
            return True, report
