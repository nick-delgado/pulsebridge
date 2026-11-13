"""Build an HL7v2 2.5.1 ORU^R01 message for one blood-pressure reading, using hl7apy.

ORU^R01 is HL7v2's "unsolicited observation result": a device or lab pushes results without
being asked. The message has this shape (2.5.1 chapter 7, ORU_R01 structure):

    MSH                   message header: separators, sender, receiver, time, type, control ID
    PID                   patient identification (in the PATIENT_RESULT / PATIENT group)
    OBR                   observation request: what was measured (the blood-pressure panel)
    OBX                   one observation result: systolic
    OBX                   one observation result: diastolic

The FHIR mapping of this message is task T12; this module only produces the message.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Message
from hl7apy.factories import datatype_factory

from .parsing import SEGMENT_TERMINATOR

# The HL7 version we send. Spike S4 found the HL7v2 store accepts 2.3 to 2.8; 2.5.1 is the most
# widely implemented 2.5 release and the version hl7apy and Google's examples both cover well.
HL7_VERSION = "2.5.1"

# MSH-9 (message type): message code ^ trigger event ^ message structure (2.5.1 MSG data type).
MESSAGE_TYPE = ("ORU", "R01", "ORU_R01")

# LOINC codes (coding system "LN" in HL7v2 table 0396).
LOINC_SYSTEM = "LN"
LOINC_BP_PANEL = ("85354-9", "Blood pressure panel with all children optional")
LOINC_SYSTOLIC = ("8480-6", "Systolic blood pressure")
LOINC_DIASTOLIC = ("8462-4", "Diastolic blood pressure")

# UCUM unit for millimetres of mercury. OBX-6 is a CE (2.5.1): identifier ^ text ^ coding system.
UCUM_MMHG = "mm[Hg]"
UCUM_SYSTEM = "UCUM"

# MSH-10 is an ST of at most 20 characters in 2.5.1 (chapter 2.15.9.10).
_CONTROL_ID_MAX = 20
_CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,20}$")
# Caller values (names, identifiers, endpoint names) must be printable ASCII without a backslash.
# - Printable ASCII: control characters have no place in an HL7 text field, and with MSH-18
#   (character set) empty the receiver must assume ASCII.
# - No backslash: "\" is HL7's escape character (MSH-2). hl7apy escapes | ^ & ~ in a value, but it
#   leaves a backslash that already forms an escape sequence alone, so a value such as "A\F\B"
#   would reach the receiver unchanged and be decoded there as "A|B". No real name or identifier
#   needs a backslash, so we refuse it rather than guess what the caller meant.
_CALLER_TEXT_RE = re.compile(r"^[\x20-\x5b\x5d-\x7e]+$")

# MSH-11 processing ID, HL7 table 0103: P = production, T = training, D = debugging.
PROCESSING_IDS = ("P", "T", "D")


@dataclass(frozen=True)
class Patient:
    """Who the reading belongs to. Synthetic in tests; never the author's identity in code."""

    identifier: str  # PID-3.1, e.g. a medical record number
    assigning_authority: str  # PID-3.4, the system that issued the identifier
    family_name: str  # PID-5.1
    given_name: str  # PID-5.2
    identifier_type: str = "MR"  # PID-3.5, HL7 table 0203: MR = medical record number


@dataclass(frozen=True)
class Endpoints:
    """MSH-3 to MSH-6: sending and receiving application and facility.

    These name the software, not a person. They are shown by the store as ``sendFacility``.
    """

    sending_application: str = "CUFFSIM"
    sending_facility: str = "PULSEBRIDGE"
    receiving_application: str = "PHR"
    receiving_facility: str = "PULSEBRIDGE"


@dataclass(frozen=True)
class BloodPressureReading:
    """One cuff reading. ``observed_at`` must carry a time zone (OBX-14 is written with offset)."""

    systolic_mmhg: int
    diastolic_mmhg: int
    observed_at: datetime


@dataclass(frozen=True)
class BuildOptions:
    """Header settings that rarely change."""

    endpoints: Endpoints = field(default_factory=Endpoints)
    # MSH-11 processing ID, one of PROCESSING_IDS (HL7 table 0103).
    processing_id: str = "P"


