"""The Flask app: ``POST /sync``, ``GET /status``, crash containment, and what is logged."""

import logging

import pytest
from health_sync import app as app_module
from health_sync.app import STATUS_PATH, SYNC_PATH, create_app
from health_sync.testing import (
    ACCESS_TOKEN,
    ACCESS_TOKEN_2,
    FHIR_PATH,
    PATIENT_ID,
    fhir_ok,
    formatted_logs,
    leaked,
    list_response,
    oauth_error,
    rollup_page,
    token_response,
    window,
)
from pulsebridge_gcp.testing import json_response


@pytest.fixture
def http(world):
    def make(**config):
        return create_app(world.config(**config), world.deps()).test_client()

    return make


# ---- POST /sync ------------------------------------------------------------------------------


def test_sync_route_runs_a_sync_and_answers_with_the_report(world, http):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", rollup_page([window("2026-01-15T14:05:00Z", 70, 72, 75)]))
    world.google.add("GET", "/dataPoints", list_response())
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    response = http().post(SYNC_PATH)
    assert response.status_code == 200
    body = response.get_json()
    assert (body["outcome"], body["windows_written"]) == ("synced", 1)
    assert body["ranges"] == [{"start": "2026-01-15T12:05:00Z", "end": "2026-01-15T15:05:00Z"}]
    assert body["complete_before"] == "2026-01-15T14:05:00Z"


def test_reconnect_needed_answers_200_so_scheduler_does_not_retry(world, http):
    world.google.add("POST", "/token", oauth_error("invalid_grant"))
    response = http().post(SYNC_PATH)
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "reconnect-needed"


def test_a_transient_failure_answers_503(world, http):
    world.google.add("POST", "/token", oauth_error("backend", 503))
    response = http(max_attempts=2).post(SYNC_PATH)
    assert response.status_code == 503
    assert response.get_json()["outcome"] == "google-unavailable"


def test_only_the_two_routes_exist(world, http):
    client = http()
    assert client.get(SYNC_PATH).status_code == 405
    assert client.post(STATUS_PATH).status_code == 405
    assert client.get("/").status_code == 404


def test_a_bug_answers_500_and_logs_the_type_only(world, http, monkeypatch, caplog):
    def broken(*_a, **_k):
        raise KeyError(PATIENT_ID)

    monkeypatch.setattr(app_module, "run_sync", broken)
    with caplog.at_level(logging.ERROR):
        assert http().post(SYNC_PATH).status_code == 500
    assert "KeyError" in caplog.text
    assert PATIENT_ID not in formatted_logs(caplog.records)


# ---- GET /status -----------------------------------------------------------------------------


def test_status_connected(world, http):
    world.google.add("POST", "/token", token_response())
    response = http().get(STATUS_PATH)
    assert response.status_code == 200
    assert response.get_json() == {
        "googleHealth": "connected",
        "reason": None,
        "checkedAt": "2026-01-15T15:07:30Z",
    }


def test_status_reconnect_needed(world, http):
    world.google.add("POST", "/token", oauth_error("invalid_grant"))
    response = http().get(STATUS_PATH)
    assert response.status_code == 200
    assert response.get_json()["googleHealth"] == "reconnect-needed"
    assert response.get_json()["reason"] == "refresh-token-invalid"
    assert world.token_calls() == 1


def test_status_not_configured(world, http):
    world.write_secrets(client_id=None)
    response = http().get(STATUS_PATH)
    assert response.status_code == 200
    assert response.get_json()["googleHealth"] == "not-configured"
    assert response.get_json()["reason"] == "secrets-missing:client-id"
    assert world.google.requests == []


def test_status_unknown_when_the_check_cannot_finish(world, http):
    world.google.add("POST", "/token", oauth_error("backend", 503))
    response = http().get(STATUS_PATH)
    assert response.status_code == 503
    assert response.get_json()["googleHealth"] == "unknown"
    assert world.token_calls() == 2  # one retry at most


def test_status_answers_are_cached_briefly(world, http):
    world.google.add("POST", "/token", token_response())
    client = http(status_cache_seconds=60.0)
    client.get(STATUS_PATH)
    client.get(STATUS_PATH)
    assert world.token_calls() == 1
    world.clock.mono += 60
    client.get(STATUS_PATH)
    assert world.token_calls() == 2


def test_a_sync_that_finds_reconnect_needed_updates_the_status_at_once(world, http):
    world.google.add("POST", "/token", token_response(), oauth_error("invalid_grant"))
    world.google.add("POST", ":rollUp", rollup_page([]))
    client = http(status_cache_seconds=600.0)
    assert client.get(STATUS_PATH).get_json()["googleHealth"] == "connected"
    client.post(SYNC_PATH)
    assert client.get(STATUS_PATH).get_json()["googleHealth"] == "reconnect-needed"
    assert world.token_calls() == 2


def test_status_responses_carry_no_secret_material(world, http):
    world.google.add("POST", "/token", token_response())
    body = http().get(STATUS_PATH).get_data(as_text=True)
    assert leaked(body) == []


# ---- logs ------------------------------------------------------------------------------------

# Distinctive synthetic values, so a leak cannot hide behind a common number.
DISTINCT = ("187.123", "176.5", "198.25")


def test_no_log_line_or_answer_carries_a_token_a_secret_or_a_value(world, http, caplog):
    items = [window("2026-01-15T14:05:00Z", 176.5, 187.123, 198.25)]
    world.google.add(
        "POST",
        "/token",
        token_response(ACCESS_TOKEN),
        token_response(ACCESS_TOKEN_2),  # after the 401
        oauth_error("invalid_grant"),
    )
    world.google.add(
        "POST", ":rollUp", json_response(401, {}), json_response(503, {}), rollup_page(items)
    )
    world.google.add("GET", "/dataPoints", list_response("Synthetic Watch 1"))
    world.fhir_transport.add("POST", FHIR_PATH, json_response(503, {}), fhir_ok())
    client = http()
    with caplog.at_level(logging.DEBUG):
        first = client.post(SYNC_PATH)  # 401 -> refresh, 503 -> retry, FHIR 503 -> retry
        second = client.post(SYNC_PATH)  # invalid_grant
        status = client.get(STATUS_PATH)
    assert first.get_json()["outcome"] == "synced"
    assert second.get_json()["outcome"] == "reconnect-needed"
    logs = formatted_logs(caplog.records)
    assert "google-retry" in logs and "fhir-retry" in logs and "sync-run" in logs
    for text in (
        logs,
        first.get_data(as_text=True),
        second.get_data(as_text=True),
        status.get_data(as_text=True),
    ):
        assert leaked(text, *DISTINCT, PATIENT_ID, "Synthetic Watch 1") == []
