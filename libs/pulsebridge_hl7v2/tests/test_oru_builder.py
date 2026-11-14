"""ORU^R01 builder: AC "passes hl7apy validation" and AC "parses back to the same values"."""

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pulsebridge_hl7v2 import (
    BloodPressureReading,
    BuildOptions,
    Endpoints,
    Patient,
    build_bp_oru_r01,
    decode_escapes,
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


# Every caller-supplied value carries all four separators that could add structure:
# | (field), ^ (component), & (subcomponent), ~ (repetition).
TRICKY_PATIENT = Patient(
    identifier="SYN|1^2&3~4",
    assigning_authority="AU|T^H&O~R",
    family_name="O|BRIEN^X&Y~Z",
    given_name="ANN|A^B&C~MARIE",
    identifier_type="M|R^X&Y~Z",
)
TRICKY_ENDPOINTS = Endpoints(
    sending_application="SND|A^B&C~D",
    sending_facility="SF|A^B&C~D",
    receiving_application="RA|A^B&C~D",
    receiving_facility="RF|A^B&C~D",
)


def build_tricky():
    return to_er7_bytes(
        build_bp_oru_r01(
            BloodPressureReading(120, 80, SENT),
            TRICKY_PATIENT,
            control_id="SYN-1",
            sent_at=SENT,
            options=BuildOptions(endpoints=TRICKY_ENDPOINTS),
        )
    )


def segment(wire: bytes, name: bytes) -> bytes:
    return next(s for s in wire.split(b"\r") if s.startswith(name + b"|"))


def test_separators_in_caller_values_are_escaped_not_injected():
    # 2.5.1 section 2.7: | ^ & ~ inside a value are written \F\ \S\ \T\ \R\.
    wire = build_tricky()
    validate_oru_r01(wire)
    assert segment(wire, b"PID") == (
        b"PID|1||SYN\\F\\1\\S\\2\\T\\3\\R\\4^^^AU\\F\\T\\S\\H\\T\\O\\R\\R"
        b"^M\\F\\R\\S\\X\\T\\Y\\R\\Z||O\\F\\BRIEN\\S\\X\\T\\Y\\R\\Z"
        b"^ANN\\F\\A\\S\\B\\T\\C\\R\\MARIE"
    )
    assert segment(wire, b"MSH").split(b"|")[2:6] == [
        b"SND\\F\\A\\S\\B\\T\\C\\R\\D",
        b"SF\\F\\A\\S\\B\\T\\C\\R\\D",
        b"RA\\F\\A\\S\\B\\T\\C\\R\\D",
        b"RF\\F\\A\\S\\B\\T\\C\\R\\D",
    ]
    assert segment(wire, b"OBR").split(b"|")[3] == b"SYN-1^SND\\F\\A\\S\\B\\T\\C\\R\\D"


def test_escaped_values_parse_back_to_the_original_text():
    parsed = parse_er7(build_tricky())
    chars = parsed.encoding_chars
    pid = parsed.oru_r01_patient_result.oru_r01_patient.pid
    # The structure is unchanged: PID-3 has exactly components 1, 4 and 5 filled, PID-5 has two.
    assert [c.name for c in pid.pid_3.children] == ["CX_1", "CX_4", "CX_5"]
    assert [c.name for c in pid.pid_5.children] == ["XPN_1", "XPN_2"]
    assert decode_escapes(pid.pid_3.cx_1.to_er7(), chars) == TRICKY_PATIENT.identifier
    assert decode_escapes(pid.pid_3.cx_4.to_er7(), chars) == TRICKY_PATIENT.assigning_authority
    assert decode_escapes(pid.pid_3.cx_5.to_er7(), chars) == TRICKY_PATIENT.identifier_type
    assert decode_escapes(pid.pid_5.xpn_1.to_er7(), chars) == TRICKY_PATIENT.family_name
    assert decode_escapes(pid.pid_5.xpn_2.to_er7(), chars) == TRICKY_PATIENT.given_name
    msh = parsed.msh
    assert decode_escapes(msh.msh_4.to_er7(), chars) == TRICKY_ENDPOINTS.sending_facility


CALLER_FIELDS = [
    ("patient.identifier", lambda v: (Patient(v, "PB", "F", "G"), Endpoints())),
    ("patient.assigning_authority", lambda v: (Patient("SYN-1", v, "F", "G"), Endpoints())),
    ("patient.identifier_type", lambda v: (Patient("SYN-1", "PB", "F", "G", v), Endpoints())),
    ("patient.family_name", lambda v: (Patient("SYN-1", "PB", v, "G"), Endpoints())),
    ("patient.given_name", lambda v: (Patient("SYN-1", "PB", "F", v), Endpoints())),
    ("endpoints.sending_application", lambda v: (PATIENT, Endpoints(sending_application=v))),
    ("endpoints.sending_facility", lambda v: (PATIENT, Endpoints(sending_facility=v))),
    ("endpoints.receiving_application", lambda v: (PATIENT, Endpoints(receiving_application=v))),
    ("endpoints.receiving_facility", lambda v: (PATIENT, Endpoints(receiving_facility=v))),
]


@pytest.mark.parametrize(("field", "make"), CALLER_FIELDS, ids=[f for f, _ in CALLER_FIELDS])
@pytest.mark.parametrize("value", ["A\\F\\B", "TRAILING\\", "\\H\\BOLD\\N\\"])
def test_backslash_in_any_caller_value_is_refused(field, make, value):
    # "\" is HL7's escape character: "A\F\B" would reach the receiver unchanged and be read as
    # "A|B". hl7apy does not escape a backslash that already forms an escape sequence.
    patient, endpoints = make(value)
    with pytest.raises(ValueError, match=re.escape(field) + ".*backslash"):
        build_bp_oru_r01(
            BloodPressureReading(120, 80, SENT),
            patient,
            control_id="SYN-1",
            sent_at=SENT,
            options=BuildOptions(endpoints=endpoints),
        )


@pytest.mark.parametrize("processing_id", ["P", "T", "D"])
def test_processing_id_from_table_0103_is_written_to_msh11(processing_id):
    message = build_bp_oru_r01(
        BloodPressureReading(120, 80, SENT),
        PATIENT,
        control_id="SYN-1",
        sent_at=SENT,
        options=BuildOptions(processing_id=processing_id),
    )
    wire = to_er7_bytes(message)
    validate_oru_r01(wire)
    assert segment(wire, b"MSH").split(b"|")[10] == processing_id.encode()


@pytest.mark.parametrize("processing_id", ["P^T", "D&X", "X", "", "p"])
def test_processing_id_outside_table_0103_is_refused(processing_id):
    with pytest.raises(ValueError, match="processing_id must be one of P, T, D"):
        build_bp_oru_r01(
            BloodPressureReading(120, 80, SENT),
            PATIENT,
            control_id="SYN-1",
            sent_at=SENT,
            options=BuildOptions(processing_id=processing_id),
        )


@pytest.mark.parametrize(
    "offset", [timedelta(hours=5, seconds=30), -timedelta(hours=4, minutes=56, seconds=2)]
)
def test_utc_offset_with_seconds_is_refused(offset):
    # Python allows such offsets (some historical zones had them); HL7 DTM offsets are +/-HHMM.
    with pytest.raises(ValueError, match="whole minutes"):
        build(observed=datetime(2026, 11, 13, 20, 53, 29, tzinfo=timezone(offset)))


def test_whole_minute_offsets_are_written_as_hhmm():
    observed = datetime(2026, 11, 13, 20, 53, 29, tzinfo=timezone(timedelta(hours=5, minutes=45)))
    assert b"|20261113205329+0545\r" in to_er7_bytes(build(observed=observed))


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


def test_builder_refuses_control_characters_in_endpoint_names():
    options = BuildOptions(endpoints=Endpoints(sending_facility="PB\rPID|1"))
    with pytest.raises(ValueError, match="endpoints.sending_facility"):
        build_bp_oru_r01(
            BloodPressureReading(120, 80, SENT),
            PATIENT,
            control_id="SYN-1",
            sent_at=SENT,
            options=options,
        )


def test_new_control_id_is_unique_and_fits_msh10():
    ids = {new_control_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 20 for i in ids)
