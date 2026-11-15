"""AuditEvent for cuff send attempts (task T31). All data is synthetic.

Expected codes, details and requests are written out literally. ACK bytes are the exact examples
recorded in docs/spikes/s4.md and docs/spikes/s5.md; their base64 was checked with coreutils
``base64`` as well as Python.
"""

import copy
import dataclasses
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

import pytest
from pulsebridge_fhir import (
    Acknowledged,
    BuiltResource,
    FhirInputError,
    NotAcknowledged,
    SendAttempt,
    Write,
    audit_event_write,
    build_ingest_bundle,
    build_patient,
    build_send_attempt_audit_event,
    parse_send_attempt,
    request_for,
    sanitise_reason,
    send_attempt_key,
    send_attempt_search_params,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_HL7V2_STORE,
    SYNTHETIC_PATIENT_REF,
    SYNTHETIC_SEND_ACCEPTED,
    SYNTHETIC_SEND_NO_ACK,
    SYNTHETIC_SEND_REJECTED,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
STORE = SYNTHETIC_HL7V2_STORE
SENT = datetime(2026, 1, 1, 14, 30, 0, 125000, tzinfo=UTC)
RECORDED = SENT + timedelta(seconds=1)

# Spike S5, accepted over HTTPS: hl7Ack decoded (no MLLP frame, no trailing CR).
S5_ACK = (
    b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113224607||ACK|"
    b"2478b790-9ee0-4adf-b7de-a897e8fecc6b|P|2.5.1\rMSA|AA|S5-0001"
)
S5_ACK_B64 = (
    "TVNIfF5+XCZ8UEhSfFBVTFNFQlJJREdFfENVRkZTSU18UFVMU0VCUklER0V8MjAyNjExMTMyMjQ2MDd8fEFDS3wy"
    "NDc4Yjc5MC05ZWUwLTRhZGYtYjdkZS1hODk3ZThmZWNjNmJ8UHwyLjUuMQ1NU0F8QUF8UzUtMDAwMQ=="
)
# Spike S4 row b1, rejected over MLLP: the NACK payload inside the frame. MSA-2 is empty.
S4_NACK = (
    b"MSH|^~\\&|||||20261113205348||ACK|63a5f7b7-0ae4-42a6-b517-eed75c1861a3"
    b"\rMSA|AE||Invalid MSH header"
)
S4_NACK_B64 = (
    "TVNIfF5+XCZ8fHx8fDIwMjYxMTEzMjA1MzQ4fHxBQ0t8NjNhNWY3YjctMGFlNC00MmE2LWI1MTctZWVkNzVjMTg2"
    "MWEzDU1TQXxBRXx8SW52YWxpZCBNU0ggaGVhZGVy"
)

DCM = "http://dicom.nema.org/resources/ontology/DCM"
SOURCE_AGENT = {
    "type": {"coding": [{"system": DCM, "code": "110153", "display": "Source Role ID"}]},
    "who": {"identifier": {"system": "urn:pulsebridge:service", "value": "cuff-sim"}},
    "requestor": True,
}
DESTINATION_AGENT = {
    "type": {"coding": [{"system": DCM, "code": "110152", "display": "Destination Role ID"}]},
    "who": {"identifier": {"system": "urn:pulsebridge:hl7v2-store", "value": STORE}},
    "requestor": False,
}
PATIENT_ENTITY = {
    "what": {
        "type": "Patient",
        "identifier": {"system": "urn:pulsebridge:patient", "value": "PB-SYN-0001"},
    },
    "type": {
        "system": "http://terminology.hl7.org/CodeSystem/audit-entity-type",
        "code": "1",
        "display": "Person",
    },
    "role": {
        "system": "http://terminology.hl7.org/CodeSystem/object-role",
        "code": "1",
        "display": "Patient",
    },
}
MESSAGE_TYPE_AND_ROLE = {
    "type": {
        "system": "http://terminology.hl7.org/CodeSystem/audit-entity-type",
        "code": "2",
        "display": "System Object",
    },
    "role": {
        "system": "http://terminology.hl7.org/CodeSystem/object-role",
        "code": "3",
        "display": "Report",
    },
}


def attempt(**changes) -> SendAttempt:
    base = {
        "sent_at": SENT,
        "transport": "https",
        "control_id": "MSG-SYN-0010",
        "result": Acknowledged(code="AA", raw_ack=S5_ACK),
        "patient_id": "PB-SYN-0001",
        "agent": "cuff-sim",
        "destination": STORE,
        "message_name": STORE + "/messages/synthetic-message-0010",
        "sample_name": None,
    }
    base.update(changes)
    return SendAttempt(**base)


def rejected(**changes) -> SendAttempt:
    base = {
        "transport": "mllp",
        "control_id": None,
        "result": Acknowledged(code="AE", raw_ack=S4_NACK, reason="Invalid MSH header"),
        "message_name": None,
        "sample_name": "unparseable_no_msh",
    }
    base.update(changes)
    return attempt(**base)


def no_ack(why="timeout", http_status=None, transport="mllp", **changes) -> SendAttempt:
    base = {"message_name": None}
    base.update(changes)
    return attempt(
        transport=transport, result=NotAcknowledged(why=why, http_status=http_status), **base
    )


def build(a: SendAttempt, recorded: datetime = RECORDED) -> dict:
    return build_send_attempt_audit_event(a, recorded=recorded).resource


def message_entity(resource: dict) -> dict:
    return resource["entity"][1]


# --- AC 1: R4 required elements, with standard codes ---------------------------------------------


def test_accepted_attempt_whole_resource():
    """The complete AuditEvent for an accepted HTTPS send, written out."""
    built = build_send_attempt_audit_event(attempt(), recorded=RECORDED)
    assert built.resource == {
        "resourceType": "AuditEvent",
        "id": built.key,
        "type": {"system": DCM, "code": "110106", "display": "Export"},
        "subtype": [
            {
                "system": "http://terminology.hl7.org/CodeSystem/v2-0003",
                "code": "R01",
                "display": "ORU/ACK - Unsolicited transmission of an observation message",
            }
        ],
        "action": "R",
        "period": {
            "start": "2026-01-01T14:30:00.125+00:00",
            "end": "2026-01-01T14:30:01.125+00:00",
        },
        "recorded": "2026-01-01T14:30:01.125+00:00",
        "outcome": "0",
        "agent": [SOURCE_AGENT, DESTINATION_AGENT],
        "source": {
            "observer": {"identifier": {"system": "urn:pulsebridge:service", "value": "cuff-sim"}},
            "type": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/security-source-type",
                    "code": "4",
                    "display": "Application Server",
                }
            ],
        },
        "entity": [
            PATIENT_ENTITY,
            {
                "what": {
                    "identifier": {
                        "system": "urn:pulsebridge:source-record:hl7v2-message",
                        "value": STORE + "/messages/synthetic-message-0010",
                    }
                },
                **MESSAGE_TYPE_AND_ROLE,
                "detail": [
                    {"type": "transport", "valueString": "https"},
                    {"type": "MSH-10", "valueString": "MSG-SYN-0010"},
                    {"type": "MSA-1", "valueString": "AA"},
                    {"type": "ack-message", "valueBase64Binary": S5_ACK_B64},
                ],
            },
        ],
    }
    assert built.full_url == f"urn:uuid:{built.key}"


