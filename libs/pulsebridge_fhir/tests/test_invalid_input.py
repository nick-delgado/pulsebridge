"""Invalid input raises a clear FhirInputError (T06 AC 6)."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from pulsebridge_fhir import (
    BloodPressureReading,
    DeviceInput,
    FhirInputError,
    HeartRateWindow,
    PatientInput,
    SourceInfo,
    build_device,
    build_heart_rate_observation,
    build_patient,
    parse_google_utc_offset,
    window_from_rollup,
)
from pulsebridge_fhir.examples import SYNTHETIC_BP, SYNTHETIC_PATIENT, SYNTHETIC_WATCH

GOOD = {
    "startTime": "2026-01-01T14:05:00Z",
    "endTime": "2026-01-01T14:10:00Z",
    "heartRate": {"beatsPerMinuteMin": 61, "beatsPerMinuteAvg": 66.4, "beatsPerMinuteMax": 79},
}


def hr_values(**values):
    return dict(GOOD, heartRate=dict(GOOD["heartRate"], **values))


def without(mapping, key):
    return {k: v for k, v in mapping.items() if k != key}


# --- Missing time ---------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["startTime", "endTime"])
def test_missing_window_time(field):
    with pytest.raises(FhirInputError, match=f"{field} is missing"):
        window_from_rollup(without(GOOD, field))


@pytest.mark.parametrize(
    "value",
    ["2026-01-01T14:05:00", "2026-01-01T14:05:00+01:00", "2026-01-01", "14:05", 1767276300],
)
def test_window_time_not_utc(value):
    with pytest.raises(FhirInputError, match="startTime .* is not a UTC time"):
        window_from_rollup(dict(GOOD, startTime=value))


def test_window_time_impossible_date():
    with pytest.raises(FhirInputError, match="not a valid time"):
        window_from_rollup(dict(GOOD, startTime="2026-13-01T14:05:00Z"))


def test_window_end_before_start():
    with pytest.raises(FhirInputError, match="end .* is not after start"):
        window_from_rollup(dict(GOOD, endTime="2026-01-01T14:05:00Z"))


def test_naive_datetime_rejected():
    with pytest.raises(FhirInputError, match="no time zone"):
        HeartRateWindow(
            datetime(2026, 1, 1, 14, 5), datetime(2026, 1, 1, 14, 10, tzinfo=UTC), 60, 65, 70
        )
    with pytest.raises(FhirInputError, match="no time zone"):
        replace(SYNTHETIC_BP, effective=datetime(2026, 1, 1, 9, 30))


def test_missing_bp_time():
    with pytest.raises(FhirInputError, match="effective is missing"):
        replace(SYNTHETIC_BP, effective=None)


# --- Out-of-range and malformed values ------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("beatsPerMinuteMax", 301),
        ("beatsPerMinuteMin", 0),
        ("beatsPerMinuteAvg", -5),
    ],
)
def test_heart_rate_out_of_range(field, value):
    with pytest.raises(FhirInputError, match=rf"\({value}\) is outside 1 to 300"):
        window_from_rollup(hr_values(**{field: value}))


@pytest.mark.parametrize("field", ["beatsPerMinuteMin", "beatsPerMinuteAvg", "beatsPerMinuteMax"])
def test_heart_rate_value_missing(field):
    with pytest.raises(FhirInputError, match=f"{field} is missing"):
        window_from_rollup(dict(GOOD, heartRate=without(GOOD["heartRate"], field)))


@pytest.mark.parametrize("value", ["seventy", True, None, [70], float("nan")])
def test_heart_rate_not_a_number(value):
    point = hr_values(beatsPerMinuteAvg=value)
    if value is None:
        point = dict(GOOD, heartRate=without(GOOD["heartRate"], "beatsPerMinuteAvg"))
    with pytest.raises(FhirInputError, match="beatsPerMinuteAvg|average"):
        window_from_rollup(point)


def test_heart_rate_min_avg_max_out_of_order():
    with pytest.raises(FhirInputError, match="minimum <= average <= maximum"):
        window_from_rollup(hr_values(beatsPerMinuteMin=80))


def test_missing_heart_rate_object():
    with pytest.raises(FhirInputError, match="heartRate is missing"):
        window_from_rollup(without(GOOD, "heartRate"))


@pytest.mark.parametrize(
    ("systolic", "diastolic", "message"),
    [
        (320, 80, "systolic"),
        (16, 10, "systolic"),  # kPa entered as mm[Hg]
        (120, 210, "diastolic"),
        (80, 90, "must be greater than"),
    ],
)
def test_blood_pressure_out_of_range(systolic, diastolic, message):
    with pytest.raises(FhirInputError, match=message):
        replace(SYNTHETIC_BP, systolic=systolic, diastolic=diastolic)


# --- Wrong unit -----------------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["mmHg", "kPa", "mm Hg", ""])
def test_blood_pressure_wrong_unit(unit):
    with pytest.raises(FhirInputError, match=rf"unit '{unit}' is not the UCUM code 'mm\[Hg\]'"):
        BloodPressureReading(
            effective=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            systolic=120,
            diastolic=80,
            source_key="SYN/1",
            unit=unit,
        )


# --- UTC offset -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("-18000", "format"),
        ("-5h", "format"),
        ("-18030s", "whole number of minutes"),
        ("54000s", "outside -14:00 to \\+14:00"),
    ],
)
def test_bad_google_offset(value, message):
    with pytest.raises(FhirInputError, match=message):
        parse_google_utc_offset(value)


# --- Patient, device, source, status --------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"patient_id": ""}, "patient_id is missing"),
        ({"patient_id": " PB-1"}, "whitespace"),
        ({"family": ""}, "family is missing"),
        ({"given": ()}, "given must be"),
        ({"given": "Avery"}, "given must be"),
        ({"gender": "F"}, "gender 'F' is not one of"),
        ({"birth_date": "1980-04-12"}, "birth_date must be a date"),
    ],
)
def test_bad_patient(changes, message):
    with pytest.raises(FhirInputError, match=message):
        replace(SYNTHETIC_PATIENT, **changes)


def test_patient_needs_birth_date_type_not_datetime():
    with pytest.raises(FhirInputError, match="birth_date"):
        PatientInput("PB-1", "Example", ("Avery",), "female", datetime(1980, 4, 12))
    PatientInput("PB-1", "Example", ("Avery",), "female", date(1980, 4, 12))


def test_bad_device_name():
    with pytest.raises(FhirInputError, match="display_name is missing"):
        DeviceInput(display_name="")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"platform": "fitbit"}, "upper-case token"),
        ({"recording_method": "passive"}, "upper-case token"),
        ({"record_type": "HL7 message"}, "lower-case words"),
        ({"record_id": ""}, "record_id is missing"),
    ],
)
def test_bad_source(changes, message):
    base = SourceInfo(
        platform="FITBIT", record_type="google-health-heart-rate-rollup", record_id="x"
    )
    with pytest.raises(FhirInputError, match=message):
        replace(base, **changes)


def test_bad_status_and_wrong_resource_roles():
    patient = build_patient(SYNTHETIC_PATIENT)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    window = window_from_rollup(GOOD)
    with pytest.raises(FhirInputError, match="status 'done'"):
        build_heart_rate_observation(window, patient=patient, device=watch, tz=UTC, status="done")
    with pytest.raises(FhirInputError, match="device must be a Device"):
        build_heart_rate_observation(window, patient=patient, device=patient, tz=UTC)
    with pytest.raises(FhirInputError, match="expected a Patient"):
        build_heart_rate_observation(window, patient=watch, device=watch, tz=UTC)


def test_error_is_a_value_error():
    assert issubclass(FhirInputError, ValueError)
