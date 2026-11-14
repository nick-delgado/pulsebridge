"""Heart-rate Observation from a Google Health roll-up window (T06 AC 1, 3, 4)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pulsebridge_fhir import (
    build_device,
    build_heart_rate_observation,
    build_patient,
    parse_google_utc_offset,
    window_from_rollup,
)
from pulsebridge_fhir.examples import SYNTHETIC_PATIENT, SYNTHETIC_WATCH

ROLLUP = {
    "startTime": "2026-01-01T14:05:00Z",
    "endTime": "2026-01-01T14:10:00Z",
    "heartRate": {"beatsPerMinuteMin": 61, "beatsPerMinuteAvg": 66.4, "beatsPerMinuteMax": 79},
}


@pytest.fixture
def built():
    patient = build_patient(SYNTHETIC_PATIENT)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    return patient, watch


def hr(built, point=ROLLUP, tz=None):
    patient, watch = built
    tz = tz if tz is not None else parse_google_utc_offset("-18000s")
    return build_heart_rate_observation(
        window_from_rollup(point), patient=patient, device=watch, tz=tz
    ).resource


def component(obs, loinc):
    matches = [c for c in obs["component"] if c["code"]["coding"][0]["code"] == loinc]
    assert len(matches) == 1, f"expected one component {loinc}"
    return matches[0]


def test_meets_base_vital_signs_profile(built, vital_signs_profile):
    vital_signs_profile(hr(built))


def test_heart_rate_profile_fixed_code_and_unit(built):
    obs = hr(built)
    # heartrate profile: code.coding contains LOINC 8867-4; valueQuantity code "/min" in UCUM.
    assert {"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"} in obs["code"][
        "coding"
    ]
    assert obs["valueQuantity"]["system"] == "http://unitsofmeasure.org"
    assert obs["valueQuantity"]["code"] == "/min"
    assert obs["meta"]["profile"] == ["http://hl7.org/fhir/StructureDefinition/heartrate"]


def test_every_required_element_present(built):
    """AC: status, vital-signs category, code, subject, effective time, value with UCUM unit,
    and a device reference."""
    patient, watch = built
    obs = hr(built)
    assert obs["status"] == "final"
    assert obs["category"][0]["coding"][0]["code"] == "vital-signs"
    assert obs["subject"] == {"reference": patient.full_url}
    assert obs["device"] == {"reference": watch.full_url}
    assert "effectivePeriod" in obs
    assert "effectiveDateTime" not in obs


def test_average_is_value_min_and_max_are_components(built):
    obs = hr(built)
    assert obs["valueQuantity"]["value"] == 66.4
    minimum = component(obs, "96935-2")
    maximum = component(obs, "55426-1")
    assert minimum["valueQuantity"]["value"] == 61
    assert maximum["valueQuantity"]["value"] == 79
    for comp in (minimum, maximum):
        assert comp["valueQuantity"]["code"] == "/min"
        assert comp["code"]["coding"][0]["system"] == "http://loinc.org"
    assert len(obs["component"]) == 2


def test_component_codes_and_displays_are_the_verified_loinc_entries(built):
    obs = hr(built)
    assert component(obs, "96935-2")["code"]["coding"][0]["display"] == (
        "Heart rate unspecified time minimum by Pedometer"
    )
    assert component(obs, "55426-1")["code"]["coding"][0]["display"] == (
        "Heart rate unspecified time maximum by Pedometer"
    )


def test_period_uses_users_utc_offset(built):
    obs = hr(built)  # window 14:05Z to 14:10Z, offset -18000s (UTC-05:00)
    assert obs["effectivePeriod"] == {
        "start": "2026-01-01T09:05:00-05:00",
        "end": "2026-01-01T09:10:00-05:00",
    }


def test_offset_changes_text_not_instant(built):
    east = hr(built, tz=parse_google_utc_offset("+19800s"))  # UTC+05:30
    assert east["effectivePeriod"]["start"] == "2026-01-01T19:35:00+05:30"
    utc = hr(built, tz=UTC)
    assert utc["effectivePeriod"]["start"] == "2026-01-01T14:05:00+00:00"
    assert datetime.fromisoformat(east["effectivePeriod"]["start"]) == datetime.fromisoformat(
        utc["effectivePeriod"]["start"]
    )


def test_zoneinfo_follows_daylight_saving(built):
    summer = dict(ROLLUP, startTime="2026-07-01T14:05:00Z", endTime="2026-07-01T14:10:00Z")
    obs = hr(built, summer, tz=ZoneInfo("America/New_York"))
    assert obs["effectivePeriod"]["start"] == "2026-07-01T10:05:00-04:00"
    obs = hr(built, tz=ZoneInfo("America/New_York"))
    assert obs["effectivePeriod"]["start"] == "2026-01-01T09:05:00-05:00"


def test_accepts_s3_rollup_shape_numbers_and_decimal_average():
    window = window_from_rollup(ROLLUP)
    assert window.start == datetime(2026, 1, 1, 14, 5, tzinfo=UTC)
    assert window.end == datetime(2026, 1, 1, 14, 10, tzinfo=UTC)
    assert (window.bpm_min, window.bpm_avg, window.bpm_max) == (61, 66.4, 79)


def test_accepts_int64_as_string():
    point = dict(
        ROLLUP,
        heartRate={
            "beatsPerMinuteMin": "61",
            "beatsPerMinuteAvg": "66.4",
            "beatsPerMinuteMax": "79",
        },
    )
    window = window_from_rollup(point)
    assert (window.bpm_min, window.bpm_avg, window.bpm_max) == (61, 66.4, 79)
    assert isinstance(window.bpm_min, int)


def test_status_can_be_preliminary(built):
    patient, watch = built
    obs = build_heart_rate_observation(
        window_from_rollup(ROLLUP), patient=patient, device=watch, tz=UTC, status="preliminary"
    ).resource
    assert obs["status"] == "preliminary"