@pytest.mark.parametrize("a", [attempt(), rejected(), no_ack()], ids=["aa", "ae", "none"])
def test_r4_required_elements_present(a):
    """R4 AuditEvent cardinalities: type 1..1, recorded 1..1, agent 1..* each with requestor
    1..1, source 1..1 with observer 1..1; entity sev-1 (not both name and query)."""
    r = build(a)
    assert r["type"]["code"]
    assert r["recorded"]
    assert r["agent"] and all(isinstance(ag["requestor"], bool) for ag in r["agent"])
    assert r["source"]["observer"]
    assert "identifier" not in r  # R4 AuditEvent has no identifier element
    for entity in r["entity"]:
        assert not ("name" in entity and "query" in entity)


def test_patient_is_an_entity_not_an_agent():
    r = build(attempt())
    agent_systems = {ag["who"]["identifier"]["system"] for ag in r["agent"]}
    assert "urn:pulsebridge:patient" not in agent_systems
    assert [ag["requestor"] for ag in r["agent"]] == [True, False]


# --- AC 2: transport, outcome, control ID, message name, sample name, raw ACK -------------------


def test_rejected_attempt_details_outcome_and_reason():
    r = build(rejected())
    assert r["outcome"] == "4"
    assert r["outcomeDesc"] == "Rejected by the receiver (MSA-1 AE): Invalid MSH header"
    assert message_entity(r) == {
        **MESSAGE_TYPE_AND_ROLE,
        "detail": [
            {"type": "transport", "valueString": "mllp"},
            {"type": "MSA-1", "valueString": "AE"},
            {"type": "MSA-3", "valueString": "Invalid MSH header"},
            {"type": "ack-message", "valueBase64Binary": S4_NACK_B64},
            {"type": "malformed-sample", "valueString": "unparseable_no_msh"},
        ],
    }


