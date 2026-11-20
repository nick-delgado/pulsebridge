"""The two limiters of T46 (``fhir_proxy.rate_limit``), on an injected clock: the counts at their
boundaries, Retry-After, the capped tables (no eviction of live history) and concurrency."""

from __future__ import annotations

import threading

import pytest
from fhir_proxy.rate_limit import (
    OVERFLOW_RETRY_SECONDS,
    SlidingWindows,
    TokenBuckets,
    whole_seconds,
)


class Mono:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("seconds", "expected"), [(0.0, 1), (0.001, 1), (1.0, 1), (1.001, 2), (59.5, 60)]
)
def test_retry_after_is_whole_seconds_rounded_up_and_at_least_one(seconds, expected) -> None:
    assert whole_seconds(seconds) == expected


# ---- the sliding window -------------------------------------------------------------------------


def test_a_sliding_window_allows_exactly_the_limit_in_any_window() -> None:
    mono = Mono()
    windows = SlidingWindows(limit=3, window_seconds=600, monotonic=mono)
    for step in (0, 100, 100):  # requests at 0, 100 and 200 s
        mono.now += step
        assert windows.take("s") is None
    mono.now += 50  # 250 s
    assert windows.take("s") == 350  # the first ages out at 600 s
    mono.now = 1000.0 + 599.5
    assert windows.take("s") == 1  # half a second left, rounded up
    mono.now = 1000.0 + 600.0
    assert windows.take("s") is None  # exactly 600 s later the first has aged out
    assert windows.take("s") == 100  # the second (at 100 s) is next, at 700 s


def test_a_refused_request_is_not_counted() -> None:
    mono = Mono()
    windows = SlidingWindows(limit=1, window_seconds=600, monotonic=mono)
    windows.take("s")
    for _ in range(20):
        mono.now += 25
        windows.take("s")
    mono.now = 1000.0 + 600
    assert windows.take("s") is None


def test_keys_are_counted_apart() -> None:
    windows = SlidingWindows(limit=1, window_seconds=600, monotonic=Mono())
    assert windows.take("a") is None
    assert windows.take("a") is not None
    assert windows.take("b") is None


def test_give_back_undoes_the_latest_request_only() -> None:
    mono = Mono()
    windows = SlidingWindows(limit=2, window_seconds=600, monotonic=mono)
    windows.take("s")
    mono.now += 10
    windows.take("s")
    windows.give_back("s")
    assert windows.take("s") is None  # the slot given back is free again
    assert windows.take("s") == 590  # the first (at 0 s) is still counted
    windows.give_back("nobody")  # unknown key: nothing happens


def test_a_full_sliding_table_keeps_live_history() -> None:
    mono = Mono()
    windows = SlidingWindows(limit=1, window_seconds=600, max_keys=3, monotonic=mono)
    for key in ("a", "b", "c"):
        windows.take(key)
    assert windows.take("fresh") == OVERFLOW_RETRY_SECONDS
    assert windows.take("a") == 600  # not evicted: still limited
    assert len(windows) == 3
    mono.now += 600
    assert windows.take("fresh") is None  # spent entries make room


def test_only_spent_entries_are_dropped_when_the_table_is_full() -> None:
    mono = Mono()
    windows = SlidingWindows(limit=1, window_seconds=600, max_keys=2, monotonic=mono)
    windows.take("old")
    mono.now += 300
    windows.take("live")
    mono.now += 300  # "old" has aged out, "live" has not
    assert windows.take("fresh") is None
    assert windows.take("live") == 300  # kept, with its history
    assert windows.take("old") == OVERFLOW_RETRY_SECONDS  # dropped, and the table is full again


