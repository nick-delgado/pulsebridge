"""End to end through the Flask app: fake MLLP adapter on 127.0.0.1, FakeTransport for HTTPS
ingest and the audit store. Each test checks the result the caller gets AND the AuditEvent."""

import base64
import hashlib
import json
import logging
import random
from datetime import UTC, datetime

import pytest
from cuff_sim import message as message_module
from cuff_sim.app import READINGS_PATH, SAMPLES_PATH, create_app
from cuff_sim.ratelimit import TokenBucket
from pulsebridge_fhir import parse_send_attempt
from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.testing import json_response
from pulsebridge_hl7v2 import HL7ValidationError, parse_er7
from pulsebridge_hl7v2.samples import load

INGEST = "/hl7V2Stores/main/messages:ingest"
READING = {"systolic": 123, "diastolic": 77}


def control_id_of(payload: bytes) -> str:
    return parse_er7(payload).msh.msh_10.to_er7()


def sha_id(raw: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode("ascii")


@pytest.fixture
def bucket():
    return TokenBucket(per_minute=600, burst=100)


@pytest.fixture
def audit_ok(transport):
    transport.add("PUT", "", json_response(200, {}))  # any PUT; tests check the path
    return transport


@pytest.fixture
def http(config, client, bucket):
    return create_app(config, client, bucket=bucket).test_client()


def post(http, **body):
    response = http.post(READINGS_PATH, json=body)
    return response, response.get_json()


def only_audit_put(transport, h):
    """Exactly one request went to a FHIR store: one PUT AuditEvent to the AUDIT store."""
    fhir = [r for r in transport.requests if "/fhirStores/" in r.path]
    assert len(fhir) == 1, [r.path for r in fhir]
    (put,) = fhir
    assert put.method == "PUT"
    prefix = f"/v1/projects/{h.PROJECT}/locations/us-central1/datasets/demo/fhirStores/audit"
    assert put.path.startswith(prefix + "/fhir/AuditEvent/")
    assert put.headers["Content-Type"].startswith("application/fhir+json")
    return put.path.rsplit("/", 1)[1], put.json()


def details(event):
    out = {}
    for d in event["entity"][1]["detail"]:
        out[d["type"]] = d.get("valueString", d.get("valueBase64Binary"))
    return out


def assert_common_audit(event, h, *, transport):
    assert event["resourceType"] == "AuditEvent"
    assert event["type"]["code"] == "110106"  # DICOM Export
    assert event["agent"][0]["who"]["identifier"]["value"] == "cuff-sim"
    assert event["agent"][1]["who"]["identifier"] == {
        "system": "urn:pulsebridge:hl7v2-store",
        "value": h.STORE_NAME,
    }
    assert event["entity"][0]["what"]["identifier"] == {
        "system": "urn:pulsebridge:patient",
        "value": "SYN-000123",  # the configured patient, by identifier only
    }
    assert "name" not in json.dumps(event["entity"][0])  # no patient name in the record
    assert details(event)["transport"] == transport


# ---- Valid readings ------------------------------------------------------------------------


def test_valid_reading_over_mllp(http, adapter, audit_ok, h):
    adapter.reply_with(lambda p: h.ack_for(control_id_of(p)))
    response, body = post(http, **READING, transport="mllp")
    assert response.status_code == 200

    received = bytes(adapter.connections[0].received)
    assert received.startswith(b"\x0b") and received.endswith(b"\x1c\r")
    payload = received[1:-2]
    # Segments end with CR all the way to the wire; no line feed anywhere.
    assert b"\n" not in received
    assert payload.count(b"\r") == 5 and payload.endswith(b"\r")
    assert b"|123|mm[Hg]" in payload and b"|77|mm[Hg]" in payload
    assert b"SYN-000123^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA" in payload

    control_id = control_id_of(payload)
    assert body["controlId"] == control_id and len(control_id) == 20
    assert body["transport"] == "mllp"
    assert body["result"] == "accepted"
    assert body["sample"] is None
    assert body["framing"] == "standard"
    assert body["messageSent"] == payload.decode("ascii")
    assert body["ack"] == {
        "received": True,
        "code": "AA",
        "controlId": control_id,
        "reason": None,
        "raw": h.ack_for(control_id).decode("ascii"),
    }
    # DERIVED name: URL-safe base64 of SHA-256 of the bytes inside the frame (spike S5).
    assert body["messageName"] == f"{h.STORE_NAME}/messages/{sha_id(payload)}"
    assert body["messageNameSource"] == "derived-sha256-unverified"
    assert body["audit"] == {"recorded": True}

    attempt_id, event = only_audit_put(audit_ok, h)
    assert body["attemptId"] == attempt_id == event["id"]
    assert_common_audit(event, h, transport="mllp")
    assert event["outcome"] == "0"
    d = details(event)
    assert d["MSH-10"] == control_id and d["MSA-1"] == "AA"
    assert base64.b64decode(d["ack-message"]) == h.ack_for(control_id)
    assert "malformed-sample" not in d and "no-ack-reason" not in d
    # The derived name is recorded, marked as derived (T33): a detail of its own, never the
    # entity's "what", which is kept for a name the store returned.
    assert d["derived-message-name"] == body["messageName"]
    assert "what" not in event["entity"][1]
    record = parse_send_attempt(event)
    assert (record.message_name, record.derived_message_name) == (None, body["messageName"])
    assert record.message_name_source == "derived"
    assert not [r for r in audit_ok.requests if "hl7V2Stores" in r.path]  # MLLP: no ingest


def test_valid_reading_over_https(http, adapter, audit_ok, h):
    def ingest_ok(request_body):
        return request_body

    audit_ok.add(
        "POST",
        INGEST,
        json_response(
            200,
            {
                "hl7Ack": h.b64(h.S5_AA),
                "message": {"name": f"{h.STORE_NAME}/messages/stored-id="},
            },
        ),
    )
    response, body = post(http, **READING, transport="https")
    assert response.status_code == 200
    assert not adapter.connections  # HTTPS opens no MLLP connection

    (ingest,) = audit_ok.calls("POST", INGEST)
    sent = base64.b64decode(ingest.json()["message"]["data"])
    assert ingest.json()["message"]["labels"] == {"transport": "https"}
    assert b"\n" not in sent and sent.endswith(b"\r")
    assert body["messageSent"] == sent.decode("ascii")
    assert body["framing"] is None
    assert body["result"] == "accepted"
    assert body["ack"]["code"] == "AA" and body["ack"]["raw"] == h.S5_AA.decode("ascii")
    assert body["messageName"] == f"{h.STORE_NAME}/messages/stored-id="
    assert body["messageNameSource"] == "ingest-response"

    _id, event = only_audit_put(audit_ok, h)
    assert_common_audit(event, h, transport="https")
    assert event["outcome"] == "0"
    # From the ingest response, the stored name IS recorded (T31).
    assert event["entity"][1]["what"]["identifier"] == {
        "system": "urn:pulsebridge:source-record:hl7v2-message",
        "value": f"{h.STORE_NAME}/messages/stored-id=",
    }
    assert "derived-message-name" not in details(event)  # returned, not derived


def test_nack_over_https_is_returned_as_the_commit_level_ack(http, audit_ok, h):
    nack = {
        "error": {
            "code": 400,
            "message": "Invalid MSH header",
            "details": [{"hl7Nack": h.b64(h.S5_NACK_NO_MSH)}],
        }
    }
    audit_ok.add("POST", INGEST, json_response(400, nack))
    response, body = post(http, transport="https", sendMalformed="unparseable_no_msh")
    assert response.status_code == 200  # an answer, not an error
    assert body["result"] == "rejected"
    assert body["ack"] == {
        "received": True,
        "code": "AE",
        "controlId": None,
        "reason": "Invalid MSH header",
        "raw": h.S5_NACK_NO_MSH.decode("ascii"),
    }
    assert body["messageName"] is None
    assert body["sample"] == "unparseable_no_msh" and body["controlId"] is None
    assert body["messageSent"] == load("unparseable_no_msh").payload.decode("ascii")

    _id, event = only_audit_put(audit_ok, h)
    assert event["outcome"] == "4"
    d = details(event)
    assert d["MSA-1"] == "AE" and d["MSA-3"] == "Invalid MSH header"
    assert d["malformed-sample"] == "unparseable_no_msh"
    assert "MSH-10" not in d  # the sample has no readable control ID


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_api_error_over_https_is_no_ack_with_status(http, audit_ok, h, status):
    audit_ok.add("POST", INGEST, json_response(status, {"error": {"code": status}}))
    response, body = post(http, **READING, transport="https")
    assert response.status_code == 200  # never raised to the caller
    assert body["result"] == "no-acknowledgment"
    assert body["ack"]["received"] is False
    assert body["ack"]["why"] == "api-error"
    assert body["ack"]["httpStatus"] == status
    _id, event = only_audit_put(audit_ok, h)
    assert event["outcome"] == "8"
    d = details(event)
    assert d["no-ack-reason"] == "api-error" and d["http-status"] == str(status)
    assert "MSA-1" not in d


def test_network_failure_over_https_is_connection_closed(http, audit_ok, h):
    audit_ok.add("POST", INGEST, RetryableError("POST ingest", None))
    _response, body = post(http, **READING, transport="https")
    assert body["ack"]["why"] == "connection-closed" and body["ack"]["httpStatus"] is None
    _id, event = only_audit_put(audit_ok, h)
    assert details(event)["no-ack-reason"] == "connection-closed"
    assert "http-status" not in details(event)


# ---- MLLP failures and their AuditEvents ----------------------------------------------------


@pytest.mark.parametrize(
    ("behaviour", "why"),
    [("close", "connection-closed"), ("silent", "timeout"), ("reset", "connection-closed")],
)
def test_no_ack_over_mllp_is_recorded(http, adapter, audit_ok, h, behaviour, why):
    getattr(adapter, behaviour)()
    _response, body = post(http, **READING)
    assert body["transport"] == "mllp"  # the default
    assert body["result"] == "no-acknowledgment"
    assert body["ack"]["why"] == why and body["ack"]["httpStatus"] is None
    assert body["messageName"] is None  # nothing derived without an AA
    _id, event = only_audit_put(audit_ok, h)
    assert event["outcome"] == "8"
    assert details(event)["no-ack-reason"] == why
    assert details(event)["MSH-10"] == body["controlId"]


def test_unreachable_adapter_is_recorded(client, audit_ok, bucket, h):
    config = h.make_config(h.closed_port())
    http = create_app(config, client, bucket=bucket).test_client()
    _response, body = post(http, **READING)
    assert body["ack"]["why"] == "connection-closed"
    _id, event = only_audit_put(audit_ok, h)
    assert details(event)["no-ack-reason"] == "connection-closed"


def test_garbage_reply_is_recorded_with_its_bytes(http, adapter, audit_ok, h):
    adapter.reply(b"NOT AN ACK")
    _response, body = post(http, **READING)
    assert body["ack"]["why"] == "unreadable-reply"
    assert base64.b64decode(body["ack"]["replyPrefixBase64"]) == b"NOT AN ACK"
    _id, event = only_audit_put(audit_ok, h)
    d = details(event)
    assert d["no-ack-reason"] == "unreadable-reply"
    assert base64.b64decode(d["unreadable-reply-bytes"]) == b"NOT AN ACK"
    assert "ack-message" not in d


def test_ack_control_id_is_msa2_not_our_msh10(http, adapter, audit_ok, h, caplog):
    adapter.reply(h.S4_AA)  # the spike's exact ACK answers the spike's control ID
    with caplog.at_level(logging.INFO):
        _response, body = post(http, **READING)
    assert body["ack"]["controlId"] == "S4-205329-0001"  # MSA-2 as received
    assert body["controlId"] != "S4-205329-0001"  # our MSH-10
    assert body["result"] == "accepted"  # a mismatch is logged, not turned into a failure
    send = next(
        r.fields for r in caplog.records if getattr(r, "fields", {}).get("event") == "cuff-send"
    )
    assert send["ack_matches_control_id"] is False


def test_s4_nack_over_mllp_with_reason(http, adapter, audit_ok, h):
    adapter.reply(h.S4_NACK_LF)
    _response, body = post(http, sendMalformed="unparseable_lf_terminators")
    assert body["result"] == "rejected"
    assert body["ack"]["reason"] == 'invalid segment terminator, store expects: "\\r"'
    # The sample's own bytes, line feeds included: a malformed send is exact.
    assert bytes(adapter.connections[0].received) == load("unparseable_lf_terminators").wire_bytes
    _id, event = only_audit_put(audit_ok, h)
    assert details(event)["MSA-3"] == 'invalid segment terminator, store expects: "\\r"'


# ---- Malformed samples ----------------------------------------------------------------------


def test_framing_broken_sample_is_sent_with_its_own_framing(http, adapter, audit_ok, h):
    adapter.silent()  # S4: the adapter waits for end bytes that never come
    sample = load("noack_missing_end_block")
    _response, body = post(http, sendMalformed=sample.name, transport="mllp")
    received = bytes(adapter.connections[0].received)
    assert received == sample.wire_bytes
    assert received.startswith(b"\x0b") and b"\x1c" not in received  # no end block
    assert body["framing"] == "missing_end_block"
    assert body["ack"]["why"] == "timeout"
    assert body["controlId"] == "SYN-NOACK-0001"
    _id, event = only_audit_put(audit_ok, h)
    assert details(event)["malformed-sample"] == sample.name
    assert details(event)["no-ack-reason"] == "timeout"


def test_structurally_invalid_sample_bypasses_validation(http, adapter, audit_ok, h):
    adapter.reply_with(lambda p: h.ack_for(control_id_of(p)))
    sample = load("invalid_missing_pid3")
    _response, body = post(http, sendMalformed=sample.name)
    assert bytes(adapter.connections[0].received) == sample.wire_bytes  # sent although invalid
    assert body["result"] == "accepted" and body["controlId"] == "SYN-BAD-0001"
    _id, event = only_audit_put(audit_ok, h)
    assert details(event)["malformed-sample"] == sample.name


def test_framing_sample_over_https_is_refused_and_nothing_is_sent(http, adapter, transport):
    response, body = post(http, sendMalformed="noack_missing_end_block", transport="https")
    assert response.status_code == 422
    assert body["issue"][0]["expression"] == ["sendMalformed"]
    assert not adapter.connections and not transport.requests


def test_unknown_sample_is_refused_and_nothing_is_sent(http, adapter, transport):
    response, body = post(http, sendMalformed="drop_table_patients")
    assert response.status_code == 422
    assert "drop_table" not in json.dumps(body)  # the caller's text is not echoed
    assert not adapter.connections and not transport.requests


def test_valid_samples_are_not_offered_as_malformed(http, adapter, transport):
    response, _ = post(http, sendMalformed="valid_bp_reading")
    assert response.status_code == 422
    assert not adapter.connections


def test_samples_route_lists_malformed_samples_by_transport(http):
    body = http.get(SAMPLES_PATH).get_json()
    by_name = {s["name"]: s for s in body}
    assert "valid_bp_reading" not in by_name
    assert by_name["noack_missing_end_block"]["transports"] == ["mllp"]
    assert by_name["unparseable_no_msh"] == {
        "name": "unparseable_no_msh",
        "label": load("unparseable_no_msh").description,
        "class": "unparseable",
        "transports": ["mllp", "https"],
    }
    assert {s["class"] for s in body} == {
        "structurally_invalid",
        "unparseable",
        "no_acknowledgment",
        "parses_but_cannot_map",
    }


# ---- Validation gate ------------------------------------------------------------------------


@pytest.mark.parametrize("transport_name", ["mllp", "https"])
def test_message_failing_validation_is_never_sent(
    http, adapter, transport, monkeypatch, transport_name
):
    def fail(_payload):
        raise HL7ValidationError(["PID-3 (patient identifier list) is required but missing"])

    monkeypatch.setattr(message_module, "validate_oru_r01", fail)
    response, body = post(http, **READING, transport=transport_name)
    assert response.status_code == 500
    assert body["issue"][0]["code"] == "structure"
    assert not adapter.connections  # no MLLP connection
    assert not transport.requests  # no ingest, no AuditEvent


def test_validation_runs_on_the_exact_bytes_sent(http, adapter, audit_ok, monkeypatch, h):
    seen = []
    real = message_module.validate_oru_r01

    def spy(payload):
        seen.append(payload)
        return real(payload)

    monkeypatch.setattr(message_module, "validate_oru_r01", spy)
    adapter.reply(h.S4_AA)
    post(http, **READING)
    assert seen == [bytes(adapter.connections[0].received)[1:-2]]


# ---- Control IDs, derived name --------------------------------------------------------------


def test_every_reading_gets_a_new_control_id(http, adapter, audit_ok, h):
    adapter.reply_with(lambda p: h.ack_for(control_id_of(p)))
    ids = [post(http, **READING)[1]["controlId"] for _ in range(5)]
    assert len(set(ids)) == 5
    on_wire = [control_id_of(bytes(c.received)[1:-2]) for c in adapter.connections]
    assert on_wire == ids


def test_derived_message_name_matches_hand_computed_values(h):
    # SHA-256 of the empty string is the published e3b0c442...b855; its URL-safe base64 is
    # below (note "-" and "_", which standard base64 would write as "+" and "/").
    assert message_module.derived_message_name(h.STORE_NAME, b"") == (
        f"{h.STORE_NAME}/messages/47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU="
    )
    assert message_module.derived_message_name(
        h.STORE_NAME, b"MSH|^~\\&|CUFFSIM|PULSEBRIDGE\r"
    ) == (f"{h.STORE_NAME}/messages/HN3vn4emO9WMei98Rq8rVKLUCYfAnrz0mxUD-Dl30f8=")


def test_no_derived_name_for_a_nack_over_mllp(http, adapter, audit_ok, h):
    adapter.reply(h.S4_NACK_NO_MSH)
    _response, body = post(http, sendMalformed="unparseable_no_msh")
    assert body["result"] == "rejected"
    assert body["messageName"] is None and body["messageNameSource"] is None
    _id, event = only_audit_put(audit_ok, h)
    assert "derived-message-name" not in details(event)


# ---- Recording failure ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (json_response(503, {"error": {"code": 503}}), "HTTP 503"),
        (json_response(403, {"error": {"code": 403}}), "HTTP 403"),
        (RetryableError("PUT", None), "no HTTP response"),
    ],
)
def test_recording_failure_does_not_change_the_result(
    config, client, transport, adapter, bucket, h, caplog, failure, detail
):
    adapter.reply(h.S4_AA)
    transport.add("PUT", "", json_response(200, {}))
    ok = create_app(config, client, bucket=bucket).test_client()
    _r, recorded = post(ok, **READING)

    failing_transport = type(transport)()
    failing_transport.add("PUT", "", failure)
    failing_client = type(client)(
        project=h.PROJECT, location="us-central1", dataset="demo", transport=failing_transport
    )
    bad = create_app(config, failing_client, bucket=bucket).test_client()
    with caplog.at_level(logging.INFO):
        response, body = post(bad, **READING)
    assert response.status_code == 200
    assert body["audit"] == {
        "recorded": False,
        "detail": f"the audit store write failed ({detail})",
    }
    # Everything about the send is as it would have been with a working audit store.
    for key in ("result", "transport", "framing", "sample", "messageNameSource"):
        assert body[key] == recorded[key]
    assert body["ack"] == recorded["ack"]
    assert body["attemptId"]  # known, even though not stored
    events = [r.fields["event"] for r in caplog.records if hasattr(r, "fields")]
    assert "audit-write-failed" in events