def test_rejected_without_reason_has_fixed_outcome_desc_and_no_msa3():
    r = build(rejected(result=Acknowledged(code="AR", raw_ack=S4_NACK)))
    assert r["outcome"] == "4"
    assert r["outcomeDesc"] == "Rejected by the receiver (MSA-1 AR)"
    assert "MSA-3" not in [d["type"] for d in message_entity(r)["detail"]]


@pytest.mark.parametrize(
    ("why", "status", "transport", "desc", "extra"),
    [
        ("timeout", None, "mllp", "No acknowledgment received: the read timed out", []),
        (
            "connection-closed",
            None,
            "mllp",
            "No acknowledgment received: the connection was closed with no reply",
            [],
        ),
        (
            "api-error",
            503,
            "https",
            "No acknowledgment received: the API returned an error with no HL7 reply (HTTP 503)",
            [{"type": "http-status", "valueString": "503"}],
        ),
    ],
)
def test_no_acknowledgment_details_and_outcome(why, status, transport, desc, extra):
    r = build(no_ack(why=why, http_status=status, transport=transport))
    assert r["outcome"] == "8"
    assert r["outcomeDesc"] == desc
    assert message_entity(r) == {
        **MESSAGE_TYPE_AND_ROLE,
        "detail": [
            {"type": "transport", "valueString": transport},
            {"type": "MSH-10", "valueString": "MSG-SYN-0010"},
            {"type": "no-ack-reason", "valueString": why},
            *extra,
        ],
    }


@pytest.mark.parametrize(
    ("code", "outcome"),
    [("AA", "0"), ("CA", "0"), ("AE", "4"), ("AR", "4"), ("CE", "4"), ("CR", "4")],
)
def test_outcome_follows_the_acknowledgment_code(code, outcome):
    accepted = code in ("AA", "CA")
    a = attempt(
        result=Acknowledged(code=code, raw_ack=S5_ACK),
        message_name=attempt().message_name if accepted else None,
    )
    assert build(a)["outcome"] == outcome


def test_code_given_as_a_str_enum_is_stored_as_plain_text():
    """T07's Ack.code is a StrEnum; the resource and the record hold the plain code."""

    class AckCode(StrEnum):
        AE = "AE"

    result = Acknowledged(code=AckCode.AE, raw_ack=S4_NACK)
    assert type(result.code) is str
    plain = Acknowledged(code="AE", raw_ack=S4_NACK)
    assert json.dumps(build(rejected(result=result))) == json.dumps(build(rejected(result=plain)))


def test_accepted_with_an_msa3_text_keeps_it():
    r = build(attempt(result=Acknowledged(code="AA", raw_ack=S5_ACK, reason="stored")))
    assert r["outcome"] == "0"
    assert r["outcomeDesc"] == "Accepted (MSA-1 AA): stored"
    assert {"type": "MSA-3", "valueString": "stored"} in message_entity(r)["detail"]


def test_malformed_sample_that_was_accepted_records_sample_and_message():
    r = build(attempt(sample_name="unmappable_unknown_obx3_code"))
    details = message_entity(r)["detail"]
    assert details[-1] == {
        "type": "malformed-sample",
        "valueString": "unmappable_unknown_obx3_code",
    }
    assert message_entity(r)["what"]["identifier"]["value"].endswith("synthetic-message-0010")


def test_offset_of_sent_time_is_kept():
    sent = datetime(2026, 1, 1, 9, 30, 0, tzinfo=timezone(timedelta(hours=-5)))
    r = build(attempt(sent_at=sent), recorded=sent + timedelta(seconds=2))
    assert r["period"] == {"start": "2026-01-01T09:30:00-05:00", "end": "2026-01-01T09:30:02-05:00"}


