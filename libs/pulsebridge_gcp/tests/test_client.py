"""HealthcareClient against a scripted transport: requests sent, results parsed, errors typed.

No test makes a network call or needs credentials. The response bodies follow the shapes recorded
in docs/spikes/s5.md; every value in them is synthetic.
"""

import base64
import json

import pytest
from pulsebridge_gcp import (
    ClientError,
    ConflictError,
    HealthcareClient,
    MalformedResponseError,
    NotFoundError,
    PermissionDeniedError,
    RetryableError,
    operation_outcome_summary,
)
from pulsebridge_gcp.testing import FakeTransport, json_response
from pulsebridge_gcp.transport import HttpResponse

BASE = "https://healthcare.googleapis.com/v1"
DATASET = "projects/demo-project/locations/us-central1/datasets/demo"
STORE = f"{DATASET}/hl7V2Stores/main"
MESSAGE = f"{STORE}/messages/aBc-_9xYz="
FHIR = f"{BASE}/{DATASET}/fhirStores/fhir/fhir"
ER7 = (
    b"MSH|^~\\&|CUFFSIM|PULSEBRIDGE|PHR|PULSEBRIDGE|20261113205329+0000||ORU^R01^ORU_R01"
    b"|C1|P|2.5.1\r"
)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> HealthcareClient:
    return HealthcareClient(
        project="demo-project", location="us-central1", dataset="demo", transport=transport
    )


# ---- FHIR ------------------------------------------------------------------------------------


def test_execute_bundle_posts_fhir_json_to_the_store_base(client, transport):
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    transport.add("POST", "/fhirStores/fhir/fhir", json_response(200, {"resourceType": "Bundle"}))

    assert client.execute_bundle("fhir", bundle) == {"resourceType": "Bundle"}

    (sent,) = transport.requests
    assert sent.method == "POST"
    assert sent.url == FHIR
    assert sent.headers["Content-Type"] == "application/fhir+json; charset=utf-8"
    assert sent.json() == bundle
    assert sent.timeout == 15.0


def test_execute_bundle_refuses_a_non_bundle_without_sending(client, transport):
    with pytest.raises(ValueError, match="Bundle"):
        client.execute_bundle("fhir", {"resourceType": "Observation"})
    assert transport.requests == []


def test_read_search_and_put_build_the_fhir_rest_paths(client, transport):
    transport.add("GET", "/fhir/Patient/p-1", json_response(200, {"resourceType": "Patient"}))
    transport.add("GET", "/fhir/Observation", json_response(200, {"resourceType": "Bundle"}))
    transport.add("PUT", "/fhir/Provenance/v-1", json_response(200, {"resourceType": "Provenance"}))

    client.read_resource("fhir", "Patient", "p-1")
    client.search("fhir", "Observation", {"code": "http://loinc.org|85354-9", "_count": "10"})
    client.put_resource("fhir", "Provenance", "v-1", {"resourceType": "Provenance", "id": "v-1"})

    read, search, put = transport.requests
    assert read.url == f"{FHIR}/Patient/p-1"
    assert search.url == f"{FHIR}/Observation"
    assert search.params == {"code": "http://loinc.org|85354-9", "_count": "10"}
    assert put.url == f"{FHIR}/Provenance/v-1"
    assert put.json() == {"resourceType": "Provenance", "id": "v-1"}


@pytest.mark.parametrize(
    ("resource_type", "resource_id"),
    [("Patient", "../../x"), ("Patient", "a/b"), ("patient", "p-1"), ("Patient", "")],
)
def test_path_parts_are_validated_so_input_cannot_reach_another_resource(
    client, transport, resource_type, resource_id
):
    with pytest.raises(ValueError):
        client.read_resource("fhir", resource_type, resource_id)
    assert transport.requests == []


def test_put_refuses_an_id_that_differs_from_the_url(client):
    with pytest.raises(ValueError, match="differs"):
        client.put_resource("fhir", "Patient", "a", {"resourceType": "Patient", "id": "b"})


