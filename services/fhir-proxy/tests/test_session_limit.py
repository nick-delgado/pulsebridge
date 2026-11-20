"""The per-address limit on POST /auth/session: a sliding hour, a capped table."""

from __future__ import annotations

import threading

from fhir_proxy.session_limit import OVERFLOW_RETRY_SECONDS, SessionLimiter


class Mono:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_limit_and_retry_after_rounds_up() -> None:
    mono = Mono()
    limiter = SessionLimiter(per_address=2, monotonic=mono)
    assert limiter.attempt("a") is None
    mono.now += 0.5
    assert limiter.attempt("a") is None
    mono.now += 10
    assert limiter.attempt("a") == 3590  # 3600 - 10.5, rounded up
    mono.now = 1000.0 + 3599.9
    assert limiter.attempt("a") == 1
    mono.now = 1000.0 + 3600.0
    assert limiter.attempt("a") is None  # the first attempt has aged out


def test_a_refused_attempt_is_not_recorded() -> None:
    mono = Mono()
    limiter = SessionLimiter(per_address=1, monotonic=mono)
    limiter.attempt("a")
    for _ in range(50):
        limiter.attempt("a")
        mono.now += 60
    # 3000 s later: only the one allowed attempt counts, so it ages out at 3600 s.
    mono.now = 1000.0 + 3600
    assert limiter.attempt("a") is None


def test_a_full_table_keeps_live_entries() -> None:
    mono = Mono()
    limiter = SessionLimiter(per_address=1, max_addresses=3, monotonic=mono)
    for key in ("a", "b", "c"):
        limiter.attempt(key)
    # A fresh address cannot evict live history: it waits instead.
    assert limiter.attempt("d") == OVERFLOW_RETRY_SECONDS
    assert limiter.attempt("a") == 3600  # "a" was not evicted and is still limited
    mono.now += 3600
    assert limiter.attempt("d") is None  # expired entries make room


def test_concurrent_attempts_cannot_pass_the_limit_together() -> None:
    limiter = SessionLimiter(per_address=5)
    barrier = threading.Barrier(40)
    results = []

    def go() -> None:
        barrier.wait()
        results.append(limiter.attempt("a"))

    threads = [threading.Thread(target=go) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(None) == 5
