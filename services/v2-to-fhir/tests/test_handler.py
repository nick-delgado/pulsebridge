"""The push handler with the real HealthcareClient over a scripted transport.

Nothing here touches the network. Each test scripts what the Healthcare API answers and checks
the HTTP status returned to Pub/Sub (204 acknowledges, anything else is retried) and exactly
which API calls were made.
"""

import base64
import re

import pytest
from pulsebridge_gcp import HealthcareClient, RetryableError
from pulsebridge_gcp.testing import FakeTransport, json_response
from pulsebridge_gcp.transport import HttpResponse
from pulsebridge_hl7v2.samples import by_class, load
from v2_to_fhir.config import ServiceConfig
from v2_to_fhir.handler import handle_push, parse_envelope
from v2_to_fhir.mapping import MappingConfig

STORE = "projects/demo-project/locations/us-central1/datasets/demo/hl7V2Stores/main"
NAME = f"{STORE}/messages/m-1="
FHIR_PATH = "/fhirStores/fhir/fhir"
CONFIG = ServiceConfig(
    project_id="demo-project",
    location="us-central1",
    dataset_id="demo",
    fhir_store_id="fhir",
    hl7v2_store_id="main",
    mapping=MappingConfig(patient_id="SYN-000123"),
)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def envelope(name: str = NAME, msg_type: str | None = "ORU", attempt: int | None = 1) -> dict:
    message: dict = {"data": b64(name.encode()), "messageId": "1001", "publishTime": "x"}
    if msg_type is not None:
        message["attributes"] = {"msgType": msg_type}
    body: dict = {"message": message, "subscription": "projects/demo-project/subscriptions/s"}
    if attempt is not None:
        body["deliveryAttempt"] = attempt
    return body


def stored(raw: bytes, name: str = NAME) -> HttpResponse:
    return json_response(200, {"name": name, "data": b64(raw), "messageType": "ORU"})


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def push(transport):
    client = HealthcareClient(
        project="demo-project", location="us-central1", dataset="demo", transport=transport
    )

    def _push(body):
        return handle_push(body, client=client, config=CONFIG)

    return _push


def patched_labels(transport) -> list[dict]:
    return [r.json()["labels"] for r in transport.calls("PATCH")]


def script(transport, raw: bytes, *, fhir=None, patch=None, name: str = NAME):
    message_path = "/messages/" + name.rsplit("/", 1)[-1]
    transport.add("GET", message_path, stored(raw, name))
    transport.add("POST", FHIR_PATH, fhir or json_response(200, {"resourceType": "Bundle"}))
    transport.add("PATCH", message_path, patch or json_response(200, {"name": name}))


# ---- the main path ----------------------------------------------------------------------------


def test_valid_message_is_fetched_raw_written_labelled_and_acknowledged(push, transport):
    script(transport, load("valid_bp_reading").payload)

    result = push(envelope())

    assert result.status == 204
    assert [(r.method, r.path.rsplit("/", 1)[-1]) for r in transport.requests] == [
        ("GET", "m-1="),
        ("POST", "fhir"),
        ("PATCH", "m-1="),
    ]
    get, post, patch = transport.requests
    assert get.params == {"view": "RAW_ONLY"}
    bundle = post.json()
    assert bundle["type"] == "transaction"
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert types == ["Patient", "Device", "Observation", "Provenance"]
    provenance = bundle["entry"][3]["resource"]
    assert provenance["entity"][0]["what"]["identifier"]["value"] == NAME
    assert patch.params == {"updateMask": "labels"}
    assert patch.json() == {"labels": {"application_outcome": "accepted", "reason": "mapped"}}
    assert result.details["control_id"] == "SYN-VALID-0001"


def test_ack_notification_is_acknowledged_and_ignored(push, transport):
    result = push(envelope(msg_type="ACK"))
    assert result.status == 204
    assert result.event == "ignored-ack-message"
    assert transport.requests == []


def test_empty_msg_type_is_not_mistaken_for_ack(push, transport):
    script(transport, load("unmappable_empty_msh9").payload)
    result = push(envelope(msg_type=""))
    assert result.status == 204
    assert patched_labels(transport) == [
        {"application_outcome": "error", "reason": "unsupported-message-type"}
    ]


