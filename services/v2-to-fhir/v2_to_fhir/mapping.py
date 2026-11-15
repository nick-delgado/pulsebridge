"""HL7v2 ORU^R01 blood-pressure message -> FHIR R4 transaction Bundle.

This is the v2-to-FHIR mapping PulseBridge is built to show. It is pure: no GCP, no network, no
clock (the caller passes ``recorded``). It reads top to bottom in the order a message is checked.

Input: the raw ER7 bytes of one message stored in the HL7v2 store, and that message's resource
name. ER7 is HL7v2's pipe-delimited text form; parsing it is hl7apy's job (decision D3, through
``pulsebridge_hl7v2.parse_er7``). Spike S5 explains why the raw bytes are read rather than the
store's ``parsedData``.

Output: a :class:`MappingResult`. Either ``accepted`` with a Bundle built by
``pulsebridge_fhir.blood_pressure_transaction`` (T06), or ``error`` with a reason code from
:mod:`v2_to_fhir.reasons`. Bad message content never raises; it becomes an ``error``.

Where each FHIR element comes from::

    HL7v2 field                           FHIR element
    ------------------------------------  ---------------------------------------------------
    MSH-9   message type (ORU^R01)        only ORU^R01 is mapped
    MSH-12  version ID (2.5.1)            only 2.5.1 is mapped
    MSH-3   sending application           Device.deviceName (with MSH-4) -> Observation.device
    MSH-4   sending facility              Device.deviceName, and part of the idempotency key
    MSH-10  message control ID            Observation.identifier (a UUIDv5 over the key) ->
                                          conditional update, so a redelivery rewrites it
    PID-3.1 patient ID                    Patient.identifier (must be the configured patient)
    PID-5   patient name (family^given)   Patient.name, only if the Patient does not exist
    OBX-3   observation identifier        8480-6 -> component[systolic].code,
                                          8462-4 -> component[diastolic].code; the panel code
                                          85354-9 goes in Observation.code
    OBX-2   value type (NM)               must be numeric
    OBX-5   observation value             component.valueQuantity.value
    OBX-6   units (UCUM mm[Hg])           component.valueQuantity.code / system
    OBX-11  result status (F, C, P)       Observation.status (final, corrected, preliminary)
    OBX-14  date/time of the observation  Observation.effectiveDateTime, with its own offset
    (store) message resource name         Provenance.entity[source].what.identifier

OBR (the order) is not mapped: for one cuff reading it repeats what MSH and OBX say. OBR-7 is
deliberately not a fallback for an empty OBX-14 (see :func:`_read_observation_time`).
"""

from __future__ import annotations

import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hl7apy import exceptions as hl7apy_exceptions
from hl7apy.core import Message
from hl7apy.utils import get_datetime_info
from pulsebridge_fhir import (
    BloodPressureReading,
    DeviceInput,
    FhirInputError,
    PatientInput,
    SourceInfo,
    blood_pressure_transaction,
)
from pulsebridge_fhir.codes import BP_DIASTOLIC, BP_SYSTOLIC, UCUM_MM_HG
from pulsebridge_fhir.inputs import DIASTOLIC_RANGE, SYSTOLIC_RANGE
from pulsebridge_hl7v2 import HL7ParseError, decode_escapes, parse_er7
from pulsebridge_hl7v2.validation import NM_PATTERN

from v2_to_fhir.reasons import Outcome, Reason

logger = logging.getLogger(__name__)

# What this mapper reads. HL7 v2.5.1 is what the cuff simulator sends (T07); other versions
# are refused rather than read with field rules we have not checked.
SUPPORTED_VERSION = "2.5.1"
# MSH-9: message code ORU (unsolicited observation result), trigger event R01.
SUPPORTED_MESSAGE = ("ORU", "R01")
# OBX-3.3 "LN" is HL7 table 0396's name for LOINC.
LOINC_CODING_SYSTEM = "LN"
SYSTOLIC = BP_SYSTOLIC["code"]  # LOINC 8480-6 Systolic blood pressure
DIASTOLIC = BP_DIASTOLIC["code"]  # LOINC 8462-4 Diastolic blood pressure
# OBX-6.3 "UCUM": the units are coded in the Unified Code for Units of Measure.
UCUM_CODING_SYSTEM = "UCUM"
# OBX-11 result status (HL7 table 0085) -> FHIR Observation.status (R4 observation-status).
# Modelled on the HL7 v2-to-FHIR implementation guide's mapping; only the statuses a cuff can
# meaningfully send are accepted.
RESULT_STATUS = {"F": "final", "C": "corrected", "P": "preliminary"}
# OBX-14 is a DTM: YYYYMMDDHHMM[SS[.S[S[S[S]]]]][+/-ZZZZ]. We need at least the minute, and ASCII
# digits only (Python's strptime would also take digits from other scripts).
OBX14_PATTERN = re.compile(
    r"(?P<moment>[0-9]{12}(?:[0-9]{2}(?:\.[0-9]{1,4})?)?)(?P<offset>[+-][0-9]{4})?", re.ASCII
)