def test_next_page_follows_a_link_inside_the_store(client, transport):
    transport.add("GET", "/fhir/Observation", json_response(200, {"resourceType": "Bundle"}))
    page = {"link": [{"relation": "next", "url": f"{FHIR}/Observation?_page_token=t2"}]}

    assert client.next_page("fhir", page) == {"resourceType": "Bundle"}
    assert transport.requests[0].url == f"{FHIR}/Observation?_page_token=t2"
    assert client.next_page("fhir", {"link": [{"relation": "self", "url": FHIR}]}) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.example/v1/fhir/Observation?page=2",
        f"{BASE}/{DATASET}/fhirStores/other/fhir/Observation",
        f"http://healthcare.googleapis.com/v1/{DATASET}/fhirStores/fhir/fhir/Observation",
    ],
)
def test_next_page_refuses_a_link_outside_the_store(client, transport, url):
    with pytest.raises(MalformedResponseError):
        client.next_page("fhir", {"link": [{"relation": "next", "url": url}]})
    assert transport.requests == []


# ---- errors ----------------------------------------------------------------------------------

OUTCOME = {
    "resourceType": "OperationOutcome",
    "issue": [
        {
            "severity": "error",
            "code": "invalid",
            "diagnostics": "value 120 for SYN-000123 is bad",
            "details": {"text": "TESTPATIENT"},
            "expression": ["Observation.component[0]"],
        }
    ],
}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, NotFoundError),
        (401, PermissionDeniedError),
        (403, PermissionDeniedError),
        (408, RetryableError),
        (429, RetryableError),
        (500, RetryableError),
        (503, RetryableError),
        (400, ClientError),
        (412, ClientError),
        (422, ClientError),
        (409, ConflictError),
    ],
)
def test_status_codes_map_to_typed_errors(client, transport, status, expected):
    transport.add("POST", "/fhir", json_response(status, {"error": {"message": "x"}}))
    with pytest.raises(expected) as caught:
        client.execute_bundle("fhir", {"resourceType": "Bundle"})
    assert caught.value.status == status
    # The retry decision depends on the class; these must not overlap.
    assert isinstance(caught.value, RetryableError) == (expected is RetryableError)
    assert isinstance(caught.value, ClientError) == (expected in (ClientError, ConflictError))


def test_a_fhir_4xx_carries_the_operation_outcome_but_not_in_its_message(client, transport):
    transport.add("POST", "/fhir", json_response(400, OUTCOME))
    with pytest.raises(ClientError) as caught:
        client.execute_bundle("fhir", {"resourceType": "Bundle"})
    assert caught.value.operation_outcome == OUTCOME
    assert str(caught.value) == f"POST {DATASET}/fhirStores/fhir/fhir: HTTP 400"
    assert "SYN-000123" not in str(caught.value)


def test_google_error_text_is_kept_aside_not_in_the_message(client, transport):
    transport.add("GET", "/messages/aBc-_9xYz=", json_response(404, {"error": {"message": "gone"}}))
    with pytest.raises(NotFoundError) as caught:
        client.get_message(MESSAGE)
    assert caught.value.server_message == "gone"
    assert "gone" not in str(caught.value)


def test_operation_outcome_summary_drops_free_text():
    assert operation_outcome_summary(OUTCOME) == [
        {"severity": "error", "code": "invalid", "expression": ["Observation.component[0]"]}
    ]
    assert operation_outcome_summary(None) == []
    assert operation_outcome_summary({"issue": "nonsense"}) == []


def test_a_2xx_that_is_not_json_is_a_malformed_response(client, transport):
    transport.add("POST", "/fhir", HttpResponse(200, b"<html>"))
    with pytest.raises(MalformedResponseError):
        client.execute_bundle("fhir", {"resourceType": "Bundle"})


def test_a_transport_failure_propagates_as_retryable(client, transport):
    transport.add("POST", "/fhir", RetryableError("POST x", None))
    with pytest.raises(RetryableError):
        client.execute_bundle("fhir", {"resourceType": "Bundle"})


# ---- HL7v2 -----------------------------------------------------------------------------------