def test_missing_attributes_are_tolerated(push, transport):
    script(transport, load("valid_bp_reading").payload)
    assert push(envelope(msg_type=None)).status == 204


# ---- content errors: acknowledged with labels ------------------------------------------------

EXPECTED = {
    "unmappable_unknown_obx3_code": "unknown-obx-3-code",
    "unmappable_missing_obx14": "missing-obx-14",
    "unmappable_non_numeric_obx5": "non-numeric-obx-5",
    "unmappable_msh_only": "no-observations",
    "unmappable_empty_msh9": "unsupported-message-type",
    "unmappable_bare_msh": "unsupported-message-type",
    "unmappable_unknown_version": "unsupported-version",
    "unmappable_unknown_msh9": "unsupported-message-type",
    "unmappable_empty_msh12": "unsupported-version",
}


@pytest.mark.parametrize("sample", [s.name for s in by_class("parses_but_cannot_map")])
def test_content_errors_are_acknowledged_labelled_and_write_nothing(push, transport, sample):
    script(transport, load(sample).payload)

    result = push(envelope())

    assert result.status == 204
    assert transport.calls("POST") == []  # no Observation for a message we could not map
    assert patched_labels(transport) == [
        {"application_outcome": "error", "reason": EXPECTED[sample]}
    ]


def test_stored_data_that_is_not_base64_is_an_error_outcome(push, transport):
    transport.add("GET", "/messages/m-1=", json_response(200, {"name": NAME, "data": "%%"}))
    transport.add("PATCH", "/messages/m-1=", json_response(200, {"name": NAME}))
    assert push(envelope()).status == 204
    assert patched_labels(transport) == [
        {"application_outcome": "error", "reason": "unparseable-by-mapper"}
    ]


def test_stored_message_without_data_is_an_error_outcome(push, transport):
    transport.add("GET", "/messages/m-1=", json_response(200, {"name": NAME}))
    transport.add("PATCH", "/messages/m-1=", json_response(200, {"name": NAME}))
    assert push(envelope()).status == 204
    assert patched_labels(transport)[0]["reason"] == "unparseable-by-mapper"


def test_unknown_patient_is_acknowledged_as_error(push, transport):
    raw = load("valid_bp_reading").payload.replace(b"SYN-000123", b"SYN-000999")
    script(transport, raw)
    assert push(envelope()).status == 204
    assert transport.calls("POST") == []
    assert patched_labels(transport) == [
        {"application_outcome": "error", "reason": "unknown-patient"}
    ]


# ---- the message is gone: acknowledged -------------------------------------------------------


def test_message_deleted_before_fetch_is_acknowledged_without_retry(push, transport):
    transport.add("GET", "/messages/m-1=", json_response(404, {"error": {"message": "nf"}}))
    result = push(envelope())
    assert result.status == 204
    assert result.event == "message-gone"
    assert [r.method for r in transport.requests] == ["GET"]


def test_message_deleted_before_the_label_patch_is_acknowledged(push, transport):
    script(transport, load("valid_bp_reading").payload, patch=json_response(404, {}))
    assert push(envelope()).status == 204


# ---- infrastructure failures: retried, nothing misleading written -----------------------------

TRANSIENT_FHIR = [
    json_response(503, {"error": {"message": "unavailable"}}),
    json_response(500, {}),
    json_response(429, {"error": {"message": "quota"}}),
    json_response(403, {"error": {"message": "denied"}}),  # IAM still propagating
    json_response(401, {"error": {"message": "unauthenticated"}}),
    json_response(408, {"error": {"message": "request timeout"}}),
    json_response(404, {"error": {"message": "no store"}}),
    json_response(409, {"resourceType": "OperationOutcome", "issue": []}),
    RetryableError("POST fhir", None),  # network failure or timeout
]