# --- AC 3: no free text from the caller; the message itself is never stored ---------------------


def test_input_has_no_slot_for_the_message_or_a_name():
    """The only text-bearing inputs are validated codes, names and the cleaned MSA-3 reason."""
    assert [f.name for f in dataclasses.fields(SendAttempt)] == [
        "sent_at",
        "transport",
        "control_id",
        "result",
        "patient_id",
        "agent",
        "destination",
        "message_name",
        "sample_name",
    ]
    assert [f.name for f in dataclasses.fields(Acknowledged)] == ["code", "raw_ack", "reason"]
    assert [f.name for f in dataclasses.fields(NotAcknowledged)] == ["why", "http_status"]


@pytest.mark.parametrize("a", [attempt(), rejected(), no_ack()], ids=["aa", "ae", "none"])
def test_no_message_content_or_patient_name_in_the_resource(a):
    text = json.dumps(build(a))
    for marker in ("ORU^R01", "PID|", "OBX|", "Example", "Avery", "name", 'display": "PB-'):
        assert marker not in text


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Invalid MSH header", "Invalid MSH header"),
        (
            'invalid segment terminator, store expects: "\\E\\r"',
            'invalid segment terminator, store expects: "\\E\\r"',
        ),
        ("bad\rinjected log line", "bad\ufffdinjected log line"),
        ("tab\there", "tab\ufffdhere"),
        ("nul\x00", "nul\ufffd"),
        ("bidi \u202eevil", "bidi \ufffdevil"),
        ("line\u2028sep", "line\ufffdsep"),
        ("  padded  ", "padded"),
        ("e\u0301", "\u00e9"),  # NFC
        ("   ", None),
        ("", None),
        (None, None),
    ],
)
def test_reason_is_sanitised(raw, clean):
    assert sanitise_reason(raw) == clean
    assert Acknowledged(code="AE", raw_ack=S4_NACK, reason=raw).reason == clean


def test_long_reason_is_cut_to_the_cap_and_cleaning_is_idempotent():
    cut = sanitise_reason("x" * 500)
    assert cut == "x" * 199 + "\u2026"
    assert len(cut) == 200
    assert sanitise_reason(cut) == cut
    assert sanitise_reason("y" * 200) == "y" * 200


def test_sanitised_reason_reaches_both_detail_and_outcome_desc():
    r = build(rejected(result=Acknowledged(code="AE", raw_ack=S4_NACK, reason="a\r\nb")))
    assert r["outcomeDesc"] == "Rejected by the receiver (MSA-1 AE): a\ufffd\ufffdb"
    assert {"type": "MSA-3", "valueString": "a\ufffd\ufffdb"} in message_entity(r)["detail"]