def test_unexpected_audit_write_error_is_reported_not_raised(
    http, adapter, audit_ok, monkeypatch, h
):
    from pulsebridge_gcp import HealthcareClient

    def explode(*_a, **_k):
        raise RuntimeError("SYN-000123")

    monkeypatch.setattr(HealthcareClient, "put_resource", explode)
    adapter.reply(h.S4_AA)
    response, body = post(http, **READING)
    assert response.status_code == 200 and body["result"] == "accepted"
    assert body["audit"] == {"recorded": False, "detail": "the audit store write failed"}
    assert body["attemptId"]


def test_audit_build_failure_is_reported_not_raised(
    http, adapter, audit_ok, monkeypatch, h, caplog
):
    from cuff_sim import audit

    def refuse(*_a, **_k):
        from pulsebridge_fhir import FhirInputError

        raise FhirInputError("refused")

    monkeypatch.setattr(audit, "build_send_attempt_audit_event", refuse)
    adapter.reply(h.S4_AA)
    with caplog.at_level(logging.INFO):
        response, body = post(http, **READING)
    assert response.status_code == 200 and body["result"] == "accepted"
    assert body["audit"]["recorded"] is False and body["attemptId"] is None
    assert not [r for r in audit_ok.requests if r.method == "PUT"]
    failed = next(f for f in logged_fields(caplog) if f["event"] == "audit-write-failed")
    assert failed["failure"] == "build-failed"  # T31 refused a value: not a bug


