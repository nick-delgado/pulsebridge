"""Validate an ORU^R01 before it is sent, using hl7apy, and report every problem in plain words.

This is the first layer of malformed-message handling (D7, "structurally invalid"): a message
that fails here is never sent by the cuff simulator unless "send malformed" is set.

What hl7apy checks (STRICT level, against its 2.5.1 structure definitions):

- segments appear where the ORU_R01 structure allows them, and required segments are present;
- required fields are present (for example PID-3, OBR-4, OBX-3, OBX-11 in 2.5.1);
- fields hold the right data type, and dates are valid HL7 dates.

What hl7apy 1.3.5 does not check, and this module adds:

- OBX-5 against OBX-2. OBX-5 has the data type "varies": its type is whatever OBX-2 says.
  hl7apy's validator skips "varies" fields (its source says "it should check the real rule"), so
  "HIGH" in an OBX with OBX-2 = NM passes. We check each OBX-5 value with hl7apy's own datatype
  class for the OBX-2 type, so the rules are still hl7apy's.
- A stricter rule for NM (numeric) than hl7apy's. hl7apy reads NM with Python's ``Decimal``,
  which also accepts "NaN", "inf", "1e3", "1_000", surrounding spaces and non-ASCII digits.
  HL7 2.5.1 (chapter 2A, data type NM) allows only an optional sign, ASCII digits and an
  optional decimal point, so every NM value must also match ``NM_PATTERN`` in full.
- That OBX-2 names a data type at all when OBX-5 has a value.
- That the message type is ORU^R01 (checked first, with a clear message).

hl7apy's validator collects every error but raises only the first; the report file is its public
way to read them all, so we give it one in a temporary directory and report every problem at once.
"""

from __future__ import annotations

import os
import re
import tempfile

from hl7apy import exceptions as hl7apy_exceptions
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Field, Message, is_base_datatype
from hl7apy.factories import datatype_factory
from hl7apy.parser import get_message_info
from hl7apy.validation import Validator

from .parsing import HL7ParseError, check_framing_text, decode_payload, parse_er7


class HL7ValidationError(ValueError):
    """The message is not a valid ORU^R01. ``problems`` lists each problem as one sentence."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("HL7v2 validation failed: " + "; ".join(self.problems))


# HL7 2.5.1 NM: "+" or "-" (optional), ASCII digits, an optional decimal point. No exponent, no
# spaces, no digit separators. re.ASCII makes \d mean 0-9 only (not other scripts' digits).
NM_PATTERN = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)", re.ASCII)  # used with fullmatch

_MISSING_CHILD_RE = re.compile(r"^Missing required child (\w+)\.(\w+)$")
_FIELD_NAME_RE = re.compile(r"^([A-Z][A-Z0-9]{2})_(\d+)$")


def _describe_hl7apy_error(text: str, version: str) -> str:
    """Rewrite hl7apy's terse messages ("Missing required child PID.PID_3") in HL7 terms."""
    match = _MISSING_CHILD_RE.match(text)
    if match:
        parent, child = match.groups()
        field_match = _FIELD_NAME_RE.match(child)
        if field_match:
            segment, number = field_match.groups()
            try:
                long_name = Field(child, version=version).long_name.replace("_", " ").lower()
            except hl7apy_exceptions.HL7apyException:
                long_name = "field"
            return f"{segment}-{number} ({long_name}) is required but missing"
        return f"{child} is required in {parent} but missing"
    return text


def _segments(element, name: str):
    """Every segment called ``name`` anywhere under ``element`` (segments sit inside groups)."""
    for child in element.children:
        if child.classname == "Segment":
            if child.name == name:
                yield child
        elif child.classname == "Group":
            yield from _segments(child, name)


def _check_obx_values(message: Message) -> list[str]:
    problems = []
    for position, obx in enumerate(_segments(message, "OBX"), start=1):
        label = f"OBX #{position}"
        value_type = obx.obx_2.to_er7() if obx.obx_2 else ""
        values = [rep.to_er7() for rep in obx.obx_5] if obx.obx_5 else []
        values = [v for v in values if v]
        if not values:
            continue
        if not value_type:
            problems.append(f"{label}: OBX-2 (value type) is required when OBX-5 has a value")
            continue
        if not is_base_datatype(value_type, message.version):
            # Composite OBX-2 types (CE, CWE, SN ...) are not used by this project; hl7apy has
            # no single class to check them with, so they are reported as unsupported.
            problems.append(
                f"{label}: OBX-2 value type {value_type!r} is not a simple HL7 data type this "
                "library can check"
            )
            continue
        for value in values:
            # First hl7apy's own check of the value against the OBX-2 data type...
            try:
                datatype_factory(
                    value_type,
                    value,
                    version=message.version,
                    validation_level=VALIDATION_LEVEL.STRICT,
                )
            except (ValueError, hl7apy_exceptions.HL7apyException):
                problems.append(
                    f"{label}: OBX-5 value {value!r} is not a valid {value_type} "
                    f"(OBX-2 says the value type is {value_type})"
                )
                continue
            # ...then our stricter NM rule for values hl7apy's Decimal-based check let through.
            if value_type == "NM" and not NM_PATTERN.fullmatch(value):
                problems.append(
                    f"{label}: OBX-5 value {value!r} is not a valid NM: HL7 allows only an "
                    "optional sign, ASCII digits and an optional decimal point"
                )
    return problems


def _run_hl7apy_validator(message: Message) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="hl7apy-report-") as tmp:
        report = os.path.join(tmp, "report.txt")
        try:
            Validator.validate(message, report_file=report)
        except hl7apy_exceptions.ValidationError:
            pass  # hl7apy raised only the first error; the report file lists all of them
        except hl7apy_exceptions.HL7apyException as exc:
            # Raised before the report is written (e.g. a reference hl7apy cannot load).
            return [f"hl7apy could not validate the message: {exc}"]
        with open(report, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    return [
        _describe_hl7apy_error(line.removeprefix("Error: "), message.version)
        for line in lines
        if line.startswith("Error: ")
    ]


def validate_oru_r01(payload: bytes | str | Message) -> Message:
    """Check that ``payload`` is a valid HL7v2 ORU^R01 and return it parsed (hl7apy ``Message``).

    Accepts the bytes about to be sent, ER7 text, or an hl7apy ``Message`` (for example straight
    from :func:`pulsebridge_hl7v2.oru.build_bp_oru_r01`). Raises :class:`HL7ValidationError`
    listing every problem found.
    """
    try:
        text = payload.to_er7() if isinstance(payload, Message) else decode_payload(payload)
        check_framing_text(text)
        # hl7apy's own reading of MSH-9: the message structure, e.g. "ORU_R01". Checked before
        # the STRICT parse, which otherwise fails on an empty MSH-9 with "Cannot instantiate an
        # unknown Element".
        _chars, structure, _version = get_message_info(text)
    except (HL7ParseError, hl7apy_exceptions.HL7apyException, ValueError) as exc:
        raise HL7ValidationError([getattr(exc, "reason", str(exc))]) from exc
    if structure != "ORU_R01":
        raise HL7ValidationError(
            [
                "MSH-9 (message type) must be ORU^R01 (structure ORU_R01); got "
                + (repr(structure) if structure else "an empty or incomplete MSH-9")
            ]
        )
    try:
        message = parse_er7(text, strict=True)
    except HL7ParseError as exc:
        raise HL7ValidationError([exc.reason]) from exc

    problems = _run_hl7apy_validator(message)
    problems += _check_obx_values(message)
    if problems:
        raise HL7ValidationError(problems)
    return message
