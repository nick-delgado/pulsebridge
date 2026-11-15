"""Every application-level outcome reason, in one place, with the sentence the dashboard shows.

The mapper records two labels on each stored HL7v2 message (decision D7, spike S5):

- ``application_outcome``: ``accepted`` or ``error``;
- ``reason``: one of the codes below.

A code is a label value, so it keeps to the documented label limits: lowercase letters, digits
and ``-``, at most 63 bytes (``tests/test_reasons_and_config.py`` checks every one). The
human-readable sentence is not a label; the dashboard looks it up here (via ``fhir-proxy``).
"""

from __future__ import annotations

from enum import StrEnum

from pulsebridge_fhir.inputs import DIASTOLIC_RANGE, SYSTOLIC_RANGE


class Outcome(StrEnum):
    """The ``application_outcome`` label: did the mapper turn the message into FHIR?"""

    ACCEPTED = "accepted"
    ERROR = "error"


class Reason(StrEnum):
    # --- accepted ---------------------------------------------------------------------------
    MAPPED = "mapped"
    # --- the message as a whole ---------------------------------------------------------------
    UNPARSEABLE_BY_MAPPER = "unparseable-by-mapper"
    UNSUPPORTED_VERSION = "unsupported-version"
    UNSUPPORTED_MESSAGE_TYPE = "unsupported-message-type"
    MISSING_CONTROL_ID = "missing-control-id"
    CONTROL_ID_TOO_LONG = "control-id-too-long"
    MISSING_SENDING_APPLICATION = "missing-sending-application"
    INVALID_SENDING_APPLICATION = "invalid-sending-application"
    NO_OBSERVATIONS = "no-observations"
    # --- PID: the patient ---------------------------------------------------------------------
    MULTIPLE_PATIENTS = "multiple-patients"
    MISSING_PATIENT_IDENTIFIER = "missing-patient-identifier"
    UNKNOWN_PATIENT = "unknown-patient"
    INVALID_PATIENT_NAME = "invalid-patient-name"
    # --- OBR: the order -----------------------------------------------------------------------
    MISSING_OBR = "missing-obr"
    MULTIPLE_ORDERS = "multiple-orders"
    UNKNOWN_OBR_4_CODE = "unknown-obr-4-code"
    # --- OBX: one observation -----------------------------------------------------------------
    UNKNOWN_OBX_3_CODE = "unknown-obx-3-code"
    DUPLICATE_OBX_3_CODE = "duplicate-obx-3-code"
    MISSING_SYSTOLIC = "missing-systolic"
    MISSING_DIASTOLIC = "missing-diastolic"
    WRONG_VALUE_TYPE = "wrong-value-type"
    MISSING_OBX_5 = "missing-obx-5"
    REPEATED_OBX_5 = "repeated-obx-5"
    NON_NUMERIC_OBX_5 = "non-numeric-obx-5"
    MISSING_UNIT = "missing-unit"
    WRONG_UNIT = "wrong-unit"
    UNSUPPORTED_RESULT_STATUS = "unsupported-result-status"
    MIXED_RESULT_STATUS = "mixed-result-status"
    MISSING_OBX_14 = "missing-obx-14"
    REPEATED_OBX_14 = "repeated-obx-14"
    INVALID_OBX_14 = "invalid-obx-14"
    MISSING_TIME_ZONE = "missing-time-zone"
    MISMATCHED_OBX_14 = "mismatched-obx-14"
    # --- the reading as a whole ---------------------------------------------------------------
    OUT_OF_RANGE = "out-of-range"
    SYSTOLIC_NOT_ABOVE_DIASTOLIC = "systolic-not-above-diastolic"
    FHIR_INPUT_REJECTED = "fhir-input-rejected"
    MAPPING_FAILED = "mapping-failed"
    # --- after mapping (set by the handler) ---------------------------------------------------
    FHIR_REJECTED = "fhir-rejected"


