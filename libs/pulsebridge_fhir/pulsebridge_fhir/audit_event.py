"""AuditEvent for one cuff send attempt (task T31, decisions D12 and D15).

The HL7v2 store keeps neither rejected messages nor the ACK bytes (spikes S4 and S5), so the cuff
simulator (T13) records every attempt, accepted or not, as a FHIR R4 ``AuditEvent``, and the
messages log (T14, T19) reads them back. They are written to a **separate audit FHIR store**
(D15), not to the store that holds the health data: the simulator can write there and cannot read
health data.

An AuditEvent answers "who did what to which thing, when, and did it work": here, the simulator
(agent, source role) exported (``type`` DICOM 110106, ``action`` R) an ORU^R01 message
(``subtype`` R01) about a patient (entity) to the HL7v2 store (agent, destination role), and the
store accepted it, rejected it or gave no readable answer (``outcome``). A Provenance, by
contrast, is about where one resource came from; it is written with the Observation it describes,
and only for a message that was mapped.

Shape (see README.md, "AuditEvent for a cuff send attempt", for every code and why):

- ``type`` DICOM 110106 Export, ``subtype`` v2-0003 R01, ``action`` R.
- ``period.start``: when the message was sent; ``recorded``: when the attempt was recorded.
- ``outcome``: 0 accepted, 4 rejected (the receiver answered with a negative ACK), 8 no readable
  acknowledgment (the delivery path failed, or the reply could not be read). ``outcomeDesc``: a
  fixed sentence plus, on a NACK, the cleaned MSA-3 reason.
- ``agent``: the simulator (source role, ``requestor: true``) and the HL7v2 store (destination
  role, ``requestor: false``), both as logical references by identifier. The patient is not an
  agent: they did not take part in sending.
- ``source.observer``: the simulator, which reports the event.
- ``entity``: the patient (by identifier only, no name: the Patient lives in the other store) and
  the message (by its stored name, when it was stored), with ``detail`` pairs: the format
  version, transport, MSH-10, MSA-1, MSA-3, the raw ACK in base64, the no-acknowledgment reason,
  the HTTP status, the first bytes of an unreadable reply, the malformed sample's name.
- No ``identifier`` (R4 AuditEvent has none): written by a deterministic id,
  ``PUT AuditEvent/<id>``.
"""

import base64
import binascii
import copy
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pulsebridge_fhir import codes
from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.inputs import Acknowledged, NotAcknowledged, SendAttempt
from pulsebridge_fhir.keys import deterministic_uuid, urn_uuid
from pulsebridge_fhir.resources import BuiltResource, Write, fhir_datetime, request_for

# AuditEvent.outcome (R4 audit-event-outcome, required binding):
#   0 Success; 4 Minor failure, "often equivalent to an HTTP 400 response"; 8 Serious failure,
#   "some kind of unexpected error (often equivalent to an HTTP 500 response)"; 12 Major failure,
#   "the system is no longer available for use". 12 is never used: a timeout or a closed
#   connection does not show that the store is down, only that this attempt got no answer.
OUTCOME_SUCCESS = "0"
OUTCOME_MINOR_FAILURE = "4"
OUTCOME_SERIOUS_FAILURE = "8"

# Where a recorded message name came from (SendAttemptRecord.message_name_source).
MESSAGE_NAME_FROM_STORE = "store"
MESSAGE_NAME_DERIVED = "derived"

# The three results the messages log shows.
ACCEPTED = "accepted"
REJECTED = "rejected"
NO_ACKNOWLEDGMENT = "no-acknowledgment"

# The format of the AuditEvent this library writes. A reader branches on it; bump it when the
# meaning of an element or detail changes (not for wording or order, which the parser ignores).
# Carried as a detail entry, not in meta.tag, because a server owns meta and may rewrite it.
FORMAT_VERSION = 1