# Provenance: the source record is the stored HL7v2 message, held by the HL7v2 store.
SOURCE_RECORD_TYPE = "hl7v2-message"
SOURCE_PLATFORM = "HL7V2_STORE"


@dataclass(frozen=True)
class MappingConfig:
    """Deployment settings the mapping needs (from environment variables, never from code).

    - ``patient_id``: the one patient this deployment holds; PID-3.1 must equal it (D11). It is
      the same identifier the setup step wrote the Patient with.
    - ``patient_assigning_authority``: if set, the matching PID-3 repetition must also carry it
      in PID-3.4.1 (CX.4 assigning authority, HD.1 namespace ID). Two senders can use the same
      ID number in different namespaces; this keeps one from writing onto the other's patient.
    """

    patient_id: str
    patient_assigning_authority: str | None = None


@dataclass(frozen=True)
class MappingResult:
    """``outcome`` and ``reason`` become the message's labels; ``bundle`` is set only when
    accepted. ``control_id`` (MSH-10) is kept for log lines when it could be read."""

    outcome: Outcome
    reason: Reason
    bundle: dict | None = None
    control_id: str | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is Outcome.ACCEPTED


class _Rejected(Exception):
    """Internal: stop mapping this message with ``reason``. Never leaves this module."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def map_message(
    raw: bytes, *, message_name: str, config: MappingConfig, recorded: datetime
) -> MappingResult:
    """Map one stored ORU^R01 to an ingest Bundle, or say why not. Never raises on content.

    ``message_name`` is the HL7v2 store's resource name for the message (from the Pub/Sub
    notification); it becomes the Provenance source. ``recorded`` is when PulseBridge wrote the
    record (Provenance.recorded).
    """
    header: _Header | None = None
    try:
        message = _parse(raw)
        header = _read_header(message)
        observations = _segments(message, "OBX")
        if not observations:
            raise _Rejected(Reason.NO_OBSERVATIONS)
        patient = _read_patient(message, config)
        reading = _read_reading(observations, header)
        bundle = _build_bundle(header, patient, reading, message_name, recorded)
    except _Rejected as rejected:
        return MappingResult(
            Outcome.ERROR, rejected.reason, control_id=header.control_id if header else None
        )
    except Exception as exc:  # noqa: BLE001 - D7: content must never crash the handler
        # A bug, or input hl7apy handles in a way we did not foresee. Log where, not what:
        # the exception text could quote the message, which is health data.
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        logger.error(
            "mapping failed unexpectedly: %s at %s:%s",
            type(exc).__name__,
            frame.filename.rsplit("/", 1)[-1],
            frame.lineno,
        )
        return MappingResult(
            Outcome.ERROR, Reason.MAPPING_FAILED, control_id=header.control_id if header else None
        )
    return MappingResult(Outcome.ACCEPTED, Reason.MAPPED, bundle, header.control_id)


# ----------------------------------------------------------------------------------------------
# Step 1. Parse the ER7 bytes with hl7apy.
# ----------------------------------------------------------------------------------------------


def _parse(raw: bytes) -> Message:
    """Tolerant hl7apy parse (it keeps odd content rather than refusing it; we judge it below).

    The HL7v2 store accepts things hl7apy cannot read (spike S5): MSH-12 ``9.9`` or empty makes
    hl7apy raise ``UnsupportedVersion``. That is the store's commit-level ``AA`` followed by our
    application-level ``error`` (D7).
    """
    try:
        return parse_er7(raw)
    except HL7ParseError as exc:
        if isinstance(exc.__cause__, hl7apy_exceptions.UnsupportedVersion):
            raise _Rejected(Reason.UNSUPPORTED_VERSION) from exc
        raise _Rejected(Reason.UNPARSEABLE_BY_MAPPER) from exc
    except Exception as exc:  # noqa: BLE001 - any hl7apy failure on odd bytes is content
        raise _Rejected(Reason.UNPARSEABLE_BY_MAPPER) from exc


# Small readers over hl7apy's tree. They only look things up by HL7 name (``obx_5``,
# ``obx_3_1``); hl7apy did the splitting. Values come back as ER7 text with escape sequences
# still in place (hl7apy does not decode them), trimmed of surrounding spaces.


def _segments(node: object, name: str) -> list:
    """Every segment called ``name``, in message order, whatever group hl7apy put it in."""
    found = []
    for child in node.children:  # type: ignore[attr-defined]
        if child.classname == "Segment":
            if child.name == name:
                found.append(child)
        elif child.classname == "Group":
            found.extend(_segments(child, name))
    return found


def _repetitions(segment: object, field: str) -> list:
    """The repetitions of a field (HL7 ``~``), empty when the field is empty."""
    return [rep for rep in getattr(segment, field) if rep.to_er7().strip()]


def _text(element: object) -> str:
    return element.to_er7().strip()  # type: ignore[attr-defined]


def _component(segment: object, field: str, number: int) -> str:
    """Component ``number`` of the field's first repetition, e.g. ``("obx_3", 1)`` is OBX-3.1."""
    reps = _repetitions(segment, field)
    return _text(getattr(reps[0], f"{field}_{number}")) if reps else ""