SENTENCES: dict[Reason, str] = {
    Reason.MAPPED: "The reading was mapped to a FHIR Observation and written to the FHIR store.",
    Reason.UNPARSEABLE_BY_MAPPER: (
        "The mapper could not read the stored bytes as an HL7v2 message, although the HL7v2 "
        "store accepted them."
    ),
    Reason.UNSUPPORTED_VERSION: (
        "MSH-12 (version ID) is not 2.5.1, the only HL7v2 version this mapper reads."
    ),
    Reason.UNSUPPORTED_MESSAGE_TYPE: (
        "MSH-9 (message type) is not ORU^R01, the observation result message this mapper reads."
    ),
    Reason.MISSING_CONTROL_ID: (
        "MSH-10 (message control ID) is empty, so the reading has no key to be stored under."
    ),
    Reason.CONTROL_ID_TOO_LONG: "MSH-10 (message control ID) is longer than 199 characters.",
    Reason.MISSING_SENDING_APPLICATION: (
        "MSH-3 (sending application) is empty, so the measuring device cannot be named."
    ),
    Reason.INVALID_SENDING_APPLICATION: (
        "MSH-3 or MSH-4 (sending application or facility) contains control characters."
    ),
    Reason.NO_OBSERVATIONS: "The message has no OBX (observation) segments: there is no reading.",
    Reason.MULTIPLE_PATIENTS: (
        "The message has more than one PID segment; one reading is about one patient."
    ),
    Reason.MISSING_PATIENT_IDENTIFIER: "There is no PID segment, or PID-3.1 (patient ID) is empty.",
    Reason.UNKNOWN_PATIENT: (
        "No PID-3 identifier matches the patient this deployment is configured for."
    ),
    Reason.INVALID_PATIENT_NAME: "PID-5 (patient name) contains control characters.",
    Reason.MISSING_OBR: (
        "The OBX segments are not under an OBR (observation request) segment that opens the order."
    ),
    Reason.MULTIPLE_ORDERS: (
        "The message has more than one order (OBR); one reading is one blood-pressure panel."
    ),
    Reason.UNKNOWN_OBR_4_CODE: (
        "OBR-4 (universal service identifier) is not the LOINC blood-pressure panel 85354-9."
    ),
    Reason.UNKNOWN_OBX_3_CODE: (
        "An OBX-3 (observation identifier) is not LOINC 8480-6 (systolic) or 8462-4 "
        "(diastolic) with coding system LN."
    ),
    Reason.DUPLICATE_OBX_3_CODE: "Two OBX segments carry the same OBX-3 code.",
    Reason.MISSING_SYSTOLIC: "There is no OBX with LOINC 8480-6 (systolic blood pressure).",
    Reason.MISSING_DIASTOLIC: "There is no OBX with LOINC 8462-4 (diastolic blood pressure).",
    Reason.WRONG_VALUE_TYPE: "An OBX-2 (value type) is not NM (numeric).",
    Reason.MISSING_OBX_5: "An OBX-5 (observation value) is empty.",
    Reason.REPEATED_OBX_5: "An OBX-5 (observation value) has more than one repetition.",
    Reason.NON_NUMERIC_OBX_5: "An OBX-5 (observation value) is not a number.",
    Reason.MISSING_UNIT: "An OBX-6 (units) is empty.",
    Reason.WRONG_UNIT: "An OBX-6 (units) is not the UCUM code mm[Hg].",
    Reason.UNSUPPORTED_RESULT_STATUS: (
        "An OBX-11 (result status) is not F (final) or P (preliminary); corrections and "
        "deletions are not supported."
    ),
    Reason.MIXED_RESULT_STATUS: "The two OBX segments have different OBX-11 result statuses.",
    Reason.MISSING_OBX_14: "An OBX-14 (date/time of the observation) is empty.",
    Reason.REPEATED_OBX_14: "An OBX-14 (date/time of the observation) has more than one value.",
    Reason.INVALID_OBX_14: (
        "An OBX-14 (date/time of the observation) is not an HL7 date/time to at least the minute."
    ),
    Reason.MISSING_TIME_ZONE: (
        "An OBX-14 (date/time of the observation) has no UTC offset, so the moment is ambiguous."
    ),
    Reason.MISMATCHED_OBX_14: "The systolic and diastolic OBX-14 times are different moments.",
    Reason.OUT_OF_RANGE: (
        "A value is outside the plausible range (systolic {} to {}, diastolic {} to {} mm[Hg])."
    ).format(*SYSTOLIC_RANGE, *DIASTOLIC_RANGE),
    Reason.SYSTOLIC_NOT_ABOVE_DIASTOLIC: "The systolic value is not greater than the diastolic.",
    Reason.FHIR_INPUT_REJECTED: (
        "The FHIR builders refused a value read from the message; see the service log."
    ),
    Reason.MAPPING_FAILED: (
        "The mapper hit an unexpected problem with this message; see the service log."
    ),
    Reason.FHIR_REJECTED: (
        "The FHIR store refused the mapped Bundle (HTTP 4xx); see the service log."
    ),
}


def sentence(reason: Reason | str) -> str:
    """The dashboard sentence for a reason code. Raises ``KeyError`` for an unknown code."""
    return SENTENCES[Reason(reason)]