# AuditEvent.entity.detail.type (a plain string in R4). HL7v2 field names where the value is an
# HL7v2 field, following IHE's audit messages for HL7v2 transactions, which carry MSH-10 this way.
DETAIL_FORMAT_VERSION = "format-version"
DETAIL_TRANSPORT = "transport"
DETAIL_CONTROL_ID = "MSH-10"
DETAIL_ACK_CODE = "MSA-1"
DETAIL_REASON = "MSA-3"
DETAIL_RAW_ACK = "ack-message"  # valueBase64Binary: a readable HL7v2 ACK
DETAIL_NO_ACK_REASON = "no-ack-reason"
DETAIL_HTTP_STATUS = "http-status"
# valueBase64Binary: bytes that were NOT a readable ACK. A distinct name, so they are never taken
# for one.
DETAIL_UNREADABLE_REPLY = "unreadable-reply-bytes"
DETAIL_SAMPLE = "malformed-sample"
# A message name the SENDER computed (T13 over MLLP, from the message bytes), not one the store
# returned. Deliberately not in entity.what: ``what`` is a reference that says "this entity IS
# that stored message", which only the store can confirm. A reader that knows nothing of this
# detail therefore sees no message name at all, rather than an unverified one taken as fact.
DETAIL_DERIVED_MESSAGE_NAME = "derived-message-name"

_NO_ACK_TEXT = {
    "timeout": "the read timed out",
    "connection-closed": "the connection was closed with no reply",
    "api-error": "the API returned an error with no HL7 reply",
    "unreadable-reply": "the reply could not be read as an HL7v2 ACK",
}

# Content type for a FHIR REST write (R4 http.html, "Content Types and encodings").
FHIR_JSON = "application/fhir+json; charset=utf-8"


def send_attempt_key(attempt: SendAttempt) -> str:
    """The AuditEvent's id: UUIDv5 of the sender, the transport, MSH-10, the send time (UTC, ms)
    and, for a malformed sample, the sample's name.

    - The **send time** is in the key because the same control ID can legitimately be sent twice
      (a resend of the same reading reuses MSH-10, see T07): those are two attempts and need two
      AuditEvents.
    - The **sender** and **transport** keep two simulators, or an MLLP and an HTTPS send made at
      the same millisecond, apart.
    - The **result is left out on purpose**: the key names the attempt, and the result is what is
      recorded about it. Recording the same attempt again overwrites it, whatever it says: **last
      write wins**. T13 records each attempt once, after its final outcome, so in practice a
      second write is a retry of the same content. The audit store keeps every version of a
      resource, so an overwrite is still visible in its history.
    - A malformed sample with no MSH-10 uses an empty string for it; that cannot collide with a
      real control ID, which is never empty.
    - The **sample name** (T33) is added for a malformed sample, because samples share control
      IDs (``SYN-BAD-0001`` is in two of them) or have none (every ``unparseable_*``): without it,
      two different samples sent in the same millisecond on one transport (two instances) would
      get one id, and the second write would replace the first. The name rather than a digest
      of the bytes: the library holds no message bytes (on purpose), and the name already says
      which bytes were sent. A built reading has no sample name and a fresh MSH-10 each time, so
      its key, and every id already written for one, is unchanged. The same sample sent twice in
      one millisecond by two instances still shares an id: those are the same bytes to the same
      store, which keeps one message (spike S5), and the history keeps both versions.
    """
    parts = [
        "AuditEvent",
        "cuff-send-attempt",
        attempt.agent,
        attempt.transport,
        attempt.control_id or "",
        attempt.sent_at_utc,
    ]
    if attempt.sample_name is not None:
        parts.append(attempt.sample_name)
    return deterministic_uuid(*parts)


def _result_kind(result: Acknowledged | NotAcknowledged) -> str:
    if isinstance(result, NotAcknowledged):
        return NO_ACKNOWLEDGMENT
    return ACCEPTED if result.accepted else REJECTED


_OUTCOMES = {
    ACCEPTED: OUTCOME_SUCCESS,
    REJECTED: OUTCOME_MINOR_FAILURE,
    NO_ACKNOWLEDGMENT: OUTCOME_SERIOUS_FAILURE,
}