@pytest.mark.parametrize("failure", TRANSIENT_FHIR)
def test_transient_fhir_failure_is_retried_and_patches_nothing(push, transport, failure):
    script(transport, load("valid_bp_reading").payload, fhir=failure)

    result = push(envelope())

    assert result.status == 503
    # No label: the message stays "pending" rather than claiming an outcome it did not reach.
    assert transport.calls("PATCH") == []


@pytest.mark.parametrize(
    "failure",
    [
        json_response(500, {}),
        json_response(403, {}),
        json_response(429, {}),
        RetryableError("GET message", None),
    ],
)
def test_fetch_failure_is_retried(push, transport, failure):
    transport.add("GET", "/messages/m-1=", failure)
    assert push(envelope()).status == 503
    assert [r.method for r in transport.requests] == ["GET"]


@pytest.mark.parametrize(
    "failure", [json_response(503, {}), json_response(403, {}), json_response(400, {})]
)
def test_label_patch_failure_is_retried(push, transport, failure):
    # Including a 400: it cannot heal by itself, so after ~10 attempts the notification is
    # dead-lettered and the message stays "pending" until the dead-letter queue is replayed.
    script(transport, load("unmappable_missing_obx14").payload, patch=failure)
    assert push(envelope()).status == 503


# ---- FHIR refuses the Bundle: an error outcome, acknowledged ---------------------------------


def test_fhir_4xx_is_an_error_outcome_with_a_safe_log_summary(push, transport):
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "processing",
                "diagnostics": "SYN-000123 TESTPATIENT 120",
                "expression": ["Bundle.entry[2].resource"],
            }
        ],
    }
    script(transport, load("valid_bp_reading").payload, fhir=json_response(422, outcome))

    result = push(envelope())

    assert result.status == 204
    assert patched_labels(transport) == [
        {"application_outcome": "error", "reason": "fhir-rejected"}
    ]
    assert result.details["fhir_status"] == 422
    assert result.details["fhir_issues"] == [
        {"severity": "error", "code": "processing", "expression": ["Bundle.entry[2].resource"]}
    ]
    assert "SYN-000123" not in repr(result)


# ---- redelivery ------------------------------------------------------------------------------


def test_redelivery_rewrites_the_same_observation_and_overwrites_both_labels(push, transport):
    script(transport, load("valid_bp_reading").payload)

    first = push(envelope(attempt=1))
    second = push(envelope(attempt=2))

    assert first.status == second.status == 204
    one, two = (r.json() for r in transport.calls("POST"))
    assert one["entry"][2]["request"] == two["entry"][2]["request"]
    assert one["entry"][2]["request"]["url"] == (
        "Observation?identifier=urn:pulsebridge:observation|a704437f-1123-5b1d-9fed-3d05be2c2e36"
    )
    assert one["entry"][3]["request"] == two["entry"][3]["request"]  # Provenance by id
    assert patched_labels(transport) == [
        {"application_outcome": "accepted", "reason": "mapped"},
        {"application_outcome": "accepted", "reason": "mapped"},
    ]


def test_a_resend_stored_as_a_new_message_maps_to_the_same_observation(push, transport):
    # Same reading, new MSH-7: a new stored message (new name, new notification), same MSH-10.
    raw = load("valid_bp_reading").payload
    resent = raw.replace(b"|20261113205329+0000||ORU", b"|20261113210000+0000||ORU", 1)
    other = f"{STORE}/messages/m-2="
    script(transport, raw)
    script(transport, resent, name=other)

    push(envelope())
    push(envelope(other))

    one, two = (r.json() for r in transport.calls("POST"))
    assert one["entry"][2]["request"] == two["entry"][2]["request"]
    # The Provenance of each points at its own stored message.
    assert two["entry"][3]["resource"]["entity"][0]["what"]["identifier"]["value"] == other


def test_reprocessing_after_an_error_replaces_the_stale_reason(push, transport):
    # Both keys go in every patch, so a message first labelled error/fhir-rejected and then
    # mapped on a later delivery cannot keep the old reason next to "accepted".
    transport.add("GET", "/messages/m-1=", stored(load("valid_bp_reading").payload))
    transport.add("POST", FHIR_PATH, json_response(400, {}), json_response(200, {}))
    transport.add("PATCH", "/messages/m-1=", json_response(200, {"name": NAME}))
    push(envelope())
    push(envelope())
    assert patched_labels(transport) == [
        {"application_outcome": "error", "reason": "fhir-rejected"},
        {"application_outcome": "accepted", "reason": "mapped"},
    ]


