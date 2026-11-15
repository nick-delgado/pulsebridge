"""The Flask entry point: route, status codes, crash containment and log lines."""

import base64
import json
import logging

import pytest
from pulsebridge_gcp import HealthcareClient
from pulsebridge_gcp.testing import FakeTransport, json_response
from pulsebridge_hl7v2.samples import load
from v2_to_fhir import app as app_module
from v2_to_fhir.app import PUSH_PATH, _JsonFormatter, create_app
from v2_to_fhir.config import ServiceConfig
from v2_to_fhir.mapping import MappingConfig

STORE = "projects/demo-project/locations/us-central1/datasets/demo/hl7V2Stores/main"
NAME = f"{STORE}/messages/m-1="
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


ENVELOPE = {"message": {"data": b64(NAME.encode()), "attributes": {"msgType": "ORU"}}}


@pytest.fixture
def transport():
    t = FakeTransport()
    raw = load("valid_bp_reading").payload
    t.add("GET", "/messages/m-1=", json_response(200, {"name": NAME, "data": b64(raw)}))
    t.add("POST", "/fhir", json_response(200, {}))
    t.add("PATCH", "/messages/m-1=", json_response(200, {"name": NAME}))
    return t


@pytest.fixture
def http(transport):
    client = HealthcareClient(
        project="demo-project", location="us-central1", dataset="demo", transport=transport
    )
    return create_app(CONFIG, client).test_client()


def test_push_route_acknowledges_with_204(http, transport):
    response = http.post(PUSH_PATH, json=ENVELOPE)
    assert response.status_code == 204
    assert [r.method for r in transport.requests] == ["GET", "POST", "PATCH"]


def test_non_json_body_is_400(http, transport):
    response = http.post(PUSH_PATH, data=b"not json", content_type="text/plain")
    assert response.status_code == 400
    assert transport.requests == []


def test_only_post_on_the_push_path(http):
    assert http.get(PUSH_PATH).status_code == 405
    assert http.post("/").status_code == 404


def test_a_handler_bug_answers_500_and_does_not_crash_the_worker(http, monkeypatch, caplog):
    def broken(*_args, **_kwargs):
        raise KeyError("SYN-000123")

    monkeypatch.setattr(app_module, "handle_push", broken)
    with caplog.at_level(logging.ERROR):
        assert http.post(PUSH_PATH, json=ENVELOPE).status_code == 500
    # Logged by type only: Flask's own handler would print the traceback, with the exception
    # text, which can quote message content.
    assert [r.fields["event"] for r in caplog.records if hasattr(r, "fields")] == ["bug"]
    assert "KeyError" in caplog.text
    assert "SYN-000123" not in caplog.text
    monkeypatch.undo()
    assert http.post(PUSH_PATH, json=ENVELOPE).status_code == 204  # the app still serves


def test_log_line_is_structured_and_free_of_patient_data(http, caplog):
    with caplog.at_level(logging.INFO, logger="v2_to_fhir"):
        http.post(PUSH_PATH, json=ENVELOPE)
    (record,) = [r for r in caplog.records if r.name == "v2_to_fhir"]
    line = json.loads(_JsonFormatter().format(record))
    assert line["severity"] == "INFO"
    assert line["event"] == "processed"
    assert line["http_status"] == 204
    assert line["application_outcome"] == "accepted"
    assert line["reason"] == "mapped"
    assert line["hl7v2_message"] == "m-1="
    assert line["control_id"] == "SYN-VALID-0001"
    text = json.dumps(line)
    for phi in ("SYN-000123", "TESTPATIENT", "SYNTHIA", "120"):
        assert phi not in text