def _outcome_desc(result: Acknowledged | NotAcknowledged) -> str | None:
    """Fixed wording plus, at most, the cleaned MSA-3 reason. Nothing else enters this text."""
    if isinstance(result, NotAcknowledged):
        text = f"No acknowledgment received: {_NO_ACK_TEXT[result.why]}"
        if result.http_status is not None:
            text += f" (HTTP {result.http_status})"
        return text
    if result.accepted:
        return f"Accepted (MSA-1 {result.code}): {result.reason}" if result.reason else None
    text = f"Rejected by the receiver (MSA-1 {result.code})"
    return f"{text}: {result.reason}" if result.reason else text


def _detail(name: str, value: str) -> dict:
    return {"type": name, "valueString": value}


def _bytes_detail(name: str, value: bytes) -> dict:
    # base64Binary: RFC 4648 base64 with padding (R4 datatypes.html#base64Binary).
    return {"type": name, "valueBase64Binary": base64.b64encode(value).decode("ascii")}


def _message_details(attempt: SendAttempt) -> list[dict]:
    details = [
        _detail(DETAIL_FORMAT_VERSION, str(FORMAT_VERSION)),
        _detail(DETAIL_TRANSPORT, attempt.transport),
    ]
    if attempt.control_id is not None:
        details.append(_detail(DETAIL_CONTROL_ID, attempt.control_id))
    result = attempt.result
    if isinstance(result, Acknowledged):
        details.append(_detail(DETAIL_ACK_CODE, result.code))
        if result.reason is not None:
            details.append(_detail(DETAIL_REASON, result.reason))
        details.append(_bytes_detail(DETAIL_RAW_ACK, result.raw_ack))
    else:
        details.append(_detail(DETAIL_NO_ACK_REASON, result.why))
        if result.http_status is not None:
            details.append(_detail(DETAIL_HTTP_STATUS, str(result.http_status)))
        if result.reply_prefix is not None:
            details.append(_bytes_detail(DETAIL_UNREADABLE_REPLY, result.reply_prefix))
    if attempt.sample_name is not None:
        details.append(_detail(DETAIL_SAMPLE, attempt.sample_name))
    if attempt.derived_message_name is not None:
        details.append(_detail(DETAIL_DERIVED_MESSAGE_NAME, attempt.derived_message_name))
    return details


def _system(system: str, value: str) -> dict:
    """A logical reference to a system by identifier, with no resource behind it.

    ``type: Device``: in R4 a Device can be software, which both the simulator and the HL7v2
    store are. It says what kind of thing the identifier names, so a reader need not guess.
    """
    return {"type": "Device", "identifier": {"system": system, "value": value}}