# ---- Rate limit -----------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_rate_limit_answers_429_with_retry_after(config, client, adapter, audit_ok, h):
    clock = Clock()
    bucket = TokenBucket(per_minute=6, burst=2, clock=clock)  # one token every 10 s
    http = create_app(config, client, bucket=bucket).test_client()
    adapter.reply(h.S4_AA)
    assert post(http, **READING)[0].status_code == 200
    assert post(http, sendMalformed="unparseable_no_msh")[0].status_code == 200  # counts too
    response, body = post(http, sendMalformed="unparseable_no_msh")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "10"
    assert body["issue"][0]["code"] == "throttled"
    assert len(adapter.connections) == 2  # the refused send never connected
    clock.now += 10
    assert post(http, **READING)[0].status_code == 200


def test_invalid_input_does_not_use_a_token(config, client, adapter, audit_ok, h):
    bucket = TokenBucket(per_minute=1, burst=1, clock=Clock())
    http = create_app(config, client, bucket=bucket).test_client()
    adapter.reply(h.S4_AA)
    assert post(http, systolic=999, diastolic=80)[0].status_code == 422
    assert post(http, **READING)[0].status_code == 200


# ---- Logs -----------------------------------------------------------------------------------


FIXED_TIME = datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC)
# Letters only, so no logged value can contain the reading's digits by chance.
FIXED_CONTROL_ID = "LOGTESTCONTROLIDABCD"
SENSITIVE = (
    "187",  # systolic
    "113",  # diastolic
    "500",  # the refused diastolic
    "SYN-000123",
    "TESTPATIENT",
    "SYNTHIA",
    "PULSEBRIDGE^MR",
    "Invalid MSH header",  # MSA-3
    "MSH|",
    "ORU",
    "demo-project",  # anything from the store name
)