# ----------------------------------------------------------------------------------------------
# Step 2. MSH, the message header: is this a message we map, and what identifies it?
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Header:
    sending_application: str  # MSH-3.1 (HD.1 namespace ID)
    sending_facility: str  # MSH-4.1, may be empty
    control_id: str  # MSH-10

    @property
    def source_key(self) -> str:
        """The idempotency key for this reading at its source.

        HL7 makes MSH-10 unique per sending system (MSH-3 at MSH-4), so the key is all three.
        The cuff simulator keeps the control ID when it resends a reading (T07), so a resend,
        or Pub/Sub delivering the same notification twice, gives the same key, the same
        Observation identifier and so a rewrite instead of a second Observation. ``|`` joins
        them because it is the ER7 field separator and cannot occur inside a field value.
        """
        return "|".join((self.sending_application, self.sending_facility, self.control_id))

    @property
    def device_name(self) -> str:
        """Device.deviceName: the sending application, at its facility when MSH-4 says one."""
        if self.sending_facility:
            return f"{self.sending_application} ({self.sending_facility})"
        return self.sending_application


def _read_header(message: Message) -> _Header:
    msh = message.msh
    # MSH-9 message type (MSG): MSH-9.1 message code, MSH-9.2 trigger event. An empty MSH-9, or
    # ADT^A01 over an observation body, is not something this mapper can interpret.
    message_type = (_component(msh, "msh_9", 1), _component(msh, "msh_9", 2))
    if message_type != SUPPORTED_MESSAGE:
        raise _Rejected(Reason.UNSUPPORTED_MESSAGE_TYPE)
    # MSH-12 version ID (VID.1). hl7apy can read 2.3 to 2.8, but group and field rules differ
    # between versions; we map the one version the brief and the cuff use.
    if _component(msh, "msh_12", 1) != SUPPORTED_VERSION:
        raise _Rejected(Reason.UNSUPPORTED_VERSION)
    # MSH-10 message control ID: the sender's unique ID for this message.
    control_id = _text(msh.msh_10)
    if not control_id:
        raise _Rejected(Reason.MISSING_CONTROL_ID)
    # MSH-3 sending application, MSH-4 sending facility (HD.1, the namespace ID, of each).
    application = _component(msh, "msh_3", 1)
    if not application:
        raise _Rejected(Reason.MISSING_SENDING_APPLICATION)
    facility = _component(msh, "msh_4", 1)
    return _Header(application, facility, control_id)


# ----------------------------------------------------------------------------------------------
# Step 3. PID, the patient: is it the patient this deployment holds?
# ----------------------------------------------------------------------------------------------


