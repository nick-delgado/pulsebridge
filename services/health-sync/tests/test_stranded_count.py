"""T39: the stranded-preliminary count uses a search Cloud Healthcare supports.

Cloud Healthcare refuses ``_summary`` (400, seen on the first dev deploy by fhir-proxy). The
count is therefore ``_total=accurate&_count=0``, read from ``Bundle.total``; a missing or odd
``total`` is a failure, never zero. ``FhirFake`` refuses ``_summary`` the same way, so a count
that goes back to ``_summary=count`` fails these tests. All data is synthetic.
"""

from __future__ import annotations

import logging

import pytest
from health_sync.stored import count_stranded
from health_sync.sync import run_sync, split_range, sync_range
from health_sync.testing import (
    FHIR_PATH,
    FhirFake,
    fhir_ok,
    formatted_logs,
    list_response,
    rollup_page,
    token_response,
    utc,
    window,
)
from pulsebridge_fhir.stored import StoredReadError
from pulsebridge_gcp import ClientError, HealthcareClient
from pulsebridge_gcp.testing import json_response

W1300 = window("2026-01-15T13:00:00Z", 60, 61, 62)
BEFORE = utc("2026-01-15T12:05:00Z")


def client(transport: FhirFake) -> HealthcareClient:
    return HealthcareClient(
        project="demo-project", location="us-central1", dataset="demo", transport=transport
    )


def run(world):
    config = world.config()
    return run_sync(
        config,
        world.deps(),
        ranges=split_range(sync_range(world.clock.now, config.lookback)),
        deadline_seconds=config.run_deadline,
    )


def google(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", rollup_page([W1300]))
    world.google.add("GET", "/dataPoints", list_response())


# ---- count_stranded against the fake store -----------------------------------------------------


def test_the_count_is_the_searchset_total_of_a_zero_entry_page():
    transport = FhirFake(stranded=7)
    assert count_stranded(client(transport), "fhir", "Patient/p-7", BEFORE) == 7
    (request,) = transport.requests
    assert request.path.endswith("/fhir/Observation")
    assert request.param_pairs == [
        ("subject", "Patient/p-7"),
        ("code", "http://loinc.org|8867-4"),
        ("status", "preliminary"),
        ("date", "lt2026-01-15T12:05:00Z"),
        ("_total", "accurate"),
        ("_count", "0"),
    ]
    assert "_summary" not in request.params


@pytest.mark.parametrize(
    "bundle",
    [
        {"resourceType": "Bundle", "type": "searchset"},  # no total
        {"resourceType": "Bundle", "type": "searchset", "total": None},
        {"resourceType": "Bundle", "type": "searchset", "total": "5"},
        {"resourceType": "Bundle", "type": "searchset", "total": 5.0},
        {"resourceType": "Bundle", "type": "searchset", "total": True},
        {"resourceType": "Bundle", "type": "searchset", "total": -1},
    ],
    ids=["missing", "null", "string", "float", "bool", "negative"],
)
def test_a_missing_or_non_numeric_total_is_a_failure_not_zero(bundle):
    transport = FhirFake(count_failure=json_response(200, bundle))
    with pytest.raises(StoredReadError):
        count_stranded(client(transport), "fhir", "Patient/p-7", BEFORE)


def test_zero_is_a_count_when_the_store_says_zero():
    assert count_stranded(client(FhirFake(stranded=0)), "fhir", "Patient/p-7", BEFORE) == 0


# ---- the fake is as strict as the real store where that is known -------------------------------


def test_the_fake_refuses_summary_as_cloud_healthcare_does():
    with pytest.raises(ClientError) as raised:
        client(FhirFake(stranded=3)).search(
            "fhir",
            "Observation",
            [("subject", "Patient/p-7"), ("status", "preliminary"), ("_summary", "count")],
        )
    assert raised.value.status == 400
    assert raised.value.operation_outcome["issue"][0]["diagnostics"] == (
        "_summary argument is not supported yet"
    )


def test_the_fake_will_not_give_a_zero_page_total_without_total_accurate():
    with pytest.raises(AssertionError, match="_total=accurate"):
        client(FhirFake()).search("fhir", "Observation", [("_count", "0")])


@pytest.mark.parametrize(
    ("resource_type", "name"),
    [("Observation", "_sort"), ("Observation", "identifier"), ("Patient", "_count")],
)
def test_the_fake_fails_loudly_on_a_parameter_it_does_not_model(resource_type, name):
    with pytest.raises(AssertionError, match="does not model"):
        client(FhirFake()).search("fhir", resource_type, [(name, "x")])


# ---- through a sync run ---------------------------------------------------------------------


def test_a_run_reports_the_count_and_logs_no_count_failure(world, caplog):
    world.fhir_transport.patient_server_id = "p-7"
    world.fhir_transport.stranded = 3
    google(world)
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    with caplog.at_level(logging.WARNING):
        report = run(world)
    assert report.stranded_preliminary == 3
    assert "stranded-count-failed" not in formatted_logs(caplog.records)


def test_a_count_answer_without_total_leaves_the_count_unknown(world, caplog):
    world.fhir_transport.patient_server_id = "p-7"
    world.fhir_transport.count_failure = json_response(
        200, {"resourceType": "Bundle", "type": "searchset"}
    )
    google(world)
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    with caplog.at_level(logging.WARNING):
        report = run(world)
    assert report.stranded_preliminary is None  # unknown, not zero
    assert report.as_dict()["stranded_preliminary"] is None
    assert "stranded-count-failed" in formatted_logs(caplog.records)
