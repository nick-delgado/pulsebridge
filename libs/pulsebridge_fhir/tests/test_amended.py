"""T33: heart_rate_transaction and heart_rate_resources write named windows as ``amended``.

R4 observation-status ``amended``: "Subsequent to being Final, the observation has been
modified". All data is synthetic.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pulsebridge_fhir import (
    FhirInputError,
    build_device,
    build_patient,
    heart_rate_key,
    heart_rate_resources,
    heart_rate_transaction,
    window_from_rollup,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_DATA_SOURCE,
    SYNTHETIC_PATIENT_REF,
    SYNTHETIC_RECORDED,
    SYNTHETIC_ROLLUP,
    SYNTHETIC_WATCH,
)

FIRST_START = datetime(2026, 1, 1, 14, 5, tzinfo=UTC)  # ends 14:10Z
SECOND_START = datetime(2026, 1, 1, 14, 10, tzinfo=UTC)  # ends 14:15Z
CUTOFF = datetime(2026, 1, 1, 14, 12, tzinfo=UTC)  # first final, second preliminary


def observations(amended=(), complete_before=CUTOFF):
    bundle = heart_rate_transaction(
        SYNTHETIC_ROLLUP,
        patient=SYNTHETIC_PATIENT_REF,
        device=SYNTHETIC_WATCH,
        data_source=SYNTHETIC_DATA_SOURCE,
        tz=UTC,
        recorded=SYNTHETIC_RECORDED,
        complete_before=complete_before,
        amended=amended,
    )
    return [e for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"]


def test_default_is_unchanged_final_and_preliminary():
    assert [e["resource"]["status"] for e in observations()] == ["final", "preliminary"]


def test_a_named_complete_window_is_written_amended(vital_signs_profile):
    entries = observations(amended=[FIRST_START], complete_before=None)
    assert [e["resource"]["status"] for e in entries] == ["amended", "final"]
    vital_signs_profile(entries[0]["resource"])
    # Same key and the same conditional update as the final it replaces.
    plain = observations(complete_before=None)
    assert entries[0]["request"] == plain[0]["request"]
    assert entries[0]["resource"]["identifier"] == plain[0]["resource"]["identifier"]


def test_start_in_another_zone_names_the_same_window():
    local = FIRST_START.astimezone(timezone(timedelta(hours=-5)))  # 09:05-05:00
    assert [e["resource"]["status"] for e in observations(amended={local})] == [
        "amended",
        "preliminary",
    ]


def test_a_start_that_would_be_preliminary_is_refused():
    with pytest.raises(FhirInputError, match="not complete yet"):
        observations(amended=[SECOND_START])


def test_a_start_not_in_the_batch_is_refused():
    with pytest.raises(FhirInputError, match="no window in this batch starts at"):
        observations(amended=[FIRST_START + timedelta(minutes=1)])


@pytest.mark.parametrize(
    "bad",
    [
        FIRST_START,  # a bare datetime, not a collection
        "2026-01-01T14:05:00Z",
        [datetime(2026, 1, 1, 14, 5)],  # naive
        ["2026-01-01T14:05:00Z"],
    ],
)
def test_amended_must_be_aware_datetimes(bad):
    with pytest.raises(FhirInputError, match="amended"):
        observations(amended=bad)


def test_heart_rate_resources_takes_amended_too():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    device = build_device(SYNTHETIC_WATCH, patient=patient)
    windows = [window_from_rollup(p) for p in SYNTHETIC_ROLLUP]
    built = heart_rate_resources(
        windows,
        patient=patient,
        device=device,
        data_source=SYNTHETIC_DATA_SOURCE,
        tz=UTC,
        recorded=SYNTHETIC_RECORDED,
        complete_before=None,
        amended=(SECOND_START,),
    )
    obs = [b.resource for b in built if b.resource["resourceType"] == "Observation"]
    assert [o["status"] for o in obs] == ["final", "amended"]
    assert built[2].key == heart_rate_key(patient.key, windows[1])


def test_heart_rate_resources_accepts_a_generator_of_windows():
    # windows is an Iterable: it is read once even though amended is checked against it.
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    device = build_device(SYNTHETIC_WATCH, patient=patient)
    built = heart_rate_resources(
        (window_from_rollup(p) for p in SYNTHETIC_ROLLUP),
        patient=patient,
        device=device,
        data_source=SYNTHETIC_DATA_SOURCE,
        tz=UTC,
        recorded=SYNTHETIC_RECORDED,
        complete_before=None,
        amended=[FIRST_START],
    )
    assert [b.resource.get("status") for b in built[::2]] == ["amended", "final"]