def _read_patient(message: Message, config: MappingConfig) -> PatientInput:
    """PID-3.1 and PID-5 only (decision D11): the cuff message has no birth date or sex, and
    the ingest Bundle creates the Patient only if it does not exist, so the full record the
    setup step wrote is never touched."""
    pids = _segments(message, "PID")
    if len(pids) > 1:
        raise _Rejected(Reason.MULTIPLE_PATIENTS)
    if not pids:
        raise _Rejected(Reason.MISSING_PATIENT_IDENTIFIER)
    pid = pids[0]
    separators = message.encoding_chars

    # PID-3 patient identifier list (CX, may repeat): CX.1 ID number, CX.4 assigning authority.
    identifiers = [
        (
            decode_escapes(_text(rep.pid_3_1), separators),
            decode_escapes(_text(rep.pid_3_4.hd_1), separators),
        )
        for rep in _repetitions(pid, "pid_3")
    ]
    identifiers = [(number, authority) for number, authority in identifiers if number]
    if not identifiers:
        raise _Rejected(Reason.MISSING_PATIENT_IDENTIFIER)
    wanted_authority = config.patient_assigning_authority
    if not any(
        number == config.patient_id and wanted_authority in (None, authority)
        for number, authority in identifiers
    ):
        # Not our patient. Writing it would either create a second Patient or, worse, file one
        # person's reading under another's record.
        raise _Rejected(Reason.UNKNOWN_PATIENT)

    # PID-5 patient name (XPN), first repetition: PID-5.1.1 family name (FN.1 surname), PID-5.2
    # given name. Either may be empty; an empty part is left out, never an error.
    family = given = ""
    names = _repetitions(pid, "pid_5")
    if names:
        family = decode_escapes(_text(names[0].pid_5_1.fn_1), separators).strip()
        given = decode_escapes(_text(names[0].pid_5_2), separators).strip()
    return PatientInput(
        config.patient_id,
        family=family or None,
        given=(given,) if given else (),
    )


# ----------------------------------------------------------------------------------------------
# Step 4. OBX, the observations: exactly one systolic and one diastolic value.
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Observation:
    value: int | float  # OBX-5
    status: str  # OBX-11, as a FHIR status
    effective: datetime  # OBX-14, timezone-aware


@dataclass(frozen=True)
class _Reading:
    systolic: _Observation
    diastolic: _Observation


def _read_reading(observations: list, header: _Header) -> _Reading:
    by_code: dict[str, _Observation] = {}
    for obx in observations:
        # OBX-3 observation identifier (CE): OBX-3.1 code, OBX-3.3 coding system.
        code = _component(obx, "obx_3", 1)
        system = _component(obx, "obx_3", 3)
        if system != LOINC_CODING_SYSTEM or code not in (SYSTOLIC, DIASTOLIC):
            # An observation we do not understand is refused, not dropped: silently losing part
            # of a clinical message is worse than recording that it could not be mapped.
            raise _Rejected(Reason.UNKNOWN_OBX_3_CODE)
        if code in by_code:
            raise _Rejected(Reason.DUPLICATE_OBX_3_CODE)
        by_code[code] = _read_observation(obx)

    # Order does not matter: the codes say which value is which.
    if SYSTOLIC not in by_code:
        raise _Rejected(Reason.MISSING_SYSTOLIC)
    if DIASTOLIC not in by_code:
        raise _Rejected(Reason.MISSING_DIASTOLIC)
    reading = _Reading(systolic=by_code[SYSTOLIC], diastolic=by_code[DIASTOLIC])

    # One FHIR Observation carries both values, so they must share one status and one time.
    if reading.systolic.status != reading.diastolic.status:
        raise _Rejected(Reason.MIXED_RESULT_STATUS)
    if reading.systolic.effective != reading.diastolic.effective:  # compares instants
        raise _Rejected(Reason.MISMATCHED_OBX_14)
    if not (
        SYSTOLIC_RANGE[0] <= reading.systolic.value <= SYSTOLIC_RANGE[1]
        and DIASTOLIC_RANGE[0] <= reading.diastolic.value <= DIASTOLIC_RANGE[1]
    ):
        # The same plausibility bounds T06 enforces; checked here to give a precise reason.
        raise _Rejected(Reason.OUT_OF_RANGE)
    if reading.systolic.value <= reading.diastolic.value:
        # Systolic (pressure while the heart contracts) is always above diastolic; the other
        # way round means swapped fields or a bad measurement.
        raise _Rejected(Reason.SYSTOLIC_NOT_ABOVE_DIASTOLIC)
    return reading


