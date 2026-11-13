"""Deterministic conditional-update keys (T06 AC 2)."""

import json
from dataclasses import replace

from pulsebridge_fhir import (
    GoogleDataSource,
    build_blood_pressure_observation,
    build_device,
    build_heart_rate_observation,
    build_patient,
    heart_rate_transaction,
    parse_google_utc_offset,
    window_from_rollup,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_BP,
    SYNTHETIC_CUFF,
    SYNTHETIC_PATIENT,
    SYNTHETIC_RECORDED,
    SYNTHETIC_ROLLUP,
    SYNTHETIC_WATCH,
)
from pulsebridge_fhir.keys import conditional_url, escape_search_value

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
    assert a.request_url == b.request_url
    assert a.full_url == b.full_url
    assert a.resource == b.resource


def test_request_is_conditional_update_on_identifier():
    built = hr_built()
    assert built.request_url == f"Observation?identifier=urn:pulsebridge:observation|{built.key}"
    assert built.resource["identifier"] == [
        {"use": "official", "system": "urn:pulsebridge:observation", "value": built.key}
    ]
    assert built.bundle_entry()["request"] == {"method": "PUT", "url": built.request_url}


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


def test_key_is_built_from_the_utc_instant_not_the_display_offset():
    patient = build_patient(SYNTHETIC_PATIENT)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    window = window_from_rollup(POINT)
    a = build_heart_rate_observation(window, patient=patient, device=watch, tz=TZ)
    b = build_heart_rate_observation(
        window, patient=patient, device=watch, tz=parse_google_utc_offset("3600s")
    )
    assert a.key == b.key


def test_heart_rate_and_bp_keys_never_collide():
    """The LOINC code is part of the key, so two kinds of reading cannot share an Observation."""
    patient = build_patient(SYNTHETIC_PATIENT)
    cuff = build_device(SYNTHETIC_CUFF, patient=patient)
    bp = build_blood_pressure_observation(SYNTHETIC_BP, patient=patient, device=cuff)
    assert bp.key != hr_built().key


def test_bp_key_follows_source_key_only():
    patient = build_patient(SYNTHETIC_PATIENT)
    cuff = build_device(SYNTHETIC_CUFF, patient=patient)

    def key(reading):
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
                data_source=GoogleDataSource(),
                tz=TZ,
                recorded=SYNTHETIC_RECORDED,
            ),
            sort_keys=True,
        )

    assert build() == build()


def test_search_value_escaping():
    # R4 search escaping: \ | , $ are preceded by a backslash.
    assert escape_search_value(r"a|b,c$d\e") == r"a\|b\,c\$d\\e"
    url = conditional_url("Patient", "urn:pulsebridge:patient", "PB|1 x")
    assert url == "Patient?identifier=urn:pulsebridge:patient|PB%5C|1%20x"
