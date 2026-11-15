"""Synthetic five-minute heart-rate windows, in the Google Health roll-up shape.

Each window is the JSON item spike S3 recorded for ``dataPoints:rollUp`` (``docs/spikes/s3.md``;
only its shape is used here, never any of its data)::

    {"startTime": "2026-01-01T14:05:00Z", "endTime": "2026-01-01T14:10:00Z",
     "heartRate": {"beatsPerMinuteMin": 70, "beatsPerMinuteAvg": 72.4, "beatsPerMinuteMax": 75}}

so the seeder feeds T06's ``heart_rate_transaction`` exactly what ``health-sync`` does.

Determinism without state
-------------------------

Every random number is a hash, not a draw from a generator: ``_unit(seed, purpose, *parts)`` is
BLAKE2b over a canonical JSON list, read as a number in ``[0, 1)``. There is no ``random``
module, no ``hash()`` (salted per process) and nothing that depends on how many numbers were
taken before. So:

- the value of a window depends only on the seed, the zone the synthetic person lives in
  (``TIME_ZONE``) and the window's UTC start, never on when the seeder ran or which range it
  was asked for. Re-running later over an overlapping range gives identical values for the
  windows both runs cover; that is what makes a re-run rewrite the same Observations with the
  same content instead of rewriting history;
- whether a window is absent (watch off the wrist) is decided the same way, so gaps are stable.

Why the zone is an input: a person's rhythm follows the local clock (lower at night, higher in
the day). With the rhythm on UTC, a deployment in Tokyo would show the night dip mid-morning.
The zone is configuration that stays fixed for a deployment, like the seed; changing it is like
choosing another synthetic person. The window *keys* never depend on it (T06 keys on the UTC
start).

The model, per local day (all parameters hashed from the seed and the local date):

- a resting rate for the person (54-66 /min), varied a little day to day;
- a wake time (06:00-08:00) and a sleep time (22:00-23:30): asleep is about 4 below resting,
  awake about 14 above, with a 30-minute ramp between;
- zero to two exercise bouts (20-60 minutes, peak 115-165 /min) with a warm-up and cool-down;
- usually one off-wrist gap (charging, 20-75 minutes) in the morning, plus about 1% of windows
  missing at random. Absent windows are left out, as the API leaves out windows with no data.

A slow drift (value noise on a 30-minute UTC grid, interpolated) keeps neighbouring windows
close, and a small per-window jitter keeps them from being identical. Minimum and maximum are
whole numbers (JSON numbers, as S3 recorded) around the decimal average, and always
``minimum <= average <= maximum`` inside ``HEART_RATE_BOUNDS``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

WINDOW = timedelta(minutes=5)  # decision D8: five-minute roll-ups, about 288 a day
WINDOW_SECONDS = int(WINDOW.total_seconds())
DRIFT_STEP_SECONDS = 30 * 60
# Physiological bounds the generator keeps to; inside T06's accepted 1 to 300 /min.
HEART_RATE_BOUNDS = (38, 195)
RANDOM_DROP_PROBABILITY = 0.01
# Bump when the model changes in a way that changes values: the version is hashed into every
# number, so old and new values never mix silently under one seed.
MODEL_VERSION = "pulsebridge-seed/v1"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _unit(seed: int, purpose: str, *parts: object) -> float:
    """A deterministic number in ``[0, 1)`` for ``(seed, purpose, parts)``.

    Canonical JSON keeps the parts apart (``("ab", "c")`` and ``("a", "bc")`` hash differently);
    BLAKE2b gives the same digest on every machine and Python version."""
    data = json.dumps([MODEL_VERSION, seed, purpose, *parts], separators=(",", ":"))
    digest = hashlib.blake2b(data.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def floor_to_window(value: datetime) -> datetime:
    """Round down to a five-minute boundary in UTC (whole seconds), as health-sync does: the API
    starts windows at the range start, so ranges must be aligned for keys to repeat."""
    seconds = int((value.astimezone(UTC) - _EPOCH).total_seconds() // 1)
    return _EPOCH + timedelta(seconds=seconds - seconds % WINDOW_SECONDS)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Bout:
    start_minute: float  # minutes after local midnight
    duration: float  # minutes
    peak: float  # /min


@dataclass(frozen=True)
class DayPlan:
    """What one local day of the synthetic person looks like. Hashed from (seed, local date)."""

    resting: float
    wake_minute: float
    sleep_minute: float
    bouts: tuple[Bout, ...]
    off_wrist: tuple[float, float] | None  # (start minute, duration), or no gap that day


def day_plan(seed: int, day: date) -> DayPlan:
    key = day.isoformat()
    person_resting = 54 + 12 * _unit(seed, "person-resting")
    resting = person_resting + 4 * (_unit(seed, "day-resting", key) - 0.5)
    wake = 360 + 120 * _unit(seed, "wake", key)  # 06:00 to 08:00
    sleep = 1320 + 90 * _unit(seed, "sleep", key)  # 22:00 to 23:30
    draw = _unit(seed, "bout-count", key)
    count = 0 if draw < 0.25 else 1 if draw < 0.8 else 2
    bouts = []
    for index in range(count):
        duration = 20 + 40 * _unit(seed, "bout-duration", key, index)
        earliest, latest = wake + 60, sleep - 120 - duration
        start = earliest + (latest - earliest) * _unit(seed, "bout-start", key, index)
        peak = 115 + 50 * _unit(seed, "bout-peak", key, index)
        bouts.append(Bout(start, duration, peak))
    off_wrist = None
    if _unit(seed, "off-wrist", key) < 0.8:
        gap_start = wake + 15 + 165 * _unit(seed, "off-wrist-start", key)
        off_wrist = (gap_start, 20 + 55 * _unit(seed, "off-wrist-duration", key))
    return DayPlan(resting, wake, sleep, tuple(bouts), off_wrist)


def _drift(seed: int, epoch_seconds: int) -> float:
    """Smooth noise in ``[-1, 1]``: hashed points every 30 minutes (UTC), cosine-interpolated."""
    step, offset = divmod(epoch_seconds, DRIFT_STEP_SECONDS)
    a = 2 * _unit(seed, "drift", step) - 1
    b = 2 * _unit(seed, "drift", step + 1) - 1
    t = (1 - math.cos(math.pi * offset / DRIFT_STEP_SECONDS)) / 2
    return a + (b - a) * t


def _awake_share(plan: DayPlan, minute: float) -> float:
    """0 asleep, 1 awake, with a 30-minute ramp after waking and before sleeping."""
    ramp = 30.0
    if minute < plan.wake_minute or minute >= plan.sleep_minute:
        return 0.0
    return min(1.0, (minute - plan.wake_minute) / ramp, (plan.sleep_minute - minute) / ramp)


def _exercise_level(plan: DayPlan, minute: float) -> float | None:
    """The bout's heart rate at ``minute``: 5-minute warm-up, 10-minute cool-down after."""
    best = None
    for bout in plan.bouts:
        end = bout.start_minute + bout.duration
        if bout.start_minute <= minute < end:
            share = min(1.0, (minute - bout.start_minute) / 5)
        elif end <= minute < end + 10:
            share = 1 - (minute - end) / 10
        else:
            continue
        level = plan.resting + (bout.peak - plan.resting) * share
        best = level if best is None else max(best, level)
    return best


