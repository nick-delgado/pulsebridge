"""The daily counter in Cloud Storage: generation preconditions, the race, retries, the cap."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from fhir_proxy.demo_counter import (
    MAX_ATTEMPTS,
    CounterUnavailable,
    DailyCounter,
    DemoBusy,
)
from fhir_proxy.testing import FakeBucket

NOW = datetime(2026, 1, 2, 18, 30, tzinfo=UTC)
NAME = "bot-checks/2026-01-02"


def counter(bucket: FakeBucket, cap: int = 300, **kw: object) -> DailyCounter:
    return DailyCounter(bucket, bucket=bucket.bucket, cap=cap, timeout=5, clock=lambda: NOW, **kw)


def test_the_first_check_of_the_day_creates_the_object_only_if_absent() -> None:
    bucket = FakeBucket()
    taken = counter(bucket).take()
    assert (taken.day, taken.count) == ("2026-01-02", 1)
    assert bucket.count(NAME) == 1
    # The object name is %2F-encoded in the path (a "/" would be a path separator).
    assert bucket.requests[0] == (
        "GET",
        "https://storage.googleapis.com/storage/v1/b/example-demo-counters/o/bot-checks%2F2026-01-02",
    )


def test_each_take_adds_one_up_to_the_cap() -> None:
    bucket = FakeBucket()
    c = counter(bucket, cap=3)
    assert [c.take().count for _ in range(3)] == [1, 2, 3]
    with pytest.raises(DemoBusy) as busy:
        c.take()
    assert busy.value.retry_after == 5 * 3600 + 30 * 60  # 18:30 to midnight UTC
    assert bucket.count(NAME) == 3  # a refusal writes nothing


def test_the_day_is_the_utc_day() -> None:
    bucket = FakeBucket()
    from datetime import timedelta, timezone

    # 23:30 on 1 January in UTC-5 is 04:30 on 2 January UTC.
    local = datetime(2026, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    DailyCounter(bucket, bucket=bucket.bucket, cap=5, timeout=5, clock=lambda: local).take()
    assert list(bucket.objects) == [NAME]


def test_a_write_by_another_instance_in_between_is_not_lost() -> None:
    bucket = FakeBucket()
    bucket.put(NAME, "41")
    calls = []

    def race(name: str) -> None:
        calls.append(name)
        if len(calls) == 1:
            bucket.put(name, "42")  # the other instance turned 41 into 42 first

    bucket.before_write = race
    assert counter(bucket).take().count == 43
    assert bucket.count(NAME) == 43
    assert len(calls) == 2  # one refused write (412), one that held


def test_two_first_writes_of_the_day_cannot_both_create_the_object() -> None:
    bucket = FakeBucket()
    fired = []

    def race(name: str) -> None:
        if not fired:
            fired.append(1)
            bucket.put(name, "1")  # created by the other instance after our read saw nothing

    bucket.before_write = race
    assert counter(bucket).take().count == 2


def test_concurrent_takes_on_two_instances_lose_no_count() -> None:
    bucket = FakeBucket()
    instances = [counter(bucket, cap=10_000), counter(bucket, cap=10_000)]
    barrier = threading.Barrier(16)
    errors = []

    def work(n: int) -> None:
        barrier.wait()
        for _ in range(5):
            try:
                instances[n % 2].take()
            except CounterUnavailable as exc:  # contention past the retries: allowed, counted
                errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bucket.count(NAME) == 80 - len(errors)


def test_the_cap_holds_under_concurrency() -> None:
    bucket = FakeBucket()
    instances = [counter(bucket, cap=20), counter(bucket, cap=20)]
    allowed, busy = [], []
    barrier = threading.Barrier(10)

    def work(n: int) -> None:
        barrier.wait()
        for _ in range(5):
            try:
                allowed.append(instances[n % 2].take().count)
            except DemoBusy:
                busy.append(1)
            except CounterUnavailable:
                pass

    threads = [threading.Thread(target=work, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(allowed) == list(range(1, len(allowed) + 1))  # no count handed out twice
    assert len(allowed) <= 20 and bucket.count(NAME) == len(allowed)


def test_endless_contention_gives_up_as_unavailable() -> None:
    bucket = FakeBucket()

    def always(name: str) -> None:
        bucket.put(name, str((bucket.count(name) or 0) + 1))

    bucket.before_write = always
    with pytest.raises(CounterUnavailable, match="contention"):
        counter(bucket).take()
    assert len([r for r in bucket.requests if r[0] == "POST"]) == MAX_ATTEMPTS


def test_the_write_rate_limit_waits_and_tries_again() -> None:
    bucket = FakeBucket()
    sleeps = []
    answers = iter([429, None])

    real = bucket.request

    def flaky(method, url, **kw):  # noqa: ANN001, ANN202
        if method == "POST":
            status = next(answers)
            if status is not None:
                from pulsebridge_gcp import HttpResponse

                return HttpResponse(status, b"{}", {})
        return real(method, url, **kw)

    bucket.request = flaky  # type: ignore[method-assign]
    assert counter(bucket, sleep=sleeps.append).take().count == 1
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    "resource",
    [
        {"generation": "5", "metadata": {"count": "x"}},
        {"generation": "5", "metadata": {"count": "-1"}},
        {"generation": "5", "metadata": {}},
        {"generation": "5"},
        {"generation": 5, "metadata": {"count": "1"}},
        {"metadata": {"count": "1"}},
    ],
)
def test_an_object_that_is_not_a_counter_fails_closed(resource: dict) -> None:
    bucket = FakeBucket()
    bucket.objects[NAME] = resource
    with pytest.raises(CounterUnavailable, match="bad-object"):
        counter(bucket).take()


@pytest.mark.parametrize(
    ("reads", "writes", "code"),
    [
        (403, None, "http-403"),
        (500, None, "http-500"),
        ("timeout", None, "timeout"),
        ("unreachable", None, "unreachable"),
        (None, 403, "http-403"),
        (None, "timeout", "timeout"),
    ],
)
def test_failures_are_fixed_codes(reads, writes, code: str) -> None:
    bucket = FakeBucket()
    bucket.fail_reads, bucket.fail_writes = reads, writes
    with pytest.raises(CounterUnavailable) as raised:
        counter(bucket).take()
    assert str(raised.value) == code
