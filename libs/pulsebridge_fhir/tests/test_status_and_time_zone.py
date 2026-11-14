"""preliminary vs final for windows still filling (review fix 3), and DST handling (fix 2)."""

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pulsebridge_fhir import (
    FhirInputError,
    heart_rate_transaction,
    parse_google_utc_offset,
    window_from_rollup,
    window_status,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_DATA_SOURCE,
    SYNTHETIC_PATIENT_REF,
    SYNTHETIC_RECORDED,
    SYNTHETIC_ROLLUP,
    SYNTHETIC_WATCH,
)

FIRST, SECOND = (window_from_rollup(p) for p in SYNTHETIC_ROLLUP)  # end 14:10Z and 14:15Z


def statuses(complete_before, points=SYNTHETIC_ROLLUP, tz=UTC):
    bundle = heart_rate_transaction(
        points,
        patient=SYNTHETIC_PATIENT_REF,
        device=SYNTHETIC_WATCH,
        data_source=SYNTHETIC_DATA_SOURCE,
        tz=tz,
        recorded=SYNTHETIC_RECORDED,
        complete_before=complete_before,
    )
    return [
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"
    ]


def test_windows_after_the_cutoff_are_preliminary():
    obs = statuses(datetime(2026, 1, 1, 14, 12, tzinfo=UTC))
    assert [o["status"] for o in obs] == ["final", "preliminary"]


def test_window_ending_exactly_at_the_cutoff_is_final():
    assert window_status(FIRST, datetime(2026, 1, 1, 14, 10, tzinfo=UTC)) == "final"
    assert window_status(SECOND, datetime(2026, 1, 1, 14, 10, tzinfo=UTC)) == "preliminary"


def test_cutoff_in_another_zone_is_the_same_instant():
    cutoff = datetime(2026, 1, 1, 9, 12, tzinfo=timezone(timedelta(hours=-5)))  # 14:12Z
    assert [o["status"] for o in statuses(cutoff)] == ["final", "preliminary"]


def test_all_final_when_caller_declares_complete():
    assert [o["status"] for o in statuses(None)] == ["final", "final"]


def test_all_preliminary_when_nothing_is_complete_yet():
    obs = statuses(datetime(2026, 1, 1, 14, 0, tzinfo=UTC))
    assert [o["status"] for o in obs] == ["preliminary", "preliminary"]


def test_rewriting_as_final_keeps_the_same_observation():
    early = statuses(datetime(2026, 1, 1, 14, 0, tzinfo=UTC))
    later = statuses(None)
    assert [o["identifier"] for o in early] == [o["identifier"] for o in later]


def test_naive_cutoff_rejected():
    with pytest.raises(FhirInputError, match="complete_before must be a datetime with a time zone"):
        window_status(FIRST, datetime(2026, 1, 1, 14, 10))


def test_complete_before_is_required():
    with pytest.raises(TypeError):
        heart_rate_transaction(
            SYNTHETIC_ROLLUP,
            patient=SYNTHETIC_PATIENT_REF,
            device=SYNTHETIC_WATCH,
            data_source=SYNTHETIC_DATA_SOURCE,
            tz=UTC,
            recorded=SYNTHETIC_RECORDED,
        )


def test_zone_handles_a_dst_change_inside_one_sync_fixed_offset_does_not():
    # US clocks went forward at 2026-03-08 07:00Z (02:00 EST -> 03:00 EDT).
    points = [
        {
            "startTime": "2026-03-08T06:55:00Z",
            "endTime": "2026-03-08T07:00:00Z",
            "heartRate": {
                "beatsPerMinuteMin": 60,
                "beatsPerMinuteAvg": 62,
                "beatsPerMinuteMax": 64,
            },
        },
        {
            "startTime": "2026-03-08T07:05:00Z",
            "endTime": "2026-03-08T07:10:00Z",
            "heartRate": {
                "beatsPerMinuteMin": 60,
                "beatsPerMinuteAvg": 62,
                "beatsPerMinuteMax": 64,
            },
        },
    ]
    zone = statuses(None, points, tz=ZoneInfo("America/New_York"))
    assert [o["effectivePeriod"]["start"] for o in zone] == [
        "2026-03-08T01:55:00-05:00",
        "2026-03-08T03:05:00-04:00",
    ]
    fixed = statuses(None, points, tz=parse_google_utc_offset("-18000s"))
    # Same instants, but the second is written in the wrong local offset (an hour off on a clock).
    assert fixed[1]["effectivePeriod"]["start"] == "2026-03-08T02:05:00-05:00"