def new_control_id() -> str:
    """A fresh MSH-10 message control ID: 20 upper-case hex characters (the 2.5.1 maximum)."""
    return secrets.token_hex(_CONTROL_ID_MAX // 2).upper()


def format_dtm(value: datetime) -> str:
    """HL7v2 DTM (2.5.1 section 2.A.22): YYYYMMDDHHMMSS+ZZZZ. Naive datetimes are refused, because
    a time without an offset is ambiguous and T12 must map OBX-14 to FHIR with its zone."""
    offset = value.utcoffset() if value.tzinfo is not None else None
    if offset is None:
        raise ValueError("time must be timezone-aware (HL7 DTM is written with its UTC offset)")
    # DTM's offset is +/-HHMM. Python allows offsets with seconds (some historical zones have
    # them), which strftime would write as +HHMMSS: not a valid DTM.
    if offset.seconds % 60 or offset.microseconds:
        raise ValueError(f"UTC offset {offset} has seconds; HL7 DTM offsets are whole minutes")
    return value.strftime("%Y%m%d%H%M%S%z")


def _check_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not _CALLER_TEXT_RE.match(value):
        raise ValueError(
            f"{name} must be non-empty printable ASCII text without a backslash "
            "(HL7's escape character)"
        )


def _value(datatype: str, text: str):
    """Wrap a caller value in an hl7apy datatype object before assigning it.

    hl7apy treats a plain string assigned to an element as ER7 to be parsed: at a field, "^"
    starts a new component; at a component, "&" starts a new subcomponent. A datatype object is
    taken as a value, and hl7apy escapes every separator in it when it writes ER7 (2.5.1 section
    2.7: "A^B&C" is written "A\\S\\B\\T\\C"). So caller text can never add structure.
    """
    return datatype_factory(
        datatype, text, version=HL7_VERSION, validation_level=VALIDATION_LEVEL.STRICT
    )


def _check_reading(reading: BloodPressureReading) -> None:
    for name, value in (
        ("systolic_mmhg", reading.systolic_mmhg),
        ("diastolic_mmhg", reading.diastolic_mmhg),
    ):
        # bool is a subclass of int; True is not a pressure.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer number of mm[Hg]")
        if not 1 <= value <= 400:
            raise ValueError(f"{name} must be between 1 and 400 mm[Hg], got {value}")
    if reading.systolic_mmhg <= reading.diastolic_mmhg:
        raise ValueError("systolic pressure must be greater than diastolic pressure")


def build_bp_oru_r01(
    reading: BloodPressureReading,
    patient: Patient,
    *,
    control_id: str,
    sent_at: datetime | None = None,
    options: BuildOptions | None = None,
) -> Message:
    """Build the ORU^R01 for one blood-pressure reading and return the hl7apy ``Message``.

    ``control_id`` (MSH-10) identifies this message; the ACK echoes it in MSA-2. Use
    :func:`new_control_id` for a new reading and reuse the same ID when resending the same reading.
    ``sent_at`` is MSH-7 and defaults to now (UTC).

    Raises ``ValueError`` for out-of-range values or unusable identifiers. The result is not
    validated here; call :func:`pulsebridge_hl7v2.validation.validate_oru_r01` before sending.
    """
    _check_reading(reading)
    if not _CONTROL_ID_RE.match(control_id or ""):
        raise ValueError(
            f"control_id must be 1 to {_CONTROL_ID_MAX} characters of letters, digits, "
            "'.', '_' or '-'"
        )
    options = options or BuildOptions()
    for name in (
        "identifier",
        "assigning_authority",
        "identifier_type",
        "family_name",
        "given_name",
    ):
        _check_text(f"patient.{name}", getattr(patient, name))
    for name in (
        "sending_application",
        "sending_facility",
        "receiving_application",
        "receiving_facility",
    ):
        _check_text(f"endpoints.{name}", getattr(options.endpoints, name))
    if options.processing_id not in PROCESSING_IDS:
        raise ValueError(
            f"processing_id must be one of {', '.join(PROCESSING_IDS)} (HL7 table 0103), "
            f"got {options.processing_id!r}"
        )
    sent_at = sent_at or datetime.now(UTC)
    observed = format_dtm(reading.observed_at)

    msg = Message("ORU_R01", version=HL7_VERSION, validation_level=VALIDATION_LEVEL.STRICT)

    # --- MSH: message header. MSH-1 ("|") and MSH-2 ("^~\&") are set by hl7apy's defaults.
    msh = msg.msh
    ep = options.endpoints
    msh.msh_3.hd_1 = _value("IS", ep.sending_application)  # MSH-3 sending application
    msh.msh_4.hd_1 = _value("IS", ep.sending_facility)  # MSH-4 sending facility
    msh.msh_5.hd_1 = _value("IS", ep.receiving_application)  # MSH-5 receiving application
    msh.msh_6.hd_1 = _value("IS", ep.receiving_facility)  # MSH-6 receiving facility
    msh.msh_7 = format_dtm(sent_at)  # MSH-7 date/time of message (required in 2.5.1)
    msh.msh_9 = "^".join(MESSAGE_TYPE)  # MSH-9 ORU^R01^ORU_R01
    msh.msh_10 = control_id  # MSH-10 message control ID (checked above: no separators)
    msh.msh_11.pt_1 = _value("ID", options.processing_id)  # MSH-11.1 processing ID
    msh.msh_12 = HL7_VERSION  # MSH-12 version ID
    # MSH-15/16 (accept/application acknowledgment type) are left empty: original
    # acknowledgment mode. Spike S4 found the store ignores them anyway.

    # Every caller value goes in through _value() (an hl7apy datatype object) at its own
    # component, never as a plain string and never to a whole field, so hl7apy escapes it instead
    # of parsing it. Fixed values written by this module (codes, units) are plain ER7.

    # --- PID: patient identification, inside the PATIENT_RESULT / PATIENT groups.
    patient_result = msg.add_group("ORU_R01_PATIENT_RESULT")
    pid = patient_result.add_group("ORU_R01_PATIENT").pid
    pid.pid_1 = "1"  # PID-1 set ID
    pid.pid_3.cx_1 = _value("ST", patient.identifier)  # PID-3.1 ID number
    pid.pid_3.cx_4.hd_1 = _value("IS", patient.assigning_authority)  # PID-3.4 authority
    pid.pid_3.cx_5 = _value("ID", patient.identifier_type)  # PID-3.5 identifier type code
    pid.pid_5.xpn_1.fn_1 = _value("ST", patient.family_name)  # PID-5.1 family name
    pid.pid_5.xpn_2 = _value("ST", patient.given_name)  # PID-5.2 given name

    # --- OBR: the order/observation the results belong to.
    order = patient_result.add_group("ORU_R01_ORDER_OBSERVATION")
    obr = order.obr
    obr.obr_1 = "1"  # OBR-1 set ID
    # OBR-3 filler order number. 2.5.1 requires OBR-2 or OBR-3; the device "fills" the order, and
    # one reading is one order, so the control ID is a natural unique number.
    obr.obr_3.ei_1 = control_id
    obr.obr_3.ei_2 = _value("IS", ep.sending_application)
    # OBR-4 universal service identifier: what was ordered, the LOINC blood-pressure panel.
    obr.obr_4 = f"{LOINC_BP_PANEL[0]}^{LOINC_BP_PANEL[1]}^{LOINC_SYSTEM}"
    obr.obr_7 = observed  # OBR-7 observation date/time

    # --- OBX: one per measured value.
    for set_id, (code, display), value in (
        (1, LOINC_SYSTOLIC, reading.systolic_mmhg),
        (2, LOINC_DIASTOLIC, reading.diastolic_mmhg),
    ):
        obx = order.add_group("ORU_R01_OBSERVATION").obx
        obx.obx_1 = str(set_id)  # OBX-1 set ID
        obx.obx_2 = "NM"  # OBX-2 value type: NM = numeric, so OBX-5 must be a number
        obx.obx_3 = f"{code}^{display}^{LOINC_SYSTEM}"  # OBX-3 observation identifier (LOINC)
        obx.obx_5 = str(value)  # OBX-5 observation value
        obx.obx_6 = f"{UCUM_MMHG}^{UCUM_MMHG}^{UCUM_SYSTEM}"  # OBX-6 units (UCUM)
        obx.obx_11 = "F"  # OBX-11 result status, table 0085: F = final
        obx.obx_14 = observed  # OBX-14 date/time of the observation
    return msg


def to_er7_bytes(message: Message) -> bytes:
    """Serialise for the wire: ER7 text, a carriage return after every segment, ASCII bytes.

    hl7apy joins segments with "\\r" but leaves the last one unterminated; HL7v2 (2.5.1 section
    2.5.4) terminates every segment, so we add the final carriage return. Nothing here (or later)
    may turn "\\r" into "\\n": the store rejects line feeds (spikes S4, S5).
    """
    return (message.to_er7() + SEGMENT_TERMINATOR).encode("ascii")