def build_send_attempt_audit_event(attempt: SendAttempt, *, recorded: datetime) -> BuiltResource:
    """The AuditEvent recording one send attempt.

    ``recorded`` is when the attempt is recorded (after the ACK, timeout or error), passed in so
    the builder stays deterministic. It must not be before ``attempt.sent_at`` and must be a whole
    millisecond: pass ``now_ms()`` or ``truncate_to_ms(...)``. Sub-millisecond times are refused,
    not rounded, so the id, ``period`` and ``recorded`` always come from one exact value.
    """
    if not isinstance(attempt, SendAttempt):
        raise FhirInputError(f"audit event: expected a SendAttempt, got {type(attempt).__name__}")
    if (
        not isinstance(recorded, datetime)
        or recorded.tzinfo is None
        or recorded.utcoffset() is None
    ):
        raise FhirInputError("audit event: recorded must be a datetime with a time zone")
    if recorded.microsecond % 1000:
        raise FhirInputError(
            f"audit event: recorded ({recorded}) must be a whole millisecond; "
            "use truncate_to_ms() or now_ms()"
        )
    if recorded < attempt.sent_at:
        raise FhirInputError("audit event: recorded is before sent_at")

    key = send_attempt_key(attempt)
    kind = _result_kind(attempt.result)
    message_entity: dict = {}
    if attempt.message_name is not None:
        # Only a name the store returned (a derived one is a detail, see _message_details).
        # Same identifier as T06's Provenance.entity for this message, so the two can be joined.
        # No Reference.type: an HL7v2 store message is not a FHIR resource, and no R4 type
        # (Binary, DocumentReference) describes it without claiming something false.
        message_entity["what"] = {
            "identifier": {"system": codes.PB_HL7V2_MESSAGE, "value": attempt.message_name}
        }
    message_entity |= {
        # System Object: a message is data held by a system, not a person or an organization.
        "type": dict(codes.ENTITY_TYPE_SYSTEM_OBJECT),
        # Report: an ORU^R01 is an unsolicited observation report.
        "role": dict(codes.ENTITY_ROLE_REPORT),
        "detail": _message_details(attempt),
    }
    resource: dict = {
        "resourceType": "AuditEvent",
        # The id equals the bundle-local uuid, so fullUrl and id agree (as for Provenance).
        "id": key,
        "type": dict(codes.AUDIT_TYPE_EXPORT),
        "subtype": [dict(codes.AUDIT_SUBTYPE_ORU_R01)],
        "action": codes.AUDIT_ACTION_READ,
        # period: when the activity happened (the send, then the wait for an answer).
        "period": {"start": fhir_datetime(attempt.sent_at), "end": fhir_datetime(recorded)},
        # recorded: an instant (seconds and an offset are required, R4 datatypes.html#instant).
        "recorded": fhir_datetime(recorded),
        "outcome": _OUTCOMES[kind],
    }
    desc = _outcome_desc(attempt.result)
    if desc is not None:
        resource["outcomeDesc"] = desc
    resource |= {
        "agent": [
            {
                # The sender. requestor: true, it initiated the activity (R4: "Whether user is
                # initiator").
                "type": {"coding": [dict(codes.AGENT_SOURCE_ROLE)]},
                "who": _system(codes.PB_SERVICE, attempt.agent),
                "requestor": True,
            },
            {
                # The receiver: the HL7v2 store (over MLLP, reached through the MLLP adapter).
                "type": {"coding": [dict(codes.AGENT_DESTINATION_ROLE)]},
                "who": _system(codes.PB_HL7V2_STORE, attempt.destination),
                "requestor": False,
            },
        ],
        # source: who reports the event. The simulator observed the attempt and writes this record.
        "source": {
            "observer": _system(codes.PB_SERVICE, attempt.agent),
            "type": [dict(codes.SOURCE_APPLICATION_SERVER)],
        },
        "entity": [
            {
                # The patient the reading is for: by identifier only (no literal id, no name).
                # The Patient lives in the health-data store, not in this audit store (D15), so a
                # server id could not resolve here anyway.
                "what": {
                    "type": "Patient",
                    "identifier": {"system": codes.PB_PATIENT_ID, "value": attempt.patient_id},
                },
                "type": dict(codes.ENTITY_TYPE_PERSON),
                "role": dict(codes.ENTITY_ROLE_PATIENT),
            },
            message_entity,
        ],
    }
    return BuiltResource(resource=resource, full_url=urn_uuid(key), key=key)


def audit_event_write(built: BuiltResource) -> dict:
    """The single-resource REST write T13 sends: ``{method, url, content_type, body}``.

    ``PUT AuditEvent/<id>`` relative to the **audit** FHIR store's base URL (D15), derived from the
    resource by :func:`request_for` (never supplied by the caller). Only an AuditEvent is accepted
    here, so this cannot be used to write a Patient or Device outside the two bundle functions.
    The body is a deep copy.
    """
    if not isinstance(built, BuiltResource) or not isinstance(built.resource, dict):
        raise FhirInputError(
            f"audit event write: expected a BuiltResource, got {type(built).__name__}"
        )
    if built.resource.get("resourceType") != "AuditEvent":
        raise FhirInputError(
            f"audit event write: only an AuditEvent is written this way, "
            f"got {built.resource.get('resourceType')!r}"
        )
    request = request_for(built.resource, Write.UPDATE)
    return {
        "method": request["method"],
        "url": request["url"],
        "content_type": FHIR_JSON,
        "body": copy.deepcopy(built.resource),
    }