def _read_observation(obx: object) -> _Observation:
    # OBX-2 value type: NM (numeric) is the only type a pressure can have here.
    if _text(obx.obx_2) != "NM":  # type: ignore[attr-defined]
        raise _Rejected(Reason.WRONG_VALUE_TYPE)

    # OBX-5 observation value: exactly one, matching HL7's NM rule (optional sign, ASCII
    # digits, optional decimal point; T07's NM_PATTERN). "HIGH" or "1e2" is not a number.
    values = _repetitions(obx, "obx_5")
    if not values:
        raise _Rejected(Reason.MISSING_OBX_5)
    if len(values) > 1:
        raise _Rejected(Reason.REPEATED_OBX_5)
    text = _text(values[0])
    if not NM_PATTERN.fullmatch(text):
        raise _Rejected(Reason.NON_NUMERIC_OBX_5)
    number = Decimal(text)
    value: int | float = int(number) if number == number.to_integral_value() else float(number)

    # OBX-6 units (CE): OBX-6.1 must be the UCUM code mm[Hg]; when OBX-6.3 names a coding
    # system it must be UCUM. "mmHg" is the display text, not the UCUM code.
    unit = _component(obx, "obx_6", 1)
    if not unit:
        raise _Rejected(Reason.MISSING_UNIT)
    unit_system = _component(obx, "obx_6", 3)
    if unit != UCUM_MM_HG or unit_system not in ("", UCUM_CODING_SYSTEM):
        raise _Rejected(Reason.WRONG_UNIT)

    # OBX-11 observation result status.
    status = RESULT_STATUS.get(_text(obx.obx_11))  # type: ignore[attr-defined]
    if status is None:
        raise _Rejected(Reason.UNSUPPORTED_RESULT_STATUS)

    return _Observation(value=value, status=status, effective=_read_observation_time(obx))


def _read_observation_time(obx: object) -> datetime:
    """OBX-14 date/time of the observation, with its time zone handled explicitly.

    - **Required.** OBR-7 is not used as a fallback: D7 lists a missing OBX-14 as a malformed
      class, and the cuff always writes OBX-14 (T07), so an empty one means a broken sender.
    - **Must carry a UTC offset** (``-0500``). HL7 says a time without one is in "the local time
      zone of the sender", which the receiver cannot know; guessing UTC would move a reading by
      hours without anyone noticing. The cuff always writes the offset (T07).
    - The offset is kept: ``20261113155329-0500`` becomes ``2026-11-13T15:53:29-05:00``, the
      wall-clock time the person saw, as the same instant.
    """
    # OBX-14 in 2.5.1 is a TS; its first component (TS.1) is the DTM value.
    text = _component(obx, "obx_14", 1)
    if not text:
        raise _Rejected(Reason.MISSING_OBX_14)
    match = OBX14_PATTERN.fullmatch(text)
    if not match:
        raise _Rejected(Reason.INVALID_OBX_14)
    if not match["offset"]:
        raise _Rejected(Reason.MISSING_TIME_ZONE)
    try:
        # hl7apy's own DTM reader: checks the calendar (no 30 February) and splits the offset.
        naive, _format, offset, _precision = get_datetime_info(text)
    except ValueError as exc:
        raise _Rejected(Reason.INVALID_OBX_14) from exc
    if not offset:  # hl7apy refuses offsets beyond -1200 / +1400
        raise _Rejected(Reason.INVALID_OBX_14)
    sign = -1 if offset[0] == "-" else 1
    minutes = sign * (int(offset[1:3]) * 60 + int(offset[3:5]))
    return naive.replace(tzinfo=timezone(timedelta(minutes=minutes)))


# ----------------------------------------------------------------------------------------------
# Step 5. Build the FHIR transaction Bundle with T06's builders.
# ----------------------------------------------------------------------------------------------


def _build_bundle(
    header: _Header,
    patient: PatientInput,
    reading: _Reading,
    message_name: str,
    recorded: datetime,
) -> dict:
    """One ingest Bundle (D11): Patient and Device as conditional creates, the blood-pressure
    Observation as a conditional update on its key, and a Provenance pointing back to the stored
    HL7v2 message. See ``libs/pulsebridge_fhir/README.md`` for the exact JSON."""
    try:
        return blood_pressure_transaction(
            BloodPressureReading(
                effective=reading.systolic.effective,
                systolic=reading.systolic.value,
                diastolic=reading.diastolic.value,
                source_key=header.source_key,
                unit=UCUM_MM_HG,
            ),
            patient=patient,
            device=DeviceInput(header.device_name),
            source=SourceInfo(
                record_type=SOURCE_RECORD_TYPE,
                record_id=message_name,
                platform=SOURCE_PLATFORM,
                # The message does not say how the reading was taken; assert nothing.
                recording_method=None,
            ),
            recorded=recorded,
            status=reading.systolic.status,
        )
    except FhirInputError as exc:
        # T06 checks its inputs again (text rules, ranges). Reaching this means a value passed
        # our checks but not theirs; the error names a field but may quote the value, so it is
        # not logged.
        raise _Rejected(Reason.FHIR_INPUT_REJECTED) from exc