def logged_fields(caplog):
    return [r.fields for r in caplog.records if hasattr(r, "fields")]


def test_logs_hold_no_values_patient_or_message_text(config, client, adapter, audit_ok, h, caplog):
    # Fixed control ID and clock: nothing random can match a sensitive value by chance.
    http = create_app(
        config,
        client,
        bucket=TokenBucket(per_minute=600, burst=100),
        control_ids=lambda: FIXED_CONTROL_ID,
        clock=lambda: FIXED_TIME,
    ).test_client()
    adapter.reply(h.S4_NACK_NO_MSH)
    with caplog.at_level(logging.DEBUG):
        _r, body = post(http, systolic=187, diastolic=113)
        post(http, systolic=187, diastolic=500)  # refused
    records = logged_fields(caplog)
    for fields in records:
        for key, value in fields.items():
            if key == "duration_ms":  # a timing in ms, which can be any number
                continue
            for secret in SENSITIVE:
                assert secret not in json.dumps(value), (key, secret)
    assert all(secret not in r.getMessage() for r in caplog.records for secret in SENSITIVE)
    send = next(f for f in records if f["event"] == "cuff-send")
    assert send["control_id"] == FIXED_CONTROL_ID == body["controlId"]
    assert send["attempt_id"] == body["attemptId"]
    assert send["ack_code"] == "AE" and send["result"] == "rejected"
    assert set(send) == {
        "event",
        "transport",
        "result",
        "sample",
        "framing",
        "control_id",
        "ack_code",
        "no_ack_reason",
        "http_status",
        "message_name_source",
        "attempt_id",
        "audit_recorded",
        "audit_failure",
        "audit_http_status",
        "duration_ms",
    }
    refused = next(f for f in records if f["event"] == "request-refused")
    assert refused == {"event": "request-refused", "http_status": 422, "fields": ["diastolic"]}