# ----------------------------------------------------------------------------------------------
# Reading back (T14)
# ----------------------------------------------------------------------------------------------
#
# Two separate questions, with two errors:
#
# 1. Is it ours? A fixed set of identifying elements: resourceType, type, subtype, the two agent
#    roles with their urn:pulsebridge: identifier systems, and source.observer. Fails with
#    NotASendAttempt: the caller may skip it silently (another system's AuditEvent).
# 2. What does it say? The values the messages log needs, each validated by building the same
#    input types the builder takes. Display strings, outcomeDesc wording, the order of agents,
#    entities and details, unknown detail entries and unknown elements are ignored, so a later
#    wording or layout change does not make records already stored unreadable. Fails with
#    UnreadableSendAttempt, naming what was wrong: the caller must show or count it, never hide it.


class NotASendAttempt(FhirInputError):
    """The resource is not a send-attempt AuditEvent written by this library."""


class UnreadableSendAttempt(FhirInputError):
    """The resource is one of ours, but a value the messages log needs cannot be read."""


@dataclass(frozen=True)
class SendAttemptRecord:
    """One send attempt as the messages log needs it, read back from an AuditEvent.

    ``result`` is ``accepted``, ``rejected`` or ``no-acknowledgment``. Exactly one of ``ack_code``
    and ``no_ack_reason`` is set. ``attempt`` is the validated input the values describe.
    ``id`` is the stored resource's id (or the derived one when the resource has none).
    ``format_version`` is the version the writer declared; it may be newer than
    ``FORMAT_VERSION`` (the fields this library knows were still read).

    The stored message's name is in one of two fields, never both, so it cannot be taken for
    the other kind: ``message_name`` is a name **the store returned** (HTTPS ingest);
    ``derived_message_name`` is a name **the sender computed** and nobody has confirmed (MLLP).
    Confirm a derived name (for example with a ``get`` of the message) before treating it as
    the stored message; ``message_name_source`` says which kind is present.
    """

    id: str
    sent_at: datetime
    recorded: datetime
    transport: str
    control_id: str | None
    result: str
    outcome: str
    ack_code: str | None
    reason: str | None
    raw_ack: bytes | None
    no_ack_reason: str | None
    http_status: int | None
    message_name: str | None
    sample_name: str | None
    patient_id: str
    agent: str
    destination: str
    attempt: SendAttempt
    reply_prefix: bytes | None = None
    format_version: int = FORMAT_VERSION
    derived_message_name: str | None = None

    @property
    def message_name_source(self) -> str | None:
        """``"store"`` (``message_name``), ``"derived"`` (``derived_message_name``) or ``None``."""
        if self.message_name is not None:
            return MESSAGE_NAME_FROM_STORE
        if self.derived_message_name is not None:
            return MESSAGE_NAME_DERIVED
        return None


# [0-9], not \d (no non-ASCII digits). Up to 9 fraction digits; Python 3.13's fromisoformat
# reads them and keeps microseconds.
_INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_HTTP_STATUS = re.compile(r"[0-9]{3}")
_VERSION = re.compile(r"[1-9][0-9]{0,5}")
_STRING_DETAILS = (
    DETAIL_FORMAT_VERSION,
    DETAIL_TRANSPORT,
    DETAIL_CONTROL_ID,
    DETAIL_ACK_CODE,
    DETAIL_REASON,
    DETAIL_NO_ACK_REASON,
    DETAIL_HTTP_STATUS,
    DETAIL_SAMPLE,
    DETAIL_DERIVED_MESSAGE_NAME,
)
_BYTES_DETAILS = (DETAIL_RAW_ACK, DETAIL_UNREADABLE_REPLY)


def _not_ours(why: str) -> NotASendAttempt:
    return NotASendAttempt(f"not a PulseBridge send-attempt AuditEvent: {why}")


def _unreadable(why: str) -> UnreadableSendAttempt:
    return UnreadableSendAttempt(f"send-attempt AuditEvent cannot be read: {why}")


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _codings(concept: object) -> list:
    """The codings of a CodeableConcept, or a bare Coding as a one-item list."""
    if not isinstance(concept, dict):
        return []
    if "coding" in concept:
        return _list(concept["coding"])
    return [concept]