STORED = {
    "name": MESSAGE,
    "data": b64(ER7),
    "sendFacility": "PULSEBRIDGE",
    "sendTime": "2026-11-13T20:53:29Z",
    "messageType": "ORU",
    "createTime": "2026-11-13T20:53:30.1Z",
    "patientIds": [{"value": "SYN-000123", "type": "MR"}],
    "labels": {"transport": "https"},
}


def test_get_message_asks_for_the_view_and_decodes_data(client, transport):
    transport.add("GET", "/messages/aBc-_9xYz=", json_response(200, STORED))

    message = client.get_message(MESSAGE, view="RAW_ONLY")

    assert transport.requests[0].url == f"{BASE}/{MESSAGE}"
    assert transport.requests[0].params == {"view": "RAW_ONLY"}
    assert message.name == MESSAGE
    assert message.data == ER7
    assert message.message_type == "ORU"
    assert message.send_facility == "PULSEBRIDGE"
    assert message.patient_ids[0].value == "SYN-000123"
    assert message.patient_ids[0].type == "MR"
    assert message.labels == {"transport": "https"}


def test_get_message_without_data_or_labels(client, transport):
    transport.add("GET", "/messages/aBc-_9xYz=", json_response(200, {"name": MESSAGE}))
    message = client.get_message(MESSAGE, view="BASIC")
    assert message.data is None
    assert message.labels == {}


def test_stored_data_that_is_not_base64_is_a_malformed_response(client, transport):
    transport.add("GET", "/messages/aBc-_9xYz=", json_response(200, {**STORED, "data": "%%%"}))
    with pytest.raises(MalformedResponseError):
        client.get_message(MESSAGE)


@pytest.mark.parametrize(
    "name",
    [
        f"{STORE}/messages/../../fhirStores/x",
        f"{DATASET}/fhirStores/fhir/fhir/Patient/1",
        "messages/abc",
        f"{MESSAGE}?view=FULL",
    ],
)
def test_message_names_are_validated(client, transport, name):
    with pytest.raises(ValueError):
        client.get_message(name)
    assert transport.requests == []


def test_is_message_in_store(client):
    assert client.is_message_in_store(MESSAGE, "main")
    assert not client.is_message_in_store(MESSAGE, "other")
    other_dataset = MESSAGE.replace("/datasets/demo/", "/datasets/elsewhere/")
    assert not client.is_message_in_store(other_dataset, "main")
    assert not client.is_message_in_store(f"{STORE}/messages/a/b", "main")


PAGE_1, PAGE_2 = "cursor-1", "cursor-2"  # opaque paging cursors


def test_list_messages_passes_filter_order_and_paging(client, transport):
    transport.add(
        "GET",
        "/hl7V2Stores/main/messages",
        json_response(200, {"hl7V2Messages": [{"name": MESSAGE}], "nextPageToken": PAGE_2}),
    )
    page = client.list_messages(
        "main",
        filter='labels.application_outcome = "error"',
        order_by="send_time desc",
        page_size=50,
        page_token=PAGE_1,
        view="RAW_ONLY",
    )
    assert transport.requests[0].params == {
        "view": "RAW_ONLY",
        "filter": 'labels.application_outcome = "error"',
        "orderBy": "send_time desc",
        "pageSize": "50",
        "pageToken": PAGE_1,
    }
    assert [m.name for m in page.messages] == [MESSAGE]
    assert page.next_page_token == PAGE_2


def test_iter_messages_follows_next_page_token_until_absent(client, transport):
    second = MESSAGE.replace("aBc-_9xYz=", "second=")
    transport.add(
        "GET",
        "/hl7V2Stores/main/messages",
        json_response(200, {"hl7V2Messages": [{"name": MESSAGE}], "nextPageToken": "t2"}),
        json_response(200, {"hl7V2Messages": [{"name": second}]}),
    )
    names = [m.name for m in client.iter_messages("main")]
    assert names == [MESSAGE, second]
    assert [r.params.get("pageToken") for r in transport.requests] == [None, "t2"]