def test_a_bug_answers_500_and_logs_the_type_only(http, monkeypatch, caplog):
    from cuff_sim import app as app_module

    def broken(_body):
        raise KeyError("SYN-000123")

    monkeypatch.setattr(app_module, "parse_request", broken)
    with caplog.at_level(logging.ERROR):
        response = http.post(READINGS_PATH, json=READING)
    assert response.status_code == 500
    logged = [json.dumps(r.fields) + r.getMessage() for r in caplog.records if hasattr(r, "fields")]
    assert logged and all("SYN-000123" not in line for line in logged)
    assert [r.fields["error_type"] for r in caplog.records if hasattr(r, "fields")] == ["KeyError"]


def test_no_other_routes(http):
    assert http.get("/").status_code == 404
    assert http.get("/healthz").status_code == 404
    assert http.get(READINGS_PATH).status_code == 405


def test_a_clock_stepping_back_still_records_the_attempt(config, client, adapter, audit_ok, h):
    from datetime import UTC, datetime, timedelta

    from cuff_sim.request import SendRequest
    from cuff_sim.service import CuffSimulator

    times = iter(
        [datetime(2026, 11, 15, 10, tzinfo=UTC), datetime(2026, 11, 15, 9, 59, tzinfo=UTC)]
    )
    simulator = CuffSimulator(config, client, clock=lambda: next(times))
    adapter.reply(h.S4_AA)
    request = SendRequest(transport="mllp", systolic=120, diastolic=80)
    result = simulator.send(simulator.prepare(request), "mllp")
    assert result.audit.recorded  # T31 refuses recorded < sent_at; the service never asks it to
    _id, event = only_audit_put(audit_ok, h)
    assert event["recorded"] == event["period"]["start"]
    assert timedelta(0) == (
        datetime.fromisoformat(event["recorded"]) - datetime.fromisoformat(event["period"]["start"])
    )


