"""The synthetic person: determinism, invariants, gaps, shape."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pulsebridge_fhir import window_from_rollup
from pulsebridge_seed.generator import (
    HEART_RATE_BOUNDS,
    floor_to_window,
    generate,
    slots,
    window_at,
)

NY = ZoneInfo("America/New_York")
START = datetime(2026, 2, 1, 5, 0, tzinfo=UTC)  # local midnight in New York (winter)


def by_start(items):
    return {item["startTime"]: item for item in items}


def test_same_seed_same_windows_in_a_fresh_process():
    """No process-salted hash() and no global random state: another interpreter, with a
    different PYTHONHASHSEED, produces exactly the same windows."""
    here = generate(3, NY, START, START + timedelta(days=1)).items
    code = (
        "import json, sys\n"
        "from datetime import datetime, timedelta, UTC\n"
        "from zoneinfo import ZoneInfo\n"
        "from pulsebridge_seed.generator import generate\n"
        "s = datetime(2026, 2, 1, 5, tzinfo=UTC)\n"
        "print(json.dumps(generate(3, ZoneInfo('America/New_York'), s, s + timedelta(days=1))"
        ".items))\n"
    )

    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": "12345", "PATH": ""},
    )
    assert json.loads(out.stdout) == here


def test_overlapping_ranges_with_different_ends_agree_window_for_window():
    """A run "today" and a run three days later over overlapping ranges: the overlap is
    identical, so the second run rewrites the same values (no history rewritten)."""
    early = generate(5, NY, START, START + timedelta(days=4))
    late = generate(5, NY, START + timedelta(days=2, hours=7), START + timedelta(days=7))
    a, b = by_start(early.items), by_start(late.items)
    overlap = set(a) & set(b)
    assert len(overlap) > 400  # about 1.7 days of windows minus gaps
    assert all(a[k] == b[k] for k in overlap)
    # The gaps agree too: a window absent in one run is absent in the other.
    overlap_slots = {
        s.start.strftime("%Y-%m-%dT%H:%M:%SZ")
        for s in slots(5, NY, START + timedelta(days=2, hours=7), START + timedelta(days=4))
    }
    assert {k for k in overlap_slots if k in a} == {k for k in overlap_slots if k in b}


def test_window_value_does_not_depend_on_the_range_it_was_asked_in():
    start = START + timedelta(hours=13, minutes=35)
    alone = window_at(9, NY, start)
    in_range = by_start(generate(9, NY, START, START + timedelta(days=1)).items)
    assert alone is not None
    assert in_range[alone["startTime"]] == alone


def test_a_different_seed_is_a_different_person():
    one = by_start(generate(1, NY, START, START + timedelta(days=2)).items)
    two = by_start(generate(2, NY, START, START + timedelta(days=2)).items)
    common = set(one) & set(two)
    differing = [k for k in common if one[k]["heartRate"] != two[k]["heartRate"]]
    assert len(differing) > 0.9 * len(common)
    assert set(one) != set(two)  # and the gaps fall elsewhere


@pytest.mark.parametrize("seed", [0, 1, 2, 31337, 2**63 - 1])
@pytest.mark.parametrize("zone", ["America/New_York", "Asia/Tokyo", "UTC", "Australia/Lord_Howe"])
def test_invariants_over_many_days(seed, zone):
    """Property-style, seeded: 60 days for each seed and zone. Every window is valid for T06,
    in the physiological bounds, min <= avg <= max, whole-number min and max, aligned bounds."""
    tz = ZoneInfo(zone)
    low, high = HEART_RATE_BOUNDS
    result = generate(seed, tz, START, START + timedelta(days=60))
    assert result.items
    for item in result.items:
        hr = item["heartRate"]
        lo, avg, hi = hr["beatsPerMinuteMin"], hr["beatsPerMinuteAvg"], hr["beatsPerMinuteMax"]
        assert low <= lo <= avg <= hi <= high, item
        assert isinstance(lo, int) and isinstance(hi, int)
        assert hi - lo >= 2  # a five-minute window always has some spread
        window = window_from_rollup(item)  # T06's own validation
        assert window.end - window.start == timedelta(minutes=5)
        assert window.start == floor_to_window(window.start)


def test_the_rhythm_is_plausible():
    """Lower at night than in the day (local time), with exercise peaks on some days."""
    result = generate(11, NY, START, START + timedelta(days=30))
    night, day, peaks = [], [], 0
    for item in result.items:
        local = datetime.fromisoformat(item["startTime"]).astimezone(NY)
        avg = item["heartRate"]["beatsPerMinuteAvg"]
        if 1 <= local.hour < 5:
            night.append(avg)
        elif 10 <= local.hour < 20:
            day.append(avg)
        peaks += avg >= 110
    assert sum(night) / len(night) + 8 < sum(day) / len(day)
    assert min(night) >= 40 and max(night) < 90
    assert peaks > 30  # bouts on most days, each several windows long


def test_the_night_follows_the_local_clock():
    """The same seed in Tokyo and New York: the low hours are local night in both."""
    for zone in ("Asia/Tokyo", "America/New_York"):
        tz = ZoneInfo(zone)
        by_hour: dict[int, list[float]] = {}
        for item in generate(4, tz, START, START + timedelta(days=20)).items:
            hour = datetime.fromisoformat(item["startTime"]).astimezone(tz).hour
            by_hour.setdefault(hour, []).append(item["heartRate"]["beatsPerMinuteAvg"])
        mean = {h: sum(v) / len(v) for h, v in by_hour.items()}
        assert min(mean, key=mean.get) in range(0, 6), zone


def test_gaps_exist_are_realistic_and_stable():
    first = generate(6, NY, START, START + timedelta(days=30))
    again = generate(6, NY, START, START + timedelta(days=30))
    assert first == again
    assert first.slots == 30 * 288
    assert first.absent == first.slots - len(first.items)
    # Off the wrist for 1% to 10% of windows: some gaps, never most of the data.
    assert 0.01 * first.slots < first.absent < 0.10 * first.slots
    # At least one gap is a run of several consecutive windows (charging), not only drops.
    present = {s.start for s in slots(6, NY, START, START + timedelta(days=30)) if s.item}
    runs, run = [], 0
    t = START
    while t < START + timedelta(days=30):
        if t in present:
            if run:
                runs.append(run)
            run = 0
        else:
            run += 1
        t += timedelta(minutes=5)
    # Both kinds of gap: off-wrist stretches (charging, 20 to 75 minutes) on most days...
    assert sum(1 for r in runs if r >= 4) >= 15
    # ...and single missing windows scattered through the day (about 1%).
    assert sum(1 for r in runs if r == 1) >= 20


def test_a_day_is_about_288_windows_minus_gaps():
    for day in range(10):
        start = START + timedelta(days=day)
        result = generate(8, NY, start, start + timedelta(days=1))
        assert result.slots == 288
        assert 250 <= len(result.items) <= 288


def test_unaligned_bounds_are_refused():
    with pytest.raises(ValueError, match="five-minute"):
        window_at(1, NY, START + timedelta(minutes=2))
    with pytest.raises(ValueError, match="five-minute"):
        generate(1, NY, START, START + timedelta(minutes=7))


def test_floor_to_window():
    assert floor_to_window(datetime(2026, 3, 10, 12, 4, 59, 999, tzinfo=UTC)) == datetime(
        2026, 3, 10, 12, 0, tzinfo=UTC
    )
    assert floor_to_window(datetime(2026, 3, 10, 8, 5, tzinfo=NY)) == datetime(
        2026, 3, 10, 12, 5, tzinfo=UTC
    )