# ---- envelopes that are not a usable Pub/Sub push --------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        None,  # not JSON at all
        [],
        {},
        {"message": "x"},
        {"message": {}},
        {"message": {"data": ""}},
        {"message": {"data": "%%%not-base64"}},
        {"message": {"data": b64(b"\xff\xfe")}},  # not UTF-8
        # Base64 with a stray character: a lenient decoder would skip it and read a valid name.
        {"message": {"data": b64(NAME.encode())[:8] + "!" + b64(NAME.encode())[8:]}},
        {"message": {"data": b64(NAME.encode()), "attributes": ["msgType"]}},
    ],
)
def test_malformed_envelope_is_rejected_without_any_call(push, transport, body):
    result = push(body)
    assert result.status == 400
    assert transport.requests == []


@pytest.mark.parametrize(
    "name",
    [
        "projects/demo-project/locations/us-central1/datasets/demo/hl7V2Stores/other/messages/m=",
        "projects/demo-project/locations/us-central1/datasets/demo/hl7V2Stores/main2/messages/m=",
        "projects/elsewhere/locations/us-central1/datasets/demo/hl7V2Stores/main/messages/m=",
        "projects/demo-project/locations/us-central1/datasets/other/hl7V2Stores/main/messages/m=",
    ],
)
def test_a_well_formed_message_of_another_store_is_acknowledged_with_a_warning(
    push, transport, name
):
    # It can never succeed here, and refusing it would slow delivery for the whole
    # subscription; acknowledge, fetch nothing, and log a warning.
    result = push(envelope(name))
    assert (result.status, result.event, result.warning) == (204, "ignored-foreign-message", True)
    assert transport.requests == []


@pytest.mark.parametrize(
    "name",
    [
        f"{STORE}/messages/../../fhirStores/fhir",
        f"{STORE}/messages/..%2F..%2Fx",  # encoded traversal
        f"{STORE}/messages/a/b",
        f"{STORE}/messages/",
        "hello",
    ],
)
def test_data_that_is_not_a_message_name_is_rejected_without_any_call(push, transport, name):
    result = push(envelope(name))
    assert result.status == 400
    assert transport.requests == []


def test_a_processed_message_is_not_a_warning(push, transport):
    script(transport, load("valid_bp_reading").payload)
    assert push(envelope()).warning is False


def test_logged_control_id_is_bounded(push, transport):
    raw = load("valid_bp_reading").payload.replace(b"|SYN-VALID-0001|", b"|" + b"C" * 199 + b"|")
    script(transport, raw)
    result = push(envelope())
    assert result.status == 204
    assert result.details["control_id"] == "C" * 64


def test_parse_envelope_reads_the_documented_push_shape():
    note = parse_envelope(envelope(attempt=3))
    assert note.message_name == NAME
    assert note.msg_type == "ORU"
    assert note.pubsub_message_id == "1001"
    assert note.delivery_attempt == 3


# ---- labels stay inside the documented limits -------------------------------------------------

LABEL = re.compile(r"[a-z][a-z0-9_-]{0,62}")
VALUE = re.compile(r"[a-z0-9_-]{1,63}")


@pytest.mark.parametrize(
    "sample",
    [
        s.name
        for cls in ("valid", "structurally_invalid", "parses_but_cannot_map")
        for s in by_class(cls)
    ],
)
def test_every_patch_keeps_to_label_limits_and_touches_only_our_two_keys(push, transport, sample):
    script(transport, load(sample).payload)
    push(envelope())
    (labels,) = patched_labels(transport)
    assert set(labels) == {"application_outcome", "reason"}
    for key, value in labels.items():
        assert LABEL.fullmatch(key) and VALUE.fullmatch(value)
        assert len(value.encode()) <= 63