def test_the_search_for_room_runs_at_most_once_a_second() -> None:
    mono = Mono()
    windows = SlidingWindows(limit=1, window_seconds=10, max_keys=1, monotonic=mono)
    windows.take("a")
    assert windows.take("b") == OVERFLOW_RETRY_SECONDS  # swept now: "a" is live
    mono.now += 10  # "a" is spent, but the last sweep was 10 s ago: a new sweep may run
    assert windows.take("b") is None
    mono.now += 10
    windows.take("b")  # "b" is live again (a new request at 20 s)
    mono.now += 9.5
    assert windows.take("c") == OVERFLOW_RETRY_SECONDS  # sweep at 29.5 s: "b" is live
    mono.now += 0.6  # "b" is spent now, but the last sweep was 0.6 s ago
    assert windows.take("c") == OVERFLOW_RETRY_SECONDS
    mono.now += 0.5
    assert windows.take("c") is None


# ---- the token bucket ---------------------------------------------------------------------------


def test_a_bucket_allows_its_capacity_at_once_then_refills_steadily() -> None:
    mono = Mono()
    buckets = TokenBuckets(capacity=120, window_seconds=60, monotonic=mono)
    assert all(buckets.take("s") is None for _ in range(120))
    assert buckets.take("s") == 1  # 2 tokens a second: half a second, rounded up
    mono.now += 0.5
    assert buckets.take("s") is None  # one token back
    assert buckets.take("s") == 1
    mono.now += 30  # 60 tokens back
    assert sum(buckets.take("s") is None for _ in range(100)) == 60


def test_a_bucket_never_holds_more_than_its_capacity() -> None:
    mono = Mono()
    buckets = TokenBuckets(capacity=5, window_seconds=60, monotonic=mono)
    buckets.take("s")
    mono.now += 3600  # an hour idle refills to 5, not to 60
    assert sum(buckets.take("s") is None for _ in range(10)) == 5


def test_retry_after_of_a_slow_bucket_is_the_time_to_one_token() -> None:
    mono = Mono()
    buckets = TokenBuckets(capacity=3, window_seconds=600, monotonic=mono)  # one per 200 s
    for _ in range(3):
        buckets.take("s")
    assert buckets.take("s") == 200
    mono.now += 150
    assert buckets.take("s") == 50  # a refused request takes nothing
    mono.now += 50
    assert buckets.take("s") is None


def test_buckets_are_counted_apart() -> None:
    buckets = TokenBuckets(capacity=1, window_seconds=60, monotonic=Mono())
    assert buckets.take("a") is None
    assert buckets.take("a") is not None
    assert buckets.take("b") is None


def test_a_full_bucket_table_keeps_live_history() -> None:
    mono = Mono()
    buckets = TokenBuckets(capacity=2, window_seconds=60, max_keys=2, monotonic=mono)
    buckets.take("a")
    buckets.take("a")  # "a" is empty: an abuser
    buckets.take("b")
    assert buckets.take("fresh") == OVERFLOW_RETRY_SECONDS
    assert buckets.take("a") == 30  # not evicted: still empty, one token in 30 s
    mono.now += 30  # "b" (1 + 1 token) is full again and is dropped; "a" has 1 token: kept
    assert buckets.take("fresh") is None
    assert len(buckets) == 2
    assert buckets.take("a") is None
    assert buckets.take("a") == 30  # its history survived the flood


@pytest.mark.parametrize(
    "make",
    [
        lambda: SlidingWindows(limit=7, window_seconds=600),
        lambda: TokenBuckets(capacity=7, window_seconds=3600),
    ],
)
def test_concurrent_requests_cannot_pass_a_limit_together(make) -> None:
    limiter = make()
    barrier = threading.Barrier(40)
    results = []

    def go() -> None:
        barrier.wait()
        results.append(limiter.take("s"))

    threads = [threading.Thread(target=go) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(None) == 7


@pytest.mark.parametrize(
    "make",
    [
        lambda: SlidingWindows(limit=0, window_seconds=60),
        lambda: SlidingWindows(limit=1, window_seconds=0),
        lambda: TokenBuckets(capacity=0, window_seconds=60),
        lambda: TokenBuckets(capacity=1, window_seconds=60, max_keys=0),
    ],
)
def test_a_limit_that_allows_nothing_is_refused_at_start(make) -> None:
    with pytest.raises(ValueError):
        make()