def _has_code(concept: object, coding: dict) -> bool:
    """Whether ``concept`` carries ``coding``'s system and code. Display is not compared."""
    return any(
        isinstance(c, dict)
        and c.get("system") == coding["system"]
        and c.get("code") == coding["code"]
        for c in _codings(concept)
    )


def _identifier(reference: object) -> dict:
    ident = reference.get("identifier") if isinstance(reference, dict) else None
    return ident if isinstance(ident, dict) else {}


def _agents(resource: dict, role: dict, system: str) -> list[dict]:
    return [
        a
        for a in _list(resource.get("agent"))
        if isinstance(a, dict)
        and _has_code(a.get("type"), role)
        and _identifier(a.get("who")).get("system") == system
    ]


def _check_ours(resource: object) -> dict:
    """Raise NotASendAttempt unless the identifying elements are ours."""
    if not isinstance(resource, dict) or resource.get("resourceType") != "AuditEvent":
        raise _not_ours("resourceType is not AuditEvent")
    if not _has_code(resource.get("type"), codes.AUDIT_TYPE_EXPORT):
        raise _not_ours("type is not DICOM 110106 Export")
    if not any(_has_code(s, codes.AUDIT_SUBTYPE_ORU_R01) for s in _list(resource.get("subtype"))):
        raise _not_ours("subtype has no v2-0003 R01")
    if not _agents(resource, codes.AGENT_SOURCE_ROLE, codes.PB_SERVICE):
        raise _not_ours(f"no source-role agent identified in {codes.PB_SERVICE}")
    if not _agents(resource, codes.AGENT_DESTINATION_ROLE, codes.PB_HL7V2_STORE):
        raise _not_ours(f"no destination-role agent identified in {codes.PB_HL7V2_STORE}")
    source = resource.get("source")
    observer = source.get("observer") if isinstance(source, dict) else None
    if _identifier(observer).get("system") != codes.PB_SERVICE:
        raise _not_ours(f"source.observer is not identified in {codes.PB_SERVICE}")
    return resource