# --- Validation ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: attempt(sent_at=datetime(2026, 1, 1)), "sent_at has no time zone"),
        (lambda: attempt(sent_at="2026-01-01T00:00:00Z"), "sent_at must be a datetime"),
        (lambda: attempt(sent_at=SENT.replace(microsecond=125001)), "whole millisecond"),
        (lambda: attempt(transport="tcp"), "transport 'tcp' is not one of mllp, https"),
        (lambda: attempt(control_id="A|B"), "control_id 'A|B' must be"),
        (lambda: attempt(control_id=""), "control_id '' must be"),
        (lambda: attempt(control_id="X" * 65), "must be 1 to 64"),
        (lambda: attempt(control_id=None), "control_id is missing"),
        (lambda: attempt(sample_name="Not A Sample"), "sample_name 'Not A Sample'"),
        (lambda: attempt(sample_name="a" * 65), "sample_name"),
        (lambda: attempt(result="AA"), "result must be Acknowledged or NotAcknowledged"),
        (lambda: attempt(patient_id=""), "patient_id is missing or empty"),
        (lambda: attempt(patient_id=" PB-SYN-0001"), "leading or trailing whitespace"),
        (lambda: attempt(agent="Cuff Sim"), "agent 'Cuff Sim' must be a service name"),
        (lambda: attempt(agent="cuff-sim-"), "agent 'cuff-sim-'"),
        (lambda: attempt(destination="hl7V2Stores/x"), "destination must be an HL7v2 store"),
        (
            lambda: attempt(destination=STORE + "/messages/x", message_name=None),
            "destination must be an HL7v2 store",
        ),
        (
            lambda: attempt(message_name=STORE.replace("example-store", "other") + "/messages/x"),
            "message_name must be <destination>/messages/<id>",
        ),
        (
            # Same length as STORE, so only the "same store" check can catch it.
            lambda: attempt(message_name=STORE[:-1] + "X/messages/x"),
            "message_name must be <destination>/messages/<id>",
        ),
        (
            lambda: attempt(message_name=STORE + "/messages/a/b"),
            "message_name must be <destination>/messages/<id>",
        ),
        (lambda: rejected(message_name=STORE + "/messages/x"), "only for an accepted message"),
        (lambda: no_ack(message_name=STORE + "/messages/x"), "only for an accepted message"),
        (lambda: no_ack(why="api-error", http_status=500), "api-error happens only over https"),
        (lambda: Acknowledged(code="NA", raw_ack=S5_ACK), "code 'NA' is not one of"),
        (lambda: Acknowledged(code="AA", raw_ack="MSH|"), "raw_ack must be bytes"),
        (lambda: Acknowledged(code="AA", raw_ack=b""), "raw_ack must be 1 to 4096 bytes"),
        (lambda: Acknowledged(code="AA", raw_ack=b"MSH" + b"x" * 4094), "1 to 4096 bytes"),
        (lambda: Acknowledged(code="AA", raw_ack=b"\x0b" + S5_ACK), "MLLP frame bytes"),
        (lambda: Acknowledged(code="AA", raw_ack=S5_ACK + b"\x1c\r"), "MLLP frame bytes"),
        (lambda: Acknowledged(code="AA", raw_ack=b"PID|1"), "does not start with an MSH"),
        (lambda: Acknowledged(code="AE", raw_ack=S4_NACK, reason=42), "reason must be text"),
        (lambda: NotAcknowledged(why="dns"), "why 'dns' is not one of"),
        (lambda: NotAcknowledged(why="api-error"), "api-error needs the http_status"),
        (lambda: NotAcknowledged(why="api-error", http_status=200), r"http_status \(200\)"),
        (lambda: NotAcknowledged(why="api-error", http_status=600), r"http_status \(600\)"),
        (lambda: NotAcknowledged(why="api-error", http_status=True), "must be a number"),
        (lambda: NotAcknowledged(why="api-error", http_status=503.0), "whole number"),
        (lambda: NotAcknowledged(why="timeout", http_status=504), "only for api-error"),
    ],
)
def test_invalid_input_is_rejected(make, message):
    with pytest.raises(FhirInputError, match=message):
        make()


def test_limits_are_inclusive():
    assert Acknowledged(code="AA", raw_ack=b"MSH" + b"x" * 4093)
    assert attempt(control_id="X" * 64, sample_name="a" * 64)
    assert NotAcknowledged(why="api-error", http_status=400)
    assert NotAcknowledged(why="api-error", http_status=599)


@pytest.mark.parametrize(
    ("recorded", "message"),
    [
        (datetime(2026, 1, 1, 15), "recorded must be a datetime with a time zone"),
        ("2026-01-01T15:00:00Z", "recorded must be a datetime with a time zone"),
        (SENT - timedelta(milliseconds=1), "recorded is before sent_at"),
        (SENT + timedelta(microseconds=1), "must be a whole millisecond"),
    ],
)
def test_invalid_recorded_is_rejected(recorded, message):
    with pytest.raises(FhirInputError, match=message):
        build_send_attempt_audit_event(attempt(), recorded=recorded)


def test_builder_needs_a_send_attempt():
    with pytest.raises(FhirInputError, match="expected a SendAttempt, got dict"):
        build_send_attempt_audit_event({"transport": "mllp"}, recorded=RECORDED)


# --- AC 4: deterministic id, the resend case, the write ------------------------------------------