# ---- Replies that crash the parser, and a fuzz check -----------------------------------------


def crash_reply_cases(h):
    """Two ingest responses carrying ``raw``: a 400 with it as hl7Nack, a 200 with it as hl7Ack."""

    def nack(raw):
        return json_response(400, {"error": {"code": 400, "details": [{"hl7Nack": h.b64(raw)}]}})

    def ok(raw):
        name = f"{h.STORE_NAME}/messages/x="
        return json_response(200, {"hl7Ack": h.b64(raw), "message": {"name": name}})

    return nack, ok


@pytest.mark.parametrize("where", ["mllp", "https-ack", "https-nack"])
def test_reply_that_crashes_the_parser_is_recorded_not_a_500(http, adapter, audit_ok, h, where):
    reply = b"MSH^~MSH|"  # T07's parse_ack raises IndexError on this (reported to T07)
    nack, ok = crash_reply_cases(h)
    if where == "mllp":
        adapter.reply(reply)
        response, body = post(http, **READING)
    else:
        audit_ok.add("POST", INGEST, ok(reply) if where == "https-ack" else nack(reply))
        response, body = post(http, **READING, transport="https")
    assert response.status_code == 200
    assert body["result"] == "no-acknowledgment" and body["ack"]["why"] == "unreadable-reply"
    assert base64.b64decode(body["ack"]["replyPrefixBase64"]) == reply
    assert body["messageName"] is None
    _id, event = only_audit_put(audit_ok, h)
    assert details(event)["no-ack-reason"] == "unreadable-reply"
    assert base64.b64decode(details(event)["unreadable-reply-bytes"]) == reply


