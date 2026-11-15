"""Deterministic conditional-update keys (T06 AC 2, review fixes 6 and 8)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from pulsebridge_fhir import (
    BloodPressureReading,
    DeviceInput,
    HeartRateWindow,
    blood_pressure_key,
    build_blood_pressure_observation,
    build_device,
    build_heart_rate_observation,
    build_patient,
    heart_rate_key,
    heart_rate_transaction,
    parse_google_utc_offset,
    window_from_rollup,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_BP,
    SYNTHETIC_CUFF,
    SYNTHETIC_DATA_SOURCE,
    SYNTHETIC_PATIENT,
    SYNTHETIC_RECORDED,
    SYNTHETIC_ROLLUP,
    SYNTHETIC_WATCH,
)
from pulsebridge_fhir.keys import conditional_url, deterministic_uuid, escape_search_value

TZ = parse_google_utc_offset("-18000s")
POINT = SYNTHETIC_ROLLUP[0]


def hr_built(point=POINT, patient_input=SYNTHETIC_PATIENT):
    patient = build_patient(patient_input)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    return build_heart_rate_observation(
        window_from_rollup(point), patient=patient, device=watch, tz=TZ
    )


def with_values(point, **heart_rate):
    return dict(point, heartRate=dict(point["heartRate"], **heart_rate))


def test_same_input_twice_same_key_and_request():
    a, b = hr_built(), hr_built()
    assert a.key == b.key
    assert a.update == b.update
    assert a.full_url == b.full_url
    assert a.resource == b.resource


def test_request_is_conditional_update_on_identifier():
    built = hr_built()
    assert built.update == {
        "method": "PUT",
        "url": "Observation?identifier=urn:pulsebridge:observation|"
        "2eb78989-1160-540e-85cd-60b73890a572",
    }
    assert built.resource["identifier"] == [
        {
            "use": "official",
            "system": "urn:pulsebridge:observation",
            "value": "2eb78989-1160-540e-85cd-60b73890a572",
        }
    ]
    assert built.create_if_absent is None  # Observations are only ever updated


def test_keys_are_stable_across_releases():
    """Literal values: a change to how keys are derived would make every stored Observation,
    Device and Provenance unreachable by the next sync, which would then write duplicates."""
    patient = build_patient(SYNTHETIC_PATIENT)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    cuff = build_device(SYNTHETIC_CUFF, patient=patient)
    window = window_from_rollup(POINT)
    assert heart_rate_key("PB-SYN-0001", window) == "2eb78989-1160-540e-85cd-60b73890a572"
    assert blood_pressure_key("PB-SYN-0001", SYNTHETIC_BP) == "ce6a72d2-ab1b-5154-b7c9-fcbec30b2ecf"
    assert watch.key == "1ce5b303-b8ab-57de-837e-76d881dd2db3"
    assert cuff.key == "95eebf63-cf9c-5007-9bca-7c92e84c69fb"
    assert patient.full_url == "urn:uuid:7ac99545-f2c3-5140-9f42-d66791921a54"


def test_heart_rate_key_is_patient_loinc_code_and_utc_start():
    window = window_from_rollup(POINT)
    assert heart_rate_key("PB-SYN-0001", window) == deterministic_uuid(
        "Observation", "PB-SYN-0001", "8867-4", "2026-01-01T14:05:00Z"
    )


def test_key_includes_the_loinc_code():
    window = window_from_rollup(POINT)
    without_code = deterministic_uuid("Observation", "PB-SYN-0001", "2026-01-01T14:05:00Z")
    assert heart_rate_key("PB-SYN-0001", window) != without_code
    bp_without_code = deterministic_uuid("Observation", "PB-SYN-0001", SYNTHETIC_BP.source_key)
    assert blood_pressure_key("PB-SYN-0001", SYNTHETIC_BP) != bp_without_code


def test_hr_and_bp_keys_differ_for_the_same_patient_and_source_string():
    """Only the LOINC code tells these two apart."""
    window = window_from_rollup(POINT)
    reading = replace(SYNTHETIC_BP, source_key="2026-01-01T14:05:00Z")  # same string as the start
    assert heart_rate_key("PB-SYN-0001", window) != blood_pressure_key("PB-SYN-0001", reading)


def test_same_window_in_utc_and_in_minus_five_gives_same_key():
    minus_five = timezone(timedelta(hours=-5))
    utc = HeartRateWindow(
        datetime(2026, 1, 1, 14, 5, tzinfo=UTC),
        datetime(2026, 1, 1, 14, 10, tzinfo=UTC),
        60,
        65,
        70,
    )
    local = HeartRateWindow(
        datetime(2026, 1, 1, 9, 5, tzinfo=minus_five),
        datetime(2026, 1, 1, 9, 10, tzinfo=minus_five),
        60,
        65,
        70,
    )
    assert heart_rate_key("PB-SYN-0001", utc) == heart_rate_key("PB-SYN-0001", local)
    assert local.start_utc == "2026-01-01T14:05:00Z"


def test_key_parts_cannot_collide_by_concatenation():
    assert deterministic_uuid("a|b", "c") != deterministic_uuid("a", "b|c")
    assert deterministic_uuid("ab", "c") != deterministic_uuid("a", "bc")


def test_unicode_forms_give_one_key():
    composed, decomposed = "Café Watch", "Café Watch"
    assert composed != decomposed
    assert deterministic_uuid(composed) == deterministic_uuid(decomposed)
    patient = build_patient(SYNTHETIC_PATIENT)
    a = build_device(DeviceInput(composed), patient=patient)
    b = build_device(DeviceInput(decomposed), patient=patient)
    assert a.key == b.key
    assert a.resource["deviceName"] == b.resource["deviceName"]
    assert a.resource["deviceName"][0]["name"] == composed  # stored in NFC


def test_partly_synced_window_keeps_its_key():
    """New values for the same window start must update the same Observation (T10)."""
    first = hr_built(with_values(POINT, beatsPerMinuteAvg=66.0, beatsPerMinuteMax=70))
    later = hr_built(with_values(POINT, beatsPerMinuteAvg=69.5, beatsPerMinuteMax=81))
    assert first.key == later.key
    assert first.resource["valueQuantity"] != later.resource["valueQuantity"]


def test_key_ignores_window_end():
    longer = dict(POINT, endTime="2026-01-01T14:11:00Z")
    assert hr_built(longer).key == hr_built().key


def test_key_depends_on_start_and_patient():
    later = dict(POINT, startTime="2026-01-01T14:06:00Z")
    assert hr_built(later).key != hr_built().key
    other = replace(SYNTHETIC_PATIENT, patient_id="PB-SYN-0002")
    assert hr_built(patient_input=other).key != hr_built().key


def test_bp_key_follows_source_key_only():
    patient = build_patient(SYNTHETIC_PATIENT)
    cuff = build_device(SYNTHETIC_CUFF, patient=patient)

    def key(reading: BloodPressureReading):
        return build_blood_pressure_observation(reading, patient=patient, device=cuff).key

    assert key(SYNTHETIC_BP) == key(replace(SYNTHETIC_BP, systolic=140))
    assert key(SYNTHETIC_BP) != key(replace(SYNTHETIC_BP, source_key="PULSEBRIDGE_CUFF/MSG-2"))


def test_whole_bundle_is_byte_identical_on_rebuild():
    def build():
        return json.dumps(
            heart_rate_transaction(
                SYNTHETIC_ROLLUP,
                patient=SYNTHETIC_PATIENT,
                device=SYNTHETIC_WATCH,
                data_source=SYNTHETIC_DATA_SOURCE,
                tz=TZ,
                recorded=SYNTHETIC_RECORDED,
                complete_before=None,
            ),
            sort_keys=True,
        )

    assert build() == build()


def test_search_value_escaping():
    # R4 search escaping: \ | , $ are preceded by a backslash.
    assert escape_search_value(r"a|b,c$d\e") == r"a\|b\,c\$d\\e"
    url = conditional_url("Patient", "urn:pulsebridge:patient", "PB|1 x")
    assert url == "Patient?identifier=urn:pulsebridge:patient|PB%5C|1%20x"