def test_example_ids_are_pinned():
    """Literal values: changing how the id is derived would make a re-recorded attempt a second
    AuditEvent instead of replacing the first."""
    assert send_attempt_key(SYNTHETIC_SEND_ACCEPTED) == "69ec535d-bb94-5c1e-927c-96a189a17a59"
    assert send_attempt_key(SYNTHETIC_SEND_REJECTED) == "914007c0-02fc-5df7-9571-d9a91edae82b"
    assert send_attempt_key(SYNTHETIC_SEND_NO_ACK) == "ba24b13a-bed7-55ea-879e-60bd9cab12ca"


def test_same_attempt_twice_is_one_audit_event():
    a = build_send_attempt_audit_event(attempt(), recorded=RECORDED)
    b = build_send_attempt_audit_event(attempt(), recorded=RECORDED)
    assert a == b
    # Recorded a second later (a retried write): same id, so the PUT replaces it.
    c = build_send_attempt_audit_event(attempt(), recorded=RECORDED + timedelta(seconds=1))
    assert c.key == a.key
    assert audit_event_write(c)["url"] == audit_event_write(a)["url"]


def test_resend_of_the_same_control_id_is_a_second_attempt():
    first = attempt()
    resend = attempt(sent_at=SENT + timedelta(milliseconds=1))
    assert send_attempt_key(first) != send_attempt_key(resend)


def test_same_instant_in_another_offset_is_the_same_attempt():
    local = SENT.astimezone(timezone(timedelta(hours=-5)))
    assert send_attempt_key(attempt(sent_at=local)) == send_attempt_key(attempt())


@pytest.mark.parametrize(
    "changes",
    [
        {"transport": "mllp"},
        {"control_id": "MSG-SYN-0011"},
        {"agent": "cuff-sim-2"},
    ],
)
def test_key_parts_each_change_the_id(changes):
    assert send_attempt_key(attempt(**changes)) != send_attempt_key(attempt())


def test_result_and_destination_are_not_key_parts():
    """The key names the attempt; what happened is what is recorded about it."""
    base = send_attempt_key(attempt())
    assert send_attempt_key(no_ack(transport="https")) == base
    other_store = STORE.replace("example-store", "other-store")
    assert send_attempt_key(attempt(destination=other_store, message_name=None)) == base


def test_missing_control_id_does_not_collide_with_a_real_one():
    sample = rejected(transport="https")
    assert send_attempt_key(sample) != send_attempt_key(attempt())


def test_write_is_a_put_by_id_derived_from_the_resource():
    built = build_send_attempt_audit_event(attempt(), recorded=RECORDED)
    write = audit_event_write(built)
    assert write == {
        "method": "PUT",
        "url": f"AuditEvent/{built.key}",
        "content_type": "application/fhir+json; charset=utf-8",
        "body": built.resource,
    }
    write["body"]["outcome"] = "12"
    assert built.resource["outcome"] == "0"  # the body is a copy
    assert built.update == {"method": "PUT", "url": f"AuditEvent/{built.key}"}
    assert built.create_if_absent is None


def test_write_url_follows_the_resource_id_not_a_stored_request():
    built = build_send_attempt_audit_event(attempt(), recorded=RECORDED)
    built.resource["id"] = "changed-id"
    assert audit_event_write(built)["url"] == "AuditEvent/changed-id"
    built.resource["id"] = "bad/id"
    with pytest.raises(FhirInputError, match="AuditEvent: id 'bad/id' is not a valid FHIR id"):
        audit_event_write(built)


def test_audit_event_has_no_conditional_create():
    built = build_send_attempt_audit_event(attempt(), recorded=RECORDED)
    with pytest.raises(FhirInputError, match="AuditEvent has no conditional-create request"):
        request_for(built.resource, Write.CREATE_IF_ABSENT)


def test_write_refuses_anything_but_an_audit_event():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    with pytest.raises(
        FhirInputError, match="only an AuditEvent is written this way, got 'Patient'"
    ):
        audit_event_write(patient)
    with pytest.raises(FhirInputError, match="expected a BuiltResource, got dict"):
        audit_event_write(build(attempt()))


def test_ingest_bundle_still_refuses_an_audit_event_record():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    audit = build_send_attempt_audit_event(attempt(), recorded=RECORDED)
    with pytest.raises(FhirInputError, match="writes only Observation and Provenance records"):
        build_ingest_bundle(patient=patient, devices=[], records=[audit])