FUZZ_PIECES = [
    b"MSH",
    b"MSA",
    b"PID",
    b"|",
    b"^",
    b"~",
    b"\\",
    b"&",
    b"\r",
    b"\n",
    b"AA",
    b"AE",
    b"AR",
    b"CA",
    b"ACK",
    b"2.5.1",
    b"9.9",
    b"",
    b"\x00",
    b"\xff",
    b"\xef\xbb\xbf",
    b"\x0b",
    b"\x1c",
    b"\\E\\",
    b"\\F\\",
    b"#",
    b"X" * 50,
]


def fuzz_replies(seed: int, count: int) -> list[bytes]:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not security
    out = []
    for _ in range(count):
        if rng.random() < 0.5:
            head = rng.choice([b"MSH|^~\\&|", b"MSH^~", b"MSH|", b"MSH#^~\\&#", b"MSH"])
        else:
            head = b""
        body = b"".join(rng.choice(FUZZ_PIECES) for _ in range(rng.randint(0, 25)))
        if rng.random() < 0.3:
            body += bytes(rng.randrange(256) for _ in range(rng.randint(0, 8)))
        out.append(head + body)
    return out


def test_fuzzed_replies_are_always_a_result(h):
    from cuff_sim.outcome import AckReceived, NoAck, read_ack

    for raw in [b"MSH^~MSH|", *fuzz_replies(seed=13, count=3000)]:
        outcome = read_ack(raw)  # must not raise
        assert isinstance(outcome, AckReceived | NoAck)
        if isinstance(outcome, NoAck):
            assert outcome.why == "unreadable-reply"
            assert outcome.reply_prefix == (raw[:4096] or None)


def test_fuzzed_replies_never_give_a_5xx_and_are_always_recorded(config, transport, h):
    from pulsebridge_gcp import HealthcareClient

    nack, ok = crash_reply_cases(h)
    replies = fuzz_replies(seed=29, count=150)
    responses = [ok(r) if i % 2 else nack(r) for i, r in enumerate(replies)]
    transport.add("POST", INGEST, *responses)
    transport.add("PUT", "", json_response(200, {}))
    client = HealthcareClient(
        project=h.PROJECT, location="us-central1", dataset="demo", transport=transport
    )
    bucket = TokenBucket(per_minute=600, burst=len(replies))
    http = create_app(config, client, bucket=bucket).test_client()
    for i in range(len(replies)):
        response, body = post(http, **READING, transport="https")
        assert response.status_code == 200, (i, replies[i])
        puts = [r for r in transport.requests if r.method == "PUT"]
        assert len(puts) == i + 1, (i, replies[i])  # one AuditEvent per send
        assert body["audit"] == {"recorded": True}, (i, replies[i])


