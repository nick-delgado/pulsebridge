"""AuditEvent for one cuff send attempt (task T31, decision D12).

The HL7v2 store keeps neither rejected messages nor the ACK bytes (spikes S4 and S5), so the cuff
simulator (T13) records every attempt, accepted or not, as a FHIR R4 ``AuditEvent`` in the FHIR
store, and the messages log (T14, T19) reads them back.

An AuditEvent answers "who did what to which thing, when, and did it work": here, the simulator
(agent, source role) exported (``type`` DICOM 110106, ``action`` R) an ORU^R01 message
(``subtype`` R01) about a patient (entity) to the HL7v2 store (agent, destination role), and the
store accepted it, rejected it or never answered (``outcome``). A Provenance, by contrast, is
about where one resource came from; it is written with the Observation it describes, and only for
a message that was mapped.

Shape (see README.md, "AuditEvent for a cuff send attempt", for every code and why):

- ``type`` DICOM 110106 Export, ``subtype`` v2-0003 R01, ``action`` R.
- ``period.start``: when the message was sent; ``recorded``: when the attempt was recorded.
- ``outcome``: 0 accepted, 4 rejected (the receiver answered with a negative ACK), 8 no
  acknowledgment (the delivery path failed). ``outcomeDesc``: a fixed sentence plus, on a NACK,
  the cleaned MSA-3 reason.
- ``agent``: the simulator (source role, ``requestor: true``) and the HL7v2 store (destination
  role, ``requestor: false``), both as logical references by identifier. The patient is not an
  agent: they did not take part in sending.
- ``source.observer``: the simulator, which reports the event.
- ``entity``: the patient (by identifier only, no name) and the message (by its stored name, when
  it was stored), with ``detail`` pairs: transport, MSH-10, MSA-1, MSA-3, the raw ACK in base64,
  the no-acknowledgment reason, the HTTP status, the malformed sample's name.
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

# The three results the messages log shows.
ACCEPTED = "accepted"
REJECTED = "rejected"
NO_ACKNOWLEDGMENT = "no-acknowledgment"

# AuditEvent.entity.detail.type (a plain string in R4). HL7v2 field names where the value is an
# HL7v2 field, following IHE's audit messages for HL7v2 transactions, which carry MSH-10 this way.
DETAIL_TRANSPORT = "transport"
DETAIL_CONTROL_ID = "MSH-10"
DETAIL_ACK_CODE = "MSA-1"
DETAIL_REASON = "MSA-3"
DETAIL_RAW_ACK = "ack-message"  # valueBase64Binary
DETAIL_NO_ACK_REASON = "no-ack-reason"
DETAIL_HTTP_STATUS = "http-status"
DETAIL_SAMPLE = "malformed-sample"

_NO_ACK_TEXT = {
    "timeout": "the read timed out",
    "connection-closed": "the connection was closed with no reply",
    "api-error": "the API returned an error with no HL7 reply",
}

# Content type for a FHIR REST write (R4 http.html, "Content Types and encodings").
FHIR_JSON = "application/fhir+json; charset=utf-8"


def send_attempt_key(attempt: SendAttempt) -> str:
    """The AuditEvent's id: UUIDv5 of the sender, the transport, MSH-10 and the send time (UTC, ms).

    - The **send time** is in the key because the same control ID can legitimately be sent twice
      (a resend of the same reading reuses MSH-10, see T07): those are two attempts and need two
      AuditEvents.
    - The **sender** and **transport** keep two simulators, or an MLLP and an HTTPS send made at
      the same millisecond, apart.
    - The **result is not** in the key: the key names the attempt, and the result is what is
      recorded about it, so re-recording one attempt (for example a retried write) replaces the
      same AuditEvent rather than adding a second one.
    - A malformed sample with no MSH-10 uses an empty string for it; that cannot collide with a
      real control ID, which is never empty.
    """
    return deterministic_uuid(
        "AuditEvent",
        "cuff-send-attempt",
        attempt.agent,
        attempt.transport,
        attempt.control_id or "",
        attempt.sent_at_utc,
    )


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


def _message_details(attempt: SendAttempt) -> list[dict]:
    details = [_detail(DETAIL_TRANSPORT, attempt.transport)]
    if attempt.control_id is not None:
        details.append(_detail(DETAIL_CONTROL_ID, attempt.control_id))
    result = attempt.result
    if isinstance(result, Acknowledged):
        details.append(_detail(DETAIL_ACK_CODE, result.code))
        if result.reason is not None:
            details.append(_detail(DETAIL_REASON, result.reason))
        # base64Binary: RFC 4648 base64 with padding (R4 datatypes.html#base64Binary).
        details.append(
            {
                "type": DETAIL_RAW_ACK,
                "valueBase64Binary": base64.b64encode(result.raw_ack).decode("ascii"),
            }
        )
    else:
        details.append(_detail(DETAIL_NO_ACK_REASON, result.why))
        if result.http_status is not None:
            details.append(_detail(DETAIL_HTTP_STATUS, str(result.http_status)))
    if attempt.sample_name is not None:
        details.append(_detail(DETAIL_SAMPLE, attempt.sample_name))
    return details


def _service(name: str) -> dict:
    """A logical reference to a PulseBridge service: an identifier, no resource behind it."""
    return {"identifier": {"system": codes.PB_SERVICE, "value": name}}


def build_send_attempt_audit_event(attempt: SendAttempt, *, recorded: datetime) -> BuiltResource:
    """The AuditEvent recording one send attempt.

    ``recorded`` is when the attempt is recorded (after the ACK, timeout or error), passed in so
    the builder stays deterministic. It must not be before ``attempt.sent_at``; whole milliseconds.
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
        raise FhirInputError(f"audit event: recorded ({recorded}) must be a whole millisecond")
    if recorded < attempt.sent_at:
        raise FhirInputError("audit event: recorded is before sent_at")

    key = send_attempt_key(attempt)
    kind = _result_kind(attempt.result)
    message_entity: dict = {}
    if attempt.message_name is not None:
        # Same identifier as T06's Provenance.entity for this message, so the two can be joined.
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
                "who": _service(attempt.agent),
                "requestor": True,
            },
            {
                # The receiver: the HL7v2 store (over MLLP, reached through the MLLP adapter).
                "type": {"coding": [dict(codes.AGENT_DESTINATION_ROLE)]},
                "who": {
                    "identifier": {"system": codes.PB_HL7V2_STORE, "value": attempt.destination}
                },
                "requestor": False,
            },
        ],
        # source: who reports the event. The simulator observed the attempt and writes this record.
        "source": {
            "observer": _service(attempt.agent),
            "type": [dict(codes.SOURCE_APPLICATION_SERVER)],
        },
        "entity": [
            {
                # The patient the reading is for: by identifier only (no literal id, no name).
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

    ``PUT AuditEvent/<id>`` relative to the FHIR base URL, derived from the resource by
    :func:`request_for` (never supplied by the caller). Only an AuditEvent is accepted here, so
    this cannot be used to write a Patient or Device outside the two bundle functions. The body is
    a deep copy.
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


@dataclass(frozen=True)
class SendAttemptRecord:
    """One send attempt as the messages log needs it, read back from an AuditEvent.

    ``result`` is ``accepted``, ``rejected`` or ``no-acknowledgment``. Exactly one of ``ack_code``
    and ``no_ack_reason`` is set. ``attempt`` is the validated input, equal to what was recorded.
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


# Elements a server adds to a resource it stores (R4 resource.html, domainresource.html).
_SERVER_ELEMENTS = ("id", "meta", "text")
# [0-9], not \d (no non-ASCII digits). Up to 6 fraction digits, as Python's datetime holds.
_INSTANT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$"
)
_HTTP_STATUS = re.compile(r"^[0-9]{3}$")


def _not_ours(why: str) -> FhirInputError:
    return FhirInputError(f"not a PulseBridge send-attempt AuditEvent: {why}")


def _instant(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not _INSTANT.match(value):
        raise _not_ours(f"{name} is not a FHIR instant")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise _not_ours(f"{name} is not a real date and time") from exc


def _first_coding(concept: object) -> object:
    """The first Coding of a CodeableConcept, or None."""
    if not isinstance(concept, dict) or not isinstance(concept.get("coding"), list):
        return None
    return concept["coding"][0] if concept["coding"] else None


def _codings_match(node: object, coding: dict) -> bool:
    return (
        isinstance(node, dict)
        and node.get("system") == coding["system"]
        and node.get("code") == coding["code"]
    )


def _one(items: object, test, name: str) -> dict:
    found = [i for i in items if test(i)] if isinstance(items, list) else []
    if len(found) != 1:
        raise _not_ours(f"expected one {name}, found {len(found)}")
    return found[0]


def _identifier_value(reference: object, system: str, name: str) -> str:
    ident = reference.get("identifier") if isinstance(reference, dict) else None
    if not isinstance(ident, dict) or ident.get("system") != system:
        raise _not_ours(f"{name} has no {system} identifier")
    value = ident.get("value")
    if not isinstance(value, str):
        raise _not_ours(f"{name} identifier has no value")
    return value


def _details(entity: dict) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in entity.get("detail", []) if isinstance(entity.get("detail"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise _not_ours("entity.detail without a type")
        if item["type"] in out:
            raise _not_ours(f"entity.detail {item['type']!r} appears twice")
        values = [k for k in item if k.startswith("value")]
        if len(values) != 1:
            raise _not_ours(f"entity.detail {item['type']!r} needs exactly one value")
        out[item["type"]] = item[values[0]]
    return out


def _string(details: dict, name: str, required: bool = False) -> str | None:
    value = details.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _not_ours(f"detail {name!r} is missing or not a string")
    return value


def parse_send_attempt(resource: object) -> SendAttemptRecord:
    """Read a send-attempt AuditEvent back into a :class:`SendAttemptRecord`.

    Tolerates the elements a server adds on read (``id``, ``meta``, ``text``) and a server that
    rewrites a time in another valid form (``Z`` for ``+00:00``). Anything else must be exactly
    what :func:`build_send_attempt_audit_event` writes: the values are read out, validated as a
    :class:`SendAttempt`, built again, and the rebuilt resource must equal the one given. So an
    AuditEvent that is not one of ours, or one of ours that was altered, raises ``FhirInputError``.
    """
    if not isinstance(resource, dict) or resource.get("resourceType") != "AuditEvent":
        raise _not_ours("resourceType is not AuditEvent")
    if not _codings_match(resource.get("type"), codes.AUDIT_TYPE_EXPORT):
        raise _not_ours("type is not DICOM 110106 Export")
    body = {k: v for k, v in resource.items() if k not in _SERVER_ELEMENTS}

    period = body.get("period") if isinstance(body.get("period"), dict) else {}
    sent_at = _instant(period.get("start"), "period.start")
    recorded = _instant(body.get("recorded"), "recorded")
    agents = body.get("agent")
    sender = _one(
        agents,
        lambda a: (
            isinstance(a, dict)
            and _codings_match(_first_coding(a.get("type")), codes.AGENT_SOURCE_ROLE)
        ),
        "source-role agent",
    )
    receiver = _one(
        agents,
        lambda a: (
            isinstance(a, dict)
            and _codings_match(_first_coding(a.get("type")), codes.AGENT_DESTINATION_ROLE)
        ),
        "destination-role agent",
    )
    entities = body.get("entity")
    patient = _one(
        entities,
        lambda e: isinstance(e, dict) and _codings_match(e.get("role"), codes.ENTITY_ROLE_PATIENT),
        "patient entity",
    )
    message = _one(
        entities,
        lambda e: isinstance(e, dict) and _codings_match(e.get("role"), codes.ENTITY_ROLE_REPORT),
        "message entity",
    )
    details = _details(message)

    result: Acknowledged | NotAcknowledged
    ack_code = _string(details, DETAIL_ACK_CODE)
    if ack_code is not None:
        encoded = _string(details, DETAIL_RAW_ACK, required=True)
        try:
            raw_ack = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _not_ours("the raw ACK is not valid base64") from exc
        result = Acknowledged(
            code=ack_code, raw_ack=raw_ack, reason=_string(details, DETAIL_REASON)
        )
    else:
        status = _string(details, DETAIL_HTTP_STATUS)
        if status is not None and not _HTTP_STATUS.match(status):
            raise _not_ours("http-status is not a three-digit number")
        result = NotAcknowledged(
            why=_string(details, DETAIL_NO_ACK_REASON, required=True),
            http_status=int(status) if status is not None else None,
        )
    message_name = (
        _identifier_value(message["what"], codes.PB_HL7V2_MESSAGE, "message entity")
        if "what" in message
        else None
    )
    attempt = SendAttempt(
        sent_at=sent_at,
        transport=_string(details, DETAIL_TRANSPORT, required=True),
        control_id=_string(details, DETAIL_CONTROL_ID),
        result=result,
        patient_id=_identifier_value(patient.get("what"), codes.PB_PATIENT_ID, "patient entity"),
        agent=_identifier_value(sender.get("who"), codes.PB_SERVICE, "source agent"),
        destination=_identifier_value(receiver.get("who"), codes.PB_HL7V2_STORE, "destination"),
        message_name=message_name,
        sample_name=_string(details, DETAIL_SAMPLE),
    )
    rebuilt = build_send_attempt_audit_event(attempt, recorded=recorded)
    if "id" in resource and resource["id"] != rebuilt.key:
        raise _not_ours("its id is not the one derived from the attempt")
    expected = {k: v for k, v in rebuilt.resource.items() if k != "id"}
    # The instants were parsed from the given strings; compare as instants, not as spellings.
    given = copy.deepcopy(body)
    given["period"] = expected["period"]
    given["recorded"] = expected["recorded"]
    if given != expected:
        raise _not_ours("it differs from the AuditEvent this library builds for the same attempt")

    return SendAttemptRecord(
        id=rebuilt.key,
        sent_at=sent_at,
        recorded=recorded,
        transport=attempt.transport,
        control_id=attempt.control_id,
        result=_result_kind(result),
        outcome=rebuilt.resource["outcome"],
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
    )


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
