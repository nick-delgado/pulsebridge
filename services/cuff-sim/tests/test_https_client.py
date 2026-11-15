"""HTTPS ingest through the real HealthcareClient over FakeTransport: the S5 response mapping."""

import base64
import hashlib

import pytest
from cuff_sim.https_client import send_https
from cuff_sim.outcome import AckReceived, NoAck
from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.testing import json_response
from pulsebridge_gcp.transport import HttpResponse

PAYLOAD = (
    b"MSH|^~\\&|CUFFSIM|PULSEBRIDGE|PHR|PULSEBRIDGE|20261115100000+0000||"
    b"ORU^R01^ORU_R01|T1|P|2.5.1\r"
)
INGEST = "/hl7V2Stores/main/messages:ingest"


def message_id(raw: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode("ascii")


def accepted_body(h, ack=None, name=None):
    return {
        "hl7Ack": h.b64(ack if ack is not None else h.S5_AA),
        "message": {
            "name": name or f"{h.STORE_NAME}/messages/{message_id(PAYLOAD)}",
            "data": h.b64(PAYLOAD),
        },
    }


def nack_body(h, status, nack):
    return {
        "error": {
            "code": status,
            "message": "Invalid MSH header",
            "details": [
                {
                    "@type": "type.googleapis.com/google.cloud.healthcare.v1.hl7v2."
                    "IngestMessageErrorDetail",
                    "hl7Nack": h.b64(nack),
                }
            ],
        }
    }


def test_200_with_hl7ack_is_aa_with_the_message_name(client, transport, h):
    transport.add("POST", INGEST, json_response(200, accepted_body(h)))
    delivery = send_https(client, "main", PAYLOAD)
    assert isinstance(delivery.outcome, AckReceived)
    assert delivery.outcome.ack.code == "AA"
    assert delivery.outcome.ack.raw == h.S5_AA  # no frame, no trailing CR: parsed as is
    assert delivery.message_name == f"{h.STORE_NAME}/messages/{message_id(PAYLOAD)}"


def test_ingest_request_carries_exact_bytes_and_the_transport_label(client, transport, h):
    transport.add("POST", INGEST, json_response(200, accepted_body(h)))
    send_https(client, "main", PAYLOAD)
    (call,) = transport.requests
    assert call.json() == {"message": {"data": h.b64(PAYLOAD), "labels": {"transport": "https"}}}
    assert b"\n" not in base64.b64decode(call.json()["message"]["data"])


@pytest.mark.parametrize("status", [400, 409])
def test_4xx_with_hl7nack_is_a_nack_not_a_transport_failure(client, transport, h, status):
    nack = h.S5_NACK_NO_MSH if status == 400 else h.S5_NACK_DUPLICATE
    transport.add("POST", INGEST, json_response(status, nack_body(h, status, nack)))
    delivery = send_https(client, "main", PAYLOAD)
    assert isinstance(delivery.outcome, AckReceived)
    assert delivery.outcome.ack.code == "AE"
    expected = "Invalid MSH header" if status == 400 else "message already exists"
    assert delivery.outcome.ack.reason == expected
    assert delivery.message_name is None  # a rejected message is not stored


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 429, 500, 503])
def test_error_without_nack_is_api_error_with_status(client, transport, status):
    transport.add("POST", INGEST, json_response(status, {"error": {"code": status}}))
    delivery = send_https(client, "main", PAYLOAD)
    assert isinstance(delivery.outcome, NoAck)
    assert delivery.outcome.why == "api-error"
    assert delivery.outcome.http_status == status
    assert str(status) in delivery.outcome.detail
    assert delivery.message_name is None


def test_network_failure_is_connection_closed_without_status(client, transport):
    transport.add("POST", INGEST, RetryableError("POST ingest", None, server_message="Reset"))
    outcome = send_https(client, "main", PAYLOAD).outcome
    assert outcome.why == "connection-closed"
    assert outcome.http_status is None


def test_unexpected_3xx_is_unreadable_not_api_error(client, transport):
    transport.add("POST", INGEST, HttpResponse(status=302, body=b""))
    outcome = send_https(client, "main", PAYLOAD).outcome
    assert outcome.why == "unreadable-reply"
    assert outcome.http_status is None


def test_200_with_hl7ack_not_base64_is_unreadable(client, transport, h):
    body = accepted_body(h)
    body["hl7Ack"] = "%%% not base64"
    transport.add("POST", INGEST, json_response(200, body))
    assert send_https(client, "main", PAYLOAD).outcome.why == "unreadable-reply"


def test_200_with_hl7ack_that_is_not_an_ack_keeps_the_bytes(client, transport, h):
    transport.add("POST", INGEST, json_response(200, accepted_body(h, ack=b"hello")))
    outcome = send_https(client, "main", PAYLOAD).outcome
    assert outcome.why == "unreadable-reply"
    assert outcome.reply_prefix == b"hello"


def test_nack_that_cannot_be_read_is_unreadable(client, transport, h):
    transport.add("POST", INGEST, json_response(400, nack_body(h, 400, b"MSH|garbage")))
    outcome = send_https(client, "main", PAYLOAD).outcome
    assert outcome.why == "unreadable-reply"
    assert outcome.reply_prefix == b"MSH|garbage"


def test_name_of_another_store_is_not_returned(client, transport, h):
    other = "projects/demo-project/locations/us-central1/datasets/demo/hl7V2Stores/other"
    body = accepted_body(h, name=f"{other}/messages/{message_id(PAYLOAD)}")
    transport.add("POST", INGEST, json_response(200, body))
    delivery = send_https(client, "main", PAYLOAD)
    assert delivery.outcome.ack.code == "AA"
    assert delivery.message_name is None


@pytest.mark.parametrize(
    "ack",
    [
        # parse_ack reads both of these, but T31 cannot store them as an ACK: over 4096 bytes,
        # and an MLLP end-block byte inside.
        b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113224607||ACK|A1|P|2.5.1\r"
        b"MSA|AA|T1\rNTE|1||" + b"Y" * 5000,
        b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113224607||ACK|A\x1c1|P|2.5.1\r"
        b"MSA|AA|T1",
    ],
)
def test_acks_t31_cannot_store_are_unreadable_with_a_bounded_prefix(client, transport, h, ack):
    transport.add("POST", INGEST, json_response(200, accepted_body(h, ack=ack)))
    delivery = send_https(client, "main", PAYLOAD)
    assert delivery.outcome.why == "unreadable-reply"
    assert delivery.outcome.reply_prefix == ack[:4096]
    assert delivery.message_name is None


def test_ack_with_a_leading_bom_is_unreadable(client, transport, h):
    transport.add(
        "POST", INGEST, json_response(200, accepted_body(h, ack=b"\xef\xbb\xbf" + h.S5_AA))
    )
    assert send_https(client, "main", PAYLOAD).outcome.why == "unreadable-reply"