def test_audit_build_bug_is_reported_not_raised(http, adapter, audit_ok, monkeypatch, h, caplog):
    from cuff_sim import audit

    def bug(*_a, **_k):
        raise IndexError("SYN-000123")

    monkeypatch.setattr(audit, "build_send_attempt_audit_event", bug)
    adapter.reply(h.S4_AA)
    with caplog.at_level(logging.INFO):
        response, body = post(http, **READING)
    failed = next(f for f in logged_fields(caplog) if f["event"] == "audit-write-failed")
    assert failed["failure"] == "build-error"  # anything else: a bug, logged by its own code
    assert response.status_code == 200 and body["result"] == "accepted"
    assert body["audit"] == {
        "recorded": False,
        "detail": "the attempt could not be expressed as an AuditEvent",
    }
    assert not [r for r in audit_ok.requests if r.method == "PUT"]


# ---- Request edges --------------------------------------------------------------------------


def test_body_over_the_size_limit_is_413_and_nothing_is_sent(http, adapter, transport):
    response = http.post(
        READINGS_PATH,
        data=json.dumps({"systolic": 120, "diastolic": 80, "pad": "x" * 20000}),
        content_type="application/json",
    )
    assert response.status_code == 413
    assert response.get_json()["issue"][0]["code"] == "too-long"
    assert not adapter.connections and not transport.requests


def test_body_just_under_the_limit_is_read(http, adapter, transport):
    response = http.post(
        READINGS_PATH,
        data=json.dumps({"systolic": 120, "diastolic": 80, "pad": "x" * 16000}),
        content_type="application/json",
    )
    assert response.status_code == 422  # read and refused for the extra field, not for size


def test_sample_names_match_exactly(http, adapter, transport):
    for name in ("UNPARSEABLE_NO_MSH", "Unparseable_No_Msh", " unparseable_no_msh"):
        response, _ = post(http, sendMalformed=name)
        assert response.status_code == 422, name
    assert not adapter.connections and not transport.requests


def test_malformed_send_needs_no_values(http, adapter, audit_ok, h):
    adapter.reply(h.S4_NACK_NO_MSH)
    response, body = post(http, sendMalformed="unparseable_no_msh")
    assert response.status_code == 200 and body["sample"] == "unparseable_no_msh"


def test_two_ack_frames_the_first_is_used_and_a_mismatch_is_visible(
    http, adapter, audit_ok, h, caplog
):
    adapter.reply_raw(
        b"\x0b" + h.ack_for("SOMEONEELSE") + b"\x1c\r" + b"\x0b" + h.S4_NACK_NO_MSH + b"\x1c\r"
    )
    with caplog.at_level(logging.INFO):
        _response, body = post(http, **READING)
    assert body["ack"]["code"] == "AA" and body["ack"]["controlId"] == "SOMEONEELSE"
    assert body["ack"]["controlId"] != body["controlId"]
    send = next(f for f in logged_fields(caplog) if f["event"] == "cuff-send")
    assert send["ack_matches_control_id"] is False


@pytest.mark.parametrize("transport_name", ["mllp", "https"])
def test_unexpected_send_error_is_recorded_not_a_500(
    http, audit_ok, monkeypatch, h, caplog, transport_name
):
    from cuff_sim import service

    def bug(*_a, **_k):
        raise RuntimeError("SYN-000123")

    monkeypatch.setattr(service, "send_mllp" if transport_name == "mllp" else "send_https", bug)
    with caplog.at_level(logging.INFO):
        response, body = post(http, **READING, transport=transport_name)
    assert response.status_code == 200
    assert body["ack"]["why"] == "connection-closed"
    assert body["ack"]["detail"] == "the send failed unexpectedly"
    _id, event = only_audit_put(audit_ok, h)
    assert details(event)["no-ack-reason"] == "connection-closed"
    send = next(f for f in logged_fields(caplog) if f["event"] == "cuff-send")
    assert send["send_error_type"] == "RuntimeError"
    assert "SYN-000123" not in json.dumps(logged_fields(caplog))