@dataclass(frozen=True)
class Slot:
    """One five-minute window of the range: the item if the watch recorded it, else ``None``."""

    start: datetime
    item: dict | None


def window_at(seed: int, tz: tzinfo, start: datetime) -> dict | None:
    """The roll-up item for the window starting at ``start`` (UTC, aligned), or ``None`` when
    the synthetic watch was off the wrist. A pure function of ``(seed, tz, start)``."""
    if start != floor_to_window(start):
        raise ValueError("window start must be on a five-minute boundary")
    start = start.astimezone(UTC)
    epoch_seconds = int((start - _EPOCH).total_seconds())
    stamp = _utc(start)
    local = start.astimezone(tz)
    plan = day_plan(seed, local.date())
    minute = local.hour * 60 + local.minute

    if plan.off_wrist is not None:
        gap_start, gap_length = plan.off_wrist
        if gap_start <= minute < gap_start + gap_length:
            return None
    if _unit(seed, "drop", stamp) < RANDOM_DROP_PROBABILITY:
        return None

    awake = _awake_share(plan, minute)
    level = plan.resting - 4 + 18 * awake + (2 + 4 * awake) * _drift(seed, epoch_seconds)
    exercise = _exercise_level(plan, minute)
    active = exercise is not None and exercise > level
    if active:
        level = exercise
    low, high = HEART_RATE_BOUNDS
    # Keep the average far enough inside the bounds that min and max fit around it.
    average = min(max(level + 1.5 * (2 * _unit(seed, "jitter", stamp) - 1), low + 3), high - 3)
    if active:
        below, above = 6 + 9 * _unit(seed, "below", stamp), 6 + 9 * _unit(seed, "above", stamp)
    else:
        below = 2 + (3 + 5 * awake) * _unit(seed, "below", stamp)
        above = 2 + (4 + 8 * awake) * _unit(seed, "above", stamp)
    average = round(average, 2)
    minimum = max(low, math.floor(average - below))
    maximum = min(high, math.ceil(average + above))
    return {
        "startTime": stamp,
        "endTime": _utc(start + WINDOW),
        "heartRate": {
            "beatsPerMinuteMin": minimum,
            "beatsPerMinuteAvg": average,
            "beatsPerMinuteMax": maximum,
        },
    }


def slots(seed: int, tz: tzinfo, start: datetime, end: datetime) -> Iterator[Slot]:
    """Every window in ``[start, end)``, oldest first, present or absent. Both bounds must be on
    five-minute boundaries. (Google sends newest first; order does not matter to T06.)"""
    if start != floor_to_window(start) or end != floor_to_window(end):
        raise ValueError("range bounds must be on five-minute boundaries")
    current = start.astimezone(UTC)
    while current < end:
        yield Slot(current, window_at(seed, tz, current))
        current += WINDOW


@dataclass(frozen=True)
class Generated:
    items: list[dict]  # the windows the synthetic watch recorded, oldest first
    slots: int  # windows in the range
    absent: int  # windows with no data (left out, like the API does)


def generate(seed: int, tz: tzinfo, start: datetime, end: datetime) -> Generated:
    items, total = [], 0
    for slot in slots(seed, tz, start, end):
        total += 1
        if slot.item is not None:
            items.append(slot.item)
    return Generated(items, total, total - len(items))


__all__ = [
    "HEART_RATE_BOUNDS",
    "WINDOW",
    "DayPlan",
    "Generated",
    "Slot",
    "day_plan",
    "floor_to_window",
    "generate",
    "slots",
    "window_at",
]
