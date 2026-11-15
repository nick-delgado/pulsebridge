"""One overall time budget per request, applied to every upstream call.

The proxy sits behind Firebase Hosting, which ends a rewritten request after 60 seconds
("Firebase Hosting is subject to a 60-second request timeout", firebase.google.com/docs/hosting/
cloud-run), and Cloud Run ends it at the service's request timeout. A route that makes several
upstream calls must therefore stop **starting** them once its budget is spent and answer with
what is complete, instead of running past both limits.

- Each request gets a :class:`Deadline` (``REQUEST_BUDGET_SECONDS``, default 50 s), kept in a
  context variable so every layer sees the same one.
- :class:`DeadlineTransport` wraps the Cloud Healthcare transport: every call gets
  ``min(its own timeout, the time left)``, and a call that would start after the deadline is not
  sent at all (:class:`BudgetExhausted`, a ``TimedOutError``, so callers treat it like a timeout).
- :func:`run_parallel` runs independent calls on the shared executor with the request's context
  copied (``ThreadPoolExecutor`` does not carry context variables by itself), and waits for them
  only until the deadline: a call still running then is reported as not finished, and its own
  timeout (capped as above) ends it shortly after.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, wait
from dataclasses import dataclass
from typing import Any

from pulsebridge_gcp import TimedOutError

# Below this many seconds left, no new upstream call is started: it could not finish usefully.
MIN_CALL_SECONDS = 0.05


class BudgetExhausted(TimedOutError):
    """The request's time budget was spent before this call could start."""

    def __init__(self, operation: str = "upstream call") -> None:
        super().__init__(operation, None, server_message="request budget spent")


@dataclass
class Deadline:
    at: float
    monotonic: Callable[[], float] = time.monotonic

    def remaining(self) -> float:
        return self.at - self.monotonic()

    def expired(self) -> bool:
        return self.remaining() < MIN_CALL_SECONDS


_current: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar(
    "fhir_proxy_deadline", default=None
)


def start(seconds: float, monotonic: Callable[[], float] = time.monotonic) -> None:
    _current.set(Deadline(monotonic() + seconds, monotonic))


def current() -> Deadline | None:
    return _current.get()


def remaining(default: float) -> float:
    """Seconds left in this request's budget, or ``default`` outside a request."""
    deadline = _current.get()
    return default if deadline is None else deadline.remaining()


def capped(timeout: float) -> float:
    """``timeout`` limited to what is left; raises :class:`BudgetExhausted` if nothing is."""
    deadline = _current.get()
    if deadline is None:
        return timeout
    left = deadline.remaining()
    if left < MIN_CALL_SECONDS:
        raise BudgetExhausted()
    return min(timeout, left)


class DeadlineTransport:
    """A ``pulsebridge_gcp`` transport whose every request honours the request's deadline."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | Sequence[tuple[str, str]] | None,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Any:
        return self._inner.request(  # type: ignore[attr-defined]
            method, url, params=params, body=body, headers=headers, timeout=capped(timeout)
        )


NOT_FINISHED = object()


def run_parallel(executor: Executor, calls: Sequence[Callable[[], Any]]) -> list[Any]:
    """Run ``calls`` at once; each result is its return value, the exception it raised, or
    :data:`NOT_FINISHED` when the request's deadline came first."""
    if not calls:
        return []
    futures = [executor.submit(contextvars.copy_context().run, call) for call in calls]
    deadline = _current.get()
    done, _ = wait(futures, timeout=None if deadline is None else max(deadline.remaining(), 0))
    results: list[Any] = []
    for future in futures:
        if future not in done:
            results.append(NOT_FINISHED)
        elif future.exception() is not None:
            results.append(future.exception())
        else:
            results.append(future.result())
    return results