# --- AC 5: reading back --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a",
    [
        attempt(),
        rejected(),
        no_ack(),
        no_ack(why="connection-closed"),
        no_ack(why="api-error", http_status=404, transport="https"),
        attempt(result=Acknowledged(code="CA", raw_ack=S5_ACK, reason="ok"), sample_name="a_b"),
    ],
)
def test_round_trip_input_to_resource_to_record(a):
    record = parse_send_attempt(build(a))
    assert record.attempt == a
    assert record.recorded == RECORDED
    assert record.sent_at == a.sent_at


def test_record_fields_for_each_result_shape():
    ok = parse_send_attempt(build(attempt()))
    assert (ok.result, ok.outcome, ok.ack_code, ok.raw_ack, ok.reason) == (
        "accepted", "0", "AA", S5_ACK, None,
    )  # fmt: skip
    assert (ok.no_ack_reason, ok.http_status, ok.control_id) == (None, None, "MSG-SYN-0010")
    assert ok.message_name == STORE + "/messages/synthetic-message-0010"
    assert ok.id == send_attempt_key(attempt())

    nack = parse_send_attempt(build(rejected()))
    assert (nack.result, nack.outcome, nack.ack_code, nack.reason, nack.raw_ack) == (
        "rejected", "4", "AE", "Invalid MSH header", S4_NACK,
    )  # fmt: skip
    assert (nack.control_id, nack.sample_name, nack.message_name, nack.transport) == (
        None, "unparseable_no_msh", None, "mllp",
    )  # fmt: skip

    none = parse_send_attempt(build(no_ack(why="api-error", http_status=403, transport="https")))
    assert (none.result, none.outcome, none.no_ack_reason, none.http_status) == (
        "no-acknowledgment", "8", "api-error", 403,
    )  # fmt: skip
    assert (none.ack_code, none.raw_ack, none.reason) == (None, None, None)


@pytest.mark.parametrize("name", ["accepted", "rejected", "no-ack"])
def test_round_trip_resource_to_record_to_resource(name):
    stored = json.loads((EXAMPLES / f"auditevent-send-{name}.json").read_text())
    record = parse_send_attempt(stored)
    again = build_send_attempt_audit_event(record.attempt, recorded=record.recorded)
    assert again.resource == stored


def test_tolerates_what_a_server_adds_on_read():
    stored = build(rejected())
    stored["meta"] = {"versionId": "MTcwMDAwMDAwMA", "lastUpdated": "2026-01-01T14:31:02.5+00:00"}
    stored["text"] = {"status": "generated", "div": '<div xmlns="http://www.w3.org/1999/xhtml"/>'}
    # A server may write the same instant another way.
    stored["recorded"] = "2026-01-01T14:30:01.125Z"
    stored["period"]["start"] = "2026-01-01T14:30:00.125Z"
    record = parse_send_attempt(stored)
    assert record.attempt == rejected()
    assert record.recorded == RECORDED


def _altered(change):
    stored = build(rejected())
    change(stored)
    return stored


def _detail(stored, name):
    return next(d for d in message_entity(stored)["detail"] if d["type"] == name)


