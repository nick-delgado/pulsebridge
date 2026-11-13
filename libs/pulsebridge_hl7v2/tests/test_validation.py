"""Validation: AC "missing required field or wrong OBX value type fails with a clear error"."""

from datetime import UTC, datetime

import pytest
from pulsebridge_hl7v2 import (
    BloodPressureReading,
    HL7ValidationError,
    Patient,
    build_bp_oru_r01,
    to_er7_bytes,
    validate_oru_r01,
)

T = datetime(2026, 11, 13, 20, 53, 29, tzinfo=UTC)
VALID = to_er7_bytes(
    build_bp_oru_r01(
        BloodPressureReading(120, 80, T),
        Patient("SYN-000123", "PULSEBRIDGE", "TESTPATIENT", "SYNTHIA"),
        control_id="SYN-V-1",
        sent_at=T,
    )
)


def problems_for(payload: bytes) -> tuple[str, ...]:
    with pytest.raises(HL7ValidationError) as caught:
        validate_oru_r01(payload)
    return caught.value.problems


def replace_once(old: bytes, new: bytes) -> bytes:
    assert VALID.count(old) == 1, old
    return VALID.replace(old, new)


def test_valid_message_passes_and_accepts_bytes_text_or_message():
    validate_oru_r01(VALID)
    validate_oru_r01(VALID.decode())
    validate_oru_r01(validate_oru_r01(VALID))


def test_missing_required_field_names_the_field():
    assert problems_for(replace_once(b"SYN-000123^^^PULSEBRIDGE^MR", b"")) == (
        "PID-3 (patient identifier list) is required but missing",
    )


def test_missing_observation_result_status_names_obx11():
    payload = VALID.replace(b"|F|||", b"||||", 1)
    assert problems_for(payload) == ("OBX-11 (observation result status) is required but missing",)


def test_wrong_obx_value_type_is_reported_even_though_hl7apy_skips_varies_fields():
    problems = problems_for(replace_once(b"^LN||120|", b"^LN||HIGH|"))
    assert problems == (
        "OBX #1: OBX-5 value 'HIGH' is not a valid NM (OBX-2 says the value type is NM)",
    )


def test_obx5_without_value_type_is_reported():
    problems = problems_for(replace_once(b"OBX|2|NM|", b"OBX|2||"))
    assert problems == ("OBX #2: OBX-2 (value type) is required when OBX-5 has a value",)


def test_every_problem_is_reported_not_just_the_first():
    payload = replace_once(b"SYN-000123^^^PULSEBRIDGE^MR", b"")
    payload = payload.replace(b"^LN||80|", b"^LN||LOW|")
    problems = problems_for(payload)
    assert len(problems) == 2
    assert "PID-3" in problems[0]
    assert "'LOW'" in problems[1]


def test_every_hl7apy_error_is_reported_not_just_its_first():
    # hl7apy's Validator raises only its first error; the report file has them all.
    payload = replace_once(b"SYN-000123^^^PULSEBRIDGE^MR", b"")
    payload = payload.replace(b"|F|||", b"||||")  # OBX-11 missing in both OBX
    problems = problems_for(payload)
    assert problems == (
        "PID-3 (patient identifier list) is required but missing",
        "OBX-11 (observation result status) is required but missing",
        "OBX-11 (observation result status) is required but missing",
    )


def test_invalid_date_in_obx14_is_a_validation_error():
    payload = VALID.replace(b"|F|||20261113205329+0000", b"|F|||2026-11-13 20:53", 1)
    (problem,) = problems_for(payload)
    assert "does not match its HL7 data type" in problem
    assert "2026-11-13 20:53" in problem


def test_other_message_type_is_rejected_with_clear_reason():
    (problem,) = problems_for(replace_once(b"ORU^R01^ORU_R01", b"ADT^A01^ADT_A01"))
    assert problem.startswith("MSH-9 (message type) must be ORU^R01")


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (VALID.replace(b"\r", b"\n"), "line feeds"),
        (VALID.replace(b"\r", b"\r\n"), "line feeds"),
        (VALID.split(b"\r", 1)[1], "does not start with an MSH segment"),
        (b"", "empty"),
    ],
)
def test_unparseable_input_gives_a_clear_reason_not_an_hl7apy_traceback(payload, fragment):
    (problem,) = problems_for(payload)
    assert fragment in problem


@pytest.mark.parametrize(
    "value",
    ["NaN", "inf", "1e3", "1_000", "120 ", " 120", "١٢٠", "0x78", "--1"],
    ids=[
        "NaN",
        "inf",
        "exponent",
        "underscore",
        "trailing-space",
        "leading-space",
        "arabic-indic-digits",
        "hex",
        "double-sign",
    ],
)
def test_nm_values_python_accepts_but_hl7_does_not_are_refused(value):
    payload = replace_once(b"^LN||120|", b"^LN||" + value.encode("utf-8") + b"|")
    (problem,) = problems_for(payload)
    assert problem.startswith(f"OBX #1: OBX-5 value {value!r} is not a valid NM")


@pytest.mark.parametrize("value", ["120", "+120", "-3", "120.5", "120.", ".5", "007"])
def test_nm_values_hl7_allows_pass(value):
    validate_oru_r01(replace_once(b"^LN||120|", b"^LN||" + value.encode() + b"|"))


def test_every_obx5_repetition_is_checked_not_only_the_first():
    (problem,) = problems_for(replace_once(b"^LN||120|", b"^LN||120~HIGH|"))
    assert "'HIGH'" in problem


def test_composite_obx2_value_type_is_reported_as_unsupported():
    payload = replace_once(b"OBX|1|NM|", b"OBX|1|CE|")
    (problem,) = problems_for(payload)
    assert problem == (
        "OBX #1: OBX-2 value type 'CE' is not a simple HL7 data type this library can check"
    )


def test_non_utf8_bytes_raise_validation_error_not_parse_error():
    (problem,) = problems_for(VALID.replace(b"TESTPATIENT", b"TEST\xffPATIENT"))
    assert "not valid UTF-8" in problem


def test_hl7apy_failing_inside_its_validator_becomes_a_problem(monkeypatch):
    # hl7apy can raise something other than ValidationError before writing its report (for
    # example when it cannot load a structure). Simulate that to check our handling of it.
    import pulsebridge_hl7v2.validation as validation
    from hl7apy.exceptions import ChildNotFound

    def failing_validate(*_args, **_kwargs):
        raise ChildNotFound("XYZ")

    monkeypatch.setattr(validation.Validator, "validate", staticmethod(failing_validate))
    (problem,) = problems_for(VALID)
    assert problem.startswith("hl7apy could not validate the message:")
    assert "XYZ" in problem
