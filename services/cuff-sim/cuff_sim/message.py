"""What gets sent: a built reading (validated) or a named malformed sample (not validated).

A built reading is an HL7v2 2.5.1 ORU^R01 from T07's builder: MSH (header), PID (patient), OBR
(the blood-pressure panel order) and two OBX (systolic LOINC 8480-6, diastolic 8462-4, UCUM
``mm[Hg]``). Every value the caller supplies is a bounded integer; the patient, the names and the
sending application and facility come from configuration. It is validated with T07's
``validate_oru_r01`` (hl7apy STRICT plus T07's own checks) and is **never sent if that fails**.

A malformed sample is one of T07's synthetic samples, chosen by name from its manifest. Its exact
bytes are sent with no validation: that is the point, to show how the later layers (the store,
the adapter, the mapper) handle bad input (D7).
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from pulsebridge_hl7v2 import (
    BloodPressureReading,
    BuildOptions,
    Endpoints,
    HL7ParseError,
    Patient,
    build_bp_oru_r01,
    frame,
    parse_er7,
    to_er7_bytes,
    validate_oru_r01,
)
from pulsebridge_hl7v2.samples import Sample, all_samples

from cuff_sim.config import ServiceConfig

# T07's builder accepts 1 to 400 mm[Hg] for each value, systolic above diastolic
# (pulsebridge_hl7v2.oru._check_reading). The simulator refuses anything outside these first, so
# the caller gets a precise error and the builder never sees an impossible value. The mapper
# (T12) is narrower (systolic 50-300, diastolic 20-200): a reading between the two is stored with
# AA and then gets the application-level outcome "out-of-range", which is a D7 layer to show.
MIN_MMHG = 1
MAX_MMHG = 400

TRANSPORTS = ("mllp", "https")

# T31 accepts a control ID of 1 to 64 of these characters; anything else read from a sample is
# treated as "no readable control ID".
_CONTROL_ID = re.compile(r"[A-Za-z0-9._-]{1,64}", re.ASCII)


@dataclass(frozen=True)
class Outgoing:
    """One message ready to send."""

    payload: bytes  # the message bytes (what HTTPS ingest carries; inside the MLLP frame)
    wire_bytes: bytes  # what goes on the MLLP socket, framing included
    framing: str | None  # MLLP: "standard" or a sample's broken framing; HTTPS: None
    control_id: str | None  # MSH-10; None only for a sample with no readable MSH-10
    sample_name: str | None = None


def endpoints(config: ServiceConfig) -> BuildOptions:
    # MSH-5/MSH-6 (receiving application/facility) keep T07's defaults (PHR / PULSEBRIDGE).
    return BuildOptions(
        endpoints=Endpoints(
            sending_application=config.sending_application,
            sending_facility=config.sending_facility,
        )
    )


def patient(config: ServiceConfig) -> Patient:
    return Patient(
        identifier=config.patient_id,  # PID-3.1
        assigning_authority=config.patient_assigning_authority,  # PID-3.4
        family_name=config.patient_family_name,  # PID-5.1
        given_name=config.patient_given_name,  # PID-5.2
    )


def build_reading(
    config: ServiceConfig,
    *,
    systolic: int,
    diastolic: int,
    control_id: str,
    observed_at: datetime,
) -> Outgoing:
    """Build and validate the ORU^R01. Raises ``HL7ValidationError`` (nothing to send) if it
    fails validation, and ``ValueError`` if the builder refuses a value.

    ``control_id`` (MSH-10) must be new for every reading: the mapper keys the Observation on it,
    so reusing one would overwrite an earlier reading (T12).
    """
    message = build_bp_oru_r01(
        BloodPressureReading(
            systolic_mmhg=systolic, diastolic_mmhg=diastolic, observed_at=observed_at
        ),
        patient(config),
        control_id=control_id,
        sent_at=observed_at,  # MSH-7: a reading is sent the moment it is taken
        options=endpoints(config),
    )
    # Every segment ends with "\r", on the wire too (HL7v2 2.5.1 section 2.5.4; the store refuses
    # line feeds, spikes S4/S5). to_er7_bytes adds the final one; nothing later converts them.
    payload = to_er7_bytes(message)
    validate_oru_r01(payload)  # the bytes that will be sent, not the in-memory message
    return Outgoing(payload, frame(payload), "standard", control_id)


# ---- Malformed samples ---------------------------------------------------------------------


def malformed_samples() -> tuple[Sample, ...]:
    """Every T07 sample that "send malformed" may send: all classes except ``valid``.

    The valid samples are left out: they are not malformed, and their fixed control IDs would make
    the mapper overwrite the same Observation every time one was sent.
    """
    return tuple(s for s in all_samples() if s.sample_class != "valid")


def find_sample(name: str, transport: str) -> Sample | None:
    """The named malformed sample if it applies to ``transport``, else ``None``.

    Over HTTPS only samples whose manifest lists ``https`` apply: a broken MLLP frame has no
    HTTPS equivalent (spike S5).
    """
    for sample in malformed_samples():
        if sample.name == name:
            return sample if transport in sample.transports else None
    return None


def sample_control_id(payload: bytes) -> str | None:
    """MSH-10 of a sample, read with hl7apy (tolerant parse), or ``None`` if there is none.

    Samples that hl7apy cannot read at all (no MSH, line-feed terminators) have no readable
    control ID, and T31 allows ``None`` for a malformed sample.
    """
    try:
        message = parse_er7(payload)
        value = message.msh.msh_10.to_er7()
    except (HL7ParseError, AttributeError, LookupError, ValueError):
        return None
    return value if _CONTROL_ID.fullmatch(value or "") else None


def from_sample(sample: Sample, transport: str) -> Outgoing:
    """The sample's exact bytes. Over MLLP its own framing is kept (possibly broken).

    The bytes carry T07's synthetic patient (SYN-000123), not the configured one; see
    ``CuffSimulator.prepare`` for the follow-up.
    """
    wire = sample.wire_bytes if transport == "mllp" else sample.payload
    framing = sample.mllp_framing if transport == "mllp" else None
    return Outgoing(
        payload=sample.payload,
        wire_bytes=wire,
        framing=framing,
        control_id=sample_control_id(sample.payload),
        sample_name=sample.name,
    )


# ---- The stored message's name over MLLP (DERIVED) --------------------------------------------


def derived_message_name(hl7v2_store_name: str, payload: bytes) -> str:
    """DERIVED, NOT RETURNED BY THE STORE: the resource name the HL7v2 store should give
    ``payload``.

    Over MLLP the adapter relays only the ACK, which does not carry the stored message's name.
    Spike S5 observed, on ONE message ingested over HTTPS, that the id after ``/messages/`` is the
    URL-safe base64 (with ``=`` padding) of the SHA-256 of the exact message bytes. This applies
    that observation. It is unverified beyond that one message and for the MLLP path; confirm it
    on the first deployment (T25) by listing the store. Use it only for an accepted (AA) send.
    """
    digest = hashlib.sha256(payload).digest()
    return f"{hl7v2_store_name}/messages/{base64.urlsafe_b64encode(digest).decode('ascii')}"