@pytest.mark.parametrize(
    ("resource", "message"),
    [
        ({"resourceType": "Provenance"}, "resourceType is not AuditEvent"),
        ("not a dict", "resourceType is not AuditEvent"),
        (_altered(lambda r: r["type"].update(code="110107")), "type is not DICOM 110106"),
        (_altered(lambda r: r.update(id="other-id")), "its id is not the one derived"),
        (_altered(lambda r: r.update(outcome="0")), "differs from the AuditEvent"),
        (_altered(lambda r: r.update(outcomeDesc="hello")), "differs from the AuditEvent"),
        (_altered(lambda r: r.update(action="C")), "differs from the AuditEvent"),
        (_altered(lambda r: r.update(language="en")), "differs from the AuditEvent"),
        (_altered(lambda r: r["subtype"].clear()), "differs from the AuditEvent"),
        (_altered(lambda r: r["agent"][0].update(requestor=False)), "differs from the AuditEvent"),
        (_altered(lambda r: r["agent"].pop()), "expected one destination-role agent, found 0"),
        (_altered(lambda r: r["agent"][1].update(type="x")), "destination-role agent, found 0"),
        (_altered(lambda r: r["agent"].append(r["agent"][0])), "source-role agent, found 2"),
        (_altered(lambda r: r["entity"].pop(0)), "expected one patient entity, found 0"),
        (_altered(lambda r: r.update(entity="x")), "expected one patient entity, found 0"),
        (
            _altered(lambda r: r["entity"][0]["what"].update(display="Avery Example")),
            "differs from the AuditEvent",
        ),
        (
            _altered(lambda r: r["entity"][0]["what"]["identifier"].update(system="urn:x")),
            "patient entity has no urn:pulsebridge:patient identifier",
        ),
        (
            _altered(
                lambda r: message_entity(r)["detail"].append({"type": "raw", "valueString": "x"})
            ),
            "differs from the AuditEvent",
        ),
        (
            _altered(lambda r: message_entity(r)["detail"].append(_detail(r, "MSA-1"))),
            "'MSA-1' appears twice",
        ),
        (
            _altered(lambda r: _detail(r, "ack-message").update(valueBase64Binary="***")),
            "the raw ACK is not valid base64",
        ),
        (
            _altered(lambda r: _detail(r, "MSA-1").update(valueString="ZZ")),
            "code 'ZZ' is not one of",
        ),
        (_altered(lambda r: _detail(r, "MSA-1").pop("valueString")), "needs exactly one value"),
        (_altered(lambda r: r.update(recorded="yesterday")), "recorded is not a FHIR instant"),
        (_altered(lambda r: r.update(period={})), "period.start is not a FHIR instant"),
        (
            _altered(lambda r: r.update(recorded="2026-02-30T00:00:00Z")),
            "recorded is not a real date",
        ),
    ],
)
def test_resource_that_is_not_ours_is_rejected(resource, message):
    with pytest.raises(FhirInputError, match=message):
        parse_send_attempt(resource)


def test_bad_http_status_detail_is_rejected():
    stored = build(no_ack(why="api-error", http_status=503, transport="https"))
    _detail(stored, "http-status")["valueString"] = "5O3"
    with pytest.raises(FhirInputError, match="http-status is not a three-digit number"):
        parse_send_attempt(stored)


def test_parse_does_not_change_its_input():
    stored = build(rejected())
    stored["meta"] = {"versionId": "1"}
    before = copy.deepcopy(stored)
    parse_send_attempt(stored)
    assert stored == before


# --- Listing --------------------------------------------------------------------------------------


def test_search_params_newest_first():
    assert send_attempt_search_params() == [
        ("type", "http://dicom.nema.org/resources/ontology/DCM|110106"),
        ("subtype", "http://terminology.hl7.org/CodeSystem/v2-0003|R01"),
        ("_sort", "-date"),
        ("_count", "50"),
    ]


def test_search_params_with_a_range_in_utc():
    since = datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
    params = send_attempt_search_params(count=10, since=since, before=since + timedelta(days=1))
    assert params == [
        ("type", "http://dicom.nema.org/resources/ontology/DCM|110106"),
        ("subtype", "http://terminology.hl7.org/CodeSystem/v2-0003|R01"),
        ("date", "ge2026-01-01T14:00:00.000Z"),
        ("date", "lt2026-01-02T14:00:00.000Z"),
        ("_sort", "-date"),
        ("_count", "10"),
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"count": 0}, "count must be 1 to 1000"),
        ({"count": 1001}, "count must be 1 to 1000"),
        ({"count": True}, "count must be 1 to 1000"),
        ({"since": datetime(2026, 1, 1)}, "since must be a datetime with a time zone"),
        ({"since": SENT, "before": SENT}, "before must be after since"),
    ],
)
def test_search_params_reject_bad_arguments(kwargs, message):
    with pytest.raises(FhirInputError, match=message):
        send_attempt_search_params(**kwargs)


def test_a_built_resource_cannot_be_retyped_into_a_write():
    """A hand-made BuiltResource gets the request its own resource implies."""
    resource = build(attempt())
    forged = BuiltResource(resource=resource, full_url="urn:uuid:x", key="Patient/PB-SYN-0001")
    assert audit_event_write(forged)["url"] == f"AuditEvent/{resource['id']}"


def test_replace_does_not_bypass_validation():
    with pytest.raises(FhirInputError, match="transport"):
        replace(attempt(), transport="ftp")