def _instant(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        raise _unreadable(f"{name} is missing or not a FHIR instant")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise _unreadable(f"{name} ({value!r}) is not a real date and time") from exc


def _only(items: list, name: str) -> dict:
    if not items:
        raise _unreadable(f"no {name}")
    if len(items) > 1:
        raise _unreadable(f"{len(items)} {name}s; expected one")
    return items[0]


def _identifier_value(reference: object, system: str, name: str) -> str:
    ident = _identifier(reference)
    if ident.get("system") != system:
        raise _unreadable(f"{name} has no {system} identifier")
    value = ident.get("value")
    if not isinstance(value, str):
        raise _unreadable(f"{name} identifier has no value")
    return value


def _known_details(entity: dict) -> dict[str, object]:
    """The detail entries this library knows, by type. Unknown entries are ignored."""
    out: dict[str, object] = {}
    for item in _list(entity.get("detail")):
        if not isinstance(item, dict):
            continue
        name = item.get("type")
        if name in _STRING_DETAILS:
            key, kind = "valueString", "valueString"
        elif name in _BYTES_DETAILS:
            key, kind = "valueBase64Binary", "valueBase64Binary"
        else:
            continue
        if name in out:
            raise _unreadable(f"detail {name!r} appears twice")
        value = item.get(key)
        if not isinstance(value, str):
            raise _unreadable(f"detail {name!r} has no {kind}")
        if name in _BYTES_DETAILS:
            try:
                value = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise _unreadable(f"detail {name!r} is not valid base64") from exc
        out[name] = value
    return out


def _read(resource: dict) -> SendAttemptRecord:
    entities = [e for e in _list(resource.get("entity")) if isinstance(e, dict)]
    patient = _only(
        [e for e in entities if _has_code(e.get("role"), codes.ENTITY_ROLE_PATIENT)],
        "patient entity",
    )
    message = _only(
        [e for e in entities if _has_code(e.get("role"), codes.ENTITY_ROLE_REPORT)],
        "message entity",
    )
    details = _known_details(message)
    version_text = details.get(DETAIL_FORMAT_VERSION)
    if not isinstance(version_text, str) or not _VERSION.fullmatch(version_text):
        raise _unreadable(f"detail {DETAIL_FORMAT_VERSION!r} is missing or not a positive number")
    version = int(version_text)
    try:
        return _read_values(resource, patient, message, details, version)
    except UnreadableSendAttempt as exc:
        if version > FORMAT_VERSION:
            raise _unreadable(
                f"{exc} (written in format version {version}, newer than this library's "
                f"{FORMAT_VERSION}; update pulsebridge_fhir)"
            ) from exc
        raise


def _read_values(
    resource: dict, patient: dict, message: dict, details: dict, version: int
) -> SendAttemptRecord:
    period = resource.get("period") if isinstance(resource.get("period"), dict) else {}
    sent_at = _instant(period.get("start"), "period.start")
    recorded = _instant(resource.get("recorded"), "recorded")
    sender = _only(_agents(resource, codes.AGENT_SOURCE_ROLE, codes.PB_SERVICE), "source agent")
    receiver = _only(
        _agents(resource, codes.AGENT_DESTINATION_ROLE, codes.PB_HL7V2_STORE), "destination agent"
    )
    try:
        if DETAIL_ACK_CODE in details or DETAIL_RAW_ACK in details:
            if DETAIL_NO_ACK_REASON in details:
                raise _unreadable("both an acknowledgment and a no-acknowledgment reason")
            if DETAIL_RAW_ACK not in details or DETAIL_ACK_CODE not in details:
                raise _unreadable(f"an acknowledgment needs both {DETAIL_ACK_CODE!r} and "
                                  f"{DETAIL_RAW_ACK!r}")  # fmt: skip
            result: Acknowledged | NotAcknowledged = Acknowledged(
                code=details[DETAIL_ACK_CODE],
                raw_ack=details[DETAIL_RAW_ACK],
                reason=details.get(DETAIL_REASON),
            )
        elif DETAIL_NO_ACK_REASON in details:
            status = details.get(DETAIL_HTTP_STATUS)
            if status is not None and not _HTTP_STATUS.fullmatch(status):
                raise _unreadable(f"detail {DETAIL_HTTP_STATUS!r} is not a three-digit number")
            result = NotAcknowledged(
                why=details[DETAIL_NO_ACK_REASON],
                http_status=int(status) if status is not None else None,
                reply_prefix=details.get(DETAIL_UNREADABLE_REPLY),
            )
        else:
            raise _unreadable(
                f"neither {DETAIL_ACK_CODE!r} nor {DETAIL_NO_ACK_REASON!r}: the result is unknown"
            )
        if DETAIL_TRANSPORT not in details:
            raise _unreadable(f"detail {DETAIL_TRANSPORT!r} is missing")
        attempt = SendAttempt(
            sent_at=sent_at,
            transport=details[DETAIL_TRANSPORT],
            control_id=details.get(DETAIL_CONTROL_ID),
            result=result,
            patient_id=_identifier_value(patient.get("what"), codes.PB_PATIENT_ID, "patient"),
            agent=_identifier_value(sender.get("who"), codes.PB_SERVICE, "source agent"),
            destination=_identifier_value(
                receiver.get("who"), codes.PB_HL7V2_STORE, "destination agent"
            ),
            message_name=(
                _identifier_value(message["what"], codes.PB_HL7V2_MESSAGE, "message entity")
                if "what" in message
                else None
            ),
            sample_name=details.get(DETAIL_SAMPLE),
            derived_message_name=details.get(DETAIL_DERIVED_MESSAGE_NAME),
        )
    except UnreadableSendAttempt:
        raise
    except FhirInputError as exc:
        # The values failed the same validation the builder applies.
        raise _unreadable(str(exc)) from exc
    if recorded < sent_at:
        raise _unreadable("recorded is before period.start")
    kind = _result_kind(result)
    if resource.get("outcome") != _OUTCOMES[kind]:
        raise _unreadable(
            f"outcome {resource.get('outcome')!r} does not match the result ({kind}, "
            f"expected {_OUTCOMES[kind]!r})"
        )
    stored_id = resource.get("id")
    return SendAttemptRecord(
        id=stored_id if isinstance(stored_id, str) else send_attempt_key(attempt),
        sent_at=sent_at,
        recorded=recorded,
        transport=attempt.transport,
        control_id=attempt.control_id,
        result=kind,
        outcome=_OUTCOMES[kind],
        ack_code=result.code if isinstance(result, Acknowledged) else None,
        reason=result.reason if isinstance(result, Acknowledged) else None,
        raw_ack=result.raw_ack if isinstance(result, Acknowledged) else None,
        no_ack_reason=result.why if isinstance(result, NotAcknowledged) else None,
        http_status=result.http_status if isinstance(result, NotAcknowledged) else None,
        message_name=attempt.message_name,
        sample_name=attempt.sample_name,
        patient_id=attempt.patient_id,
        agent=attempt.agent,
        destination=attempt.destination,
        attempt=attempt,
        reply_prefix=result.reply_prefix if isinstance(result, NotAcknowledged) else None,
        format_version=version,
        derived_message_name=attempt.derived_message_name,
    )


def is_send_attempt(resource: object) -> bool:
    """Whether ``resource`` is one of ours (the identity check only; values are not read)."""
    try:
        _check_ours(resource)
    except NotASendAttempt:
        return False
    return True


def parse_send_attempt(resource: object) -> SendAttemptRecord:
    """Read a send-attempt AuditEvent back into a :class:`SendAttemptRecord`.

    - ``NotASendAttempt`` (a ``FhirInputError``): not one of ours; safe to skip.
    - ``UnreadableSendAttempt`` (a ``FhirInputError``): ours, but a value cannot be read; the
      message names it. Show it as an error row or count it; do not hide it.

    Strict on the values it returns, which are validated by the same input types the builder
    uses; tolerant of presentation (displays, ``outcomeDesc``, element order, unknown details and
    elements, and anything a server adds such as ``id``, ``meta`` and ``text``). The input is not
    changed.
    """
    return _read(_check_ours(resource))


# ----------------------------------------------------------------------------------------------
# Listing (T14)
# ----------------------------------------------------------------------------------------------

# Cloud Healthcare caps a search page at 1000 entries.
SEARCH_COUNT_MAX = 1000


def _utc_z(value: datetime, name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FhirInputError(f"send attempt search: {name} must be a datetime with a time zone")
    # UTC with Z: no '+' in the query, which a careless URL builder would turn into a space.
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def send_attempt_search_params(
    *, count: int = 50, since: datetime | None = None, before: datetime | None = None
) -> list[tuple[str, str]]:
    """Query parameters to list send-attempt AuditEvents, newest first.

    ``GET AuditEvent?<these>``; pass them to ``urllib.parse.urlencode`` (a list of pairs, since
    ``date`` may appear twice). Standard R4 AuditEvent search parameters only:

    - ``type`` (token, AuditEvent.type) and ``subtype`` (token, AuditEvent.subtype): our events;
    - ``date`` (date, AuditEvent.recorded) with ``ge`` / ``lt``: the optional time range;
    - ``_sort=-date``: newest first; ``_count``: page size.

    Cloud Healthcare's support for each on AuditEvent is unverified until the first real call.
    ``date`` searches ``recorded``, not ``period.start``; both are close and in the same order for
    one simulator, which sends one message at a time.
    """
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= SEARCH_COUNT_MAX:
        raise FhirInputError(f"send attempt search: count must be 1 to {SEARCH_COUNT_MAX}")
    params = [
        ("type", f"{codes.AUDIT_TYPE_EXPORT['system']}|{codes.AUDIT_TYPE_EXPORT['code']}"),
        (
            "subtype",
            f"{codes.AUDIT_SUBTYPE_ORU_R01['system']}|{codes.AUDIT_SUBTYPE_ORU_R01['code']}",
        ),
    ]
    if since is not None:
        params.append(("date", "ge" + _utc_z(since, "since")))
    if before is not None:
        params.append(("date", "lt" + _utc_z(before, "before")))
    if since is not None and before is not None and before <= since:
        raise FhirInputError("send attempt search: before must be after since")
    params += [("_sort", "-date"), ("_count", str(count))]
    return params