def test_iter_messages_stops_at_max_pages(client, transport):
    transport.add(
        "GET",
        "/hl7V2Stores/main/messages",
        json_response(200, {"hl7V2Messages": [{"name": MESSAGE}], "nextPageToken": "again"}),
    )
    assert len(list(client.iter_messages("main", max_pages=3))) == 3
    assert len(transport.requests) == 3


def test_patch_labels_uses_update_mask_labels(client, transport):
    transport.add("PATCH", "/messages/aBc-_9xYz=", json_response(200, STORED))
    client.patch_labels(MESSAGE, {"application_outcome": "error", "reason": "missing-obx-14"})
    (sent,) = transport.requests
    assert sent.method == "PATCH"
    assert sent.params == {"updateMask": "labels"}
    assert sent.json() == {"labels": {"application_outcome": "error", "reason": "missing-obx-14"}}


@pytest.mark.parametrize(
    "labels",
    [
        {"Application_Outcome": "error"},  # uppercase key
        {"1reason": "x"},  # key must start with a letter
        {"reason": "Missing OBX-14"},  # uppercase and space in the value
        {"reason": "a/b"},
        {"reason": "x" * 64},  # 64 bytes: one over the limit
        {"k" * 64: "x"},
        {f"k{i}": "v" for i in range(65)},  # 65 labels
    ],
)
def test_patch_labels_refuses_values_outside_the_documented_limits(client, transport, labels):
    with pytest.raises(ValueError):
        client.patch_labels(MESSAGE, labels)
    assert transport.requests == []


def test_label_limits_allow_the_boundary_values(client, transport):
    transport.add("PATCH", "/messages/aBc-_9xYz=", json_response(200, STORED))
    client.patch_labels(MESSAGE, {"k" * 63: "v" * 63, "reason": ""})  # "" removes a key
    assert len(transport.requests) == 1


def test_ingest_returns_the_decoded_ack_and_stored_message(client, transport):
    ack = b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113224607||ACK|x|P|2.5.1\rMSA|AA|C1"
    transport.add(
        "POST",
        "/hl7V2Stores/main/messages:ingest",
        json_response(200, {"hl7Ack": b64(ack), "message": STORED}),
    )
    result = client.ingest("main", ER7, labels={"transport": "https"})
    assert result.hl7_ack == ack
    assert result.message.name == MESSAGE
    assert transport.requests[0].json() == {
        "message": {"data": b64(ER7), "labels": {"transport": "https"}}
    }


@pytest.mark.parametrize(("status", "cls"), [(400, ClientError), (409, ConflictError)])
def test_ingest_nack_is_a_client_error_carrying_the_nack(client, transport, status, cls):
    nack = b"MSH|^~\\&|||||20261113225233||ACK|y\rMSA|AE||Invalid MSH header"
    body = {
        "error": {
            "code": status,
            "message": "Invalid MSH header",
            "details": [
                {
                    "@type": "type.googleapis.com/google.cloud.healthcare.v1.hl7v2."
                    "IngestMessageErrorDetail",
                    "hl7Nack": b64(nack),
                }
            ],
        }
    }
    transport.add("POST", "/messages:ingest", json_response(status, body))
    with pytest.raises(cls) as caught:
        client.ingest("main", b"PID|1")
    assert caught.value.hl7_nack == nack


def test_ingest_api_error_without_nack(client, transport):
    transport.add(
        "POST", "/messages:ingest", json_response(400, {"error": {"message": "data needed"}})
    )
    with pytest.raises(ClientError) as caught:
        client.ingest("main", b"")
    assert caught.value.hl7_nack is None


def test_fake_transport_fails_loudly_on_an_unscripted_request(client):
    with pytest.raises(AssertionError, match="no route"):
        client.get_message(MESSAGE)


def test_request_body_is_compact_utf8_json(client, transport):
    transport.add("POST", "/fhir", json_response(200, {}))
    client.execute_bundle("fhir", {"resourceType": "Bundle", "x": "é"})
    assert transport.requests[0].body == json.dumps(
        {"resourceType": "Bundle", "x": "é"}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
