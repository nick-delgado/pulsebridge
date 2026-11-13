"""ORU^R01 builder: AC "passes hl7apy validation" and AC "parses back to the same values"."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pulsebridge_hl7v2 import (
    BloodPressureReading,
    Patient,
    build_bp_oru_r01,
    new_control_id,
    parse_er7,
    to_er7_bytes,
    validate_oru_r01,
)
from pulsebridge_hl7v2.samples import load

PATIENT = Patient("SYN-000123", "PULSEBRIDGE", "TESTPATIENT", "SYNTHIA")
SENT = datetime(2026, 11, 13, 20, 53, 29, tzinfo=UTC)
EASTERN = timezone(timedelta(hours=-5))


def build(systolic=120, diastolic=80, observed=SENT, patient=PATIENT, control_id="SYN-VALID-0001"):
    reading = BloodPressureReading(systolic, diastolic, observed)
    return build_bp_oru_r01(reading, patient, control_id=control_id, sent_at=SENT)


def observations(message):
    """(OBX-3.1, OBX-3.3, OBX-5, OBX-6.1, OBX-6.3, OBX-14) per OBX, read through hl7apy."""
    order = message.oru_r01_patient_result.oru_r01_order_observation
    rows = []
    for group in order.oru_r01_observation:
        obx = group.obx
        rows.append(
            (
                obx.obx_3.obx_3_1.to_er7(),
                obx.obx_3.obx_3_3.to_er7(),
                obx.obx_5.to_er7(),
                obx.obx_6.obx_6_1.to_er7(),
                obx.obx_6.obx_6_3.to_er7(),
                obx.obx_14.to_er7(),
            )
        )
    return rows


def test_built_message_passes_hl7apy_validation():
    validated = validate_oru_r01(to_er7_bytes(build()))
    assert validated.msh.msh_9.to_er7() == "ORU^R01^ORU_R01"
    assert validated.msh.msh_12.to_er7() == "2.5.1"


def test_round_trip_keeps_patient_codes_values_units_and_time():
    observed = datetime(2026, 11, 13, 15, 53, 29, tzinfo=EASTERN)
    wire = to_er7_bytes(build(135, 85, observed))
    parsed = parse_er7(wire)

    pid = parsed.oru_r01_patient_result.oru_r01_patient.pid
    assert pid.pid_3.cx_1.to_er7() == "SYN-000123"
    assert pid.pid_3.cx_4.to_er7() == "PULSEBRIDGE"
    assert pid.pid_3.cx_5.to_er7() == "MR"
    assert pid.pid_5.to_er7() == "TESTPATIENT^SYNTHIA"
    obr = parsed.oru_r01_patient_result.oru_r01_order_observation.obr
    assert obr.obr_4.obr_4_1.to_er7() == "85354-9"
    assert observations(parsed) == [
        ("8480-6", "LN", "135", "mm[Hg]", "UCUM", "20261113155329-0500"),
        ("8462-4", "LN", "85", "mm[Hg]", "UCUM", "20261113155329-0500"),
    ]
    assert parsed.msh.msh_10.to_er7() == "SYN-VALID-0001"


def test_wire_bytes_end_every_segment_with_carriage_return_and_never_line_feed():
    wire = to_er7_bytes(build())
    assert b"\n" not in wire
    assert wire.endswith(b"\r")
    assert wire.count(b"\r") == 5  # MSH, PID, OBR, OBX, OBX


def test_builder_output_matches_the_committed_valid_sample():
    # The valid sample is the builder's output; if either drifts, T12/T13 fixtures go stale.
    assert to_er7_bytes(build()) == load("valid_bp_reading").payload


def test_separator_characters_in_names_are_escaped_not_injected():
    tricky = Patient("SYN-1|X", "PULSEBRIDGE", "O^BRIEN&CO", "ANN~MARIE\\")
    wire = to_er7_bytes(build(patient=tricky))
    validate_oru_r01(wire)
    pid_segment = next(s for s in wire.split(b"\r") if s.startswith(b"PID"))
    assert pid_segment == (
        b"PID|1||SYN-1\\F\\X^^^PULSEBRIDGE^MR||O\\S\\BRIEN\\T\\CO^ANN\\R\\MARIE\\E\\"
    )
    parsed = parse_er7(wire)
    # Still exactly the two components, so nothing leaked into a new field or component.
    assert len(parsed.oru_r01_patient_result.oru_r01_patient.pid.pid_5.children) == 2


@pytest.mark.parametrize(
    ("systolic", "diastolic", "match"),
    [
        (80, 120, "greater than diastolic"),
        (0, -1, "between 1 and 400"),
        (401, 80, "between 1 and 400"),
        (120.5, 80, "integer"),
        (True, 0, "integer"),
    ],
)
def test_builder_refuses_impossible_readings(systolic, diastolic, match):
    with pytest.raises(ValueError, match=match):
        build(systolic, diastolic)


def test_builder_refuses_naive_observation_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        build(observed=datetime(2026, 11, 13, 20, 53, 29))


@pytest.mark.parametrize("control_id", ["", "A" * 21, "has|pipe", "has space"])
def test_builder_refuses_unusable_control_ids(control_id):
    with pytest.raises(ValueError, match="control_id"):
        build(control_id=control_id)


def test_builder_refuses_control_characters_in_patient_fields():
    with pytest.raises(ValueError, match="patient.family_name"):
        build(patient=Patient("SYN-1", "PB", "TEST\rMSH", "X"))


def test_new_control_id_is_unique_and_fits_msh10():
    ids = {new_control_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 20 for i in ids)
