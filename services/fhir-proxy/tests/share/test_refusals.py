"""Refusals before OpenEMR is called: an OperationOutcome via Problem (400, 404, 422, 503), and
not one request to OpenEMR. Only these let the page say "Nothing was sent to the clinic"."""

from __future__ import annotations

import json

import pytest
from fhir_proxy.share.testing import (
    BP_ID,
    OTHER_PATIENT_STORE_ID,
    build_share_env,
    stored_observation,
    stored_patient,
)
from fhir_proxy.testing import FHIR_STORE, ORIGIN


def reason(response) -> str:  # noqa: ANN001
    body = response.get_json()
    assert body["resourceType"] == "OperationOutcome", body
    return body["issue"][0]["details"]["coding"][0]["code"]


def refused(env, response, status: int, code: str) -> dict:  # noqa: ANN001
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.mimetype == "application/fhir+json"
    assert reason(response) == code
    assert env.openemr.requests == [], "nothing may be sent to OpenEMR"
    assert "steps" not in response.get_json()
    return response.get_json()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"null",
        b"[]",
        b'"pb"',
        b"{}",
        b'{"observationId": 7}',
        b'{"observationId": ""}',
        b'{"observationId": ".."}',
        b'{"observationId": "a/b"}',
        b'{"observationId": "x", "patient": "Patient/1"}',
        b'{"observationId": "x", "observationId": "y"}',
        b'{"observationid": "x"}',
        b'{"observationId": "https://evil.example.test/x"}',
        b'{"observationId": NaN}',
        b"\xff\xfe",
        json.dumps({"observationId": "a" * 65}).encode(),
        json.dumps({"observationId": "x", "pad": "y" * 600}).encode(),  # over 512 bytes
    ],
)
def test_anything_but_exactly_one_observation_id_is_invalid_body(env, raw: bytes) -> None:
    refused(env, env.share(raw=raw), 400, "invalid-body")


def test_a_body_that_is_not_json_by_type_is_invalid_body(env) -> None:
    refused(env, env.share(content_type="text/plain"), 400, "invalid-body")


def test_unknown_and_another_patients_observation_answer_alike(env) -> None:
    other = stored_patient(OTHER_PATIENT_STORE_ID, "PB-SYN-9999")
    env.store.put(FHIR_STORE, other)
    env.store.put(
        FHIR_STORE,
        stored_observation(
            "observation-blood-pressure.json", "pb-other-bp", OTHER_PATIENT_STORE_ID
        ),
    )
    missing = refused(env, env.share(observation_id="no-such-observation"), 404, "not-found")
    theirs = refused(env, env.share(observation_id="pb-other-bp"), 404, "not-found")
    assert missing == theirs


@pytest.mark.parametrize(
    "status",
    [
        "amended",
        "preliminary",
        "entered-in-error",
        "corrected",
        "registered",
        "cancelled",
        "unknown",
    ],
)
def test_only_final_observations_are_shared(env, status: str) -> None:
    observation = stored_observation("observation-blood-pressure.json", BP_ID)
    observation["status"] = status
    env.store.put(FHIR_STORE, observation)
    body = refused(env, env.share(), 422, "observation-not-final")
    assert (
        body["issue"][0]["diagnostics"]
        == f"Only final Observations are shared; this one is {status}."
    )


def test_an_unknown_status_is_not_echoed(env) -> None:
    observation = stored_observation("observation-blood-pressure.json", BP_ID)
    observation["status"] = "<script>x</script>"
    env.store.put(FHIR_STORE, observation)
    body = refused(env, env.share(), 422, "observation-not-final")
    assert "script" not in json.dumps(body)


@pytest.mark.parametrize(
    "change",
    [
        lambda o: o["code"]["coding"][0].update(code="8310-5"),  # body temperature
        lambda o: o["code"]["coding"][0].update(system="urn:other"),
        lambda o: o["component"][0]["valueQuantity"].update(code="mmHg"),  # not UCUM's mm[Hg]
        lambda o: o["component"].pop(1),  # no diastolic
        lambda o: o["component"][0]["valueQuantity"].update(value="121"),
    ],
)
def test_only_heart_rate_and_blood_pressure_are_shared(env, change) -> None:  # noqa: ANN001
    observation = stored_observation("observation-blood-pressure.json", BP_ID)
    change(observation)
    env.store.put(FHIR_STORE, observation)
    refused(env, env.share(), 422, "observation-not-shareable")


def test_a_heart_rate_without_ucum_per_minute_is_not_shareable(env) -> None:
    observation = stored_observation("observation-heart-rate.json", "pb-hr-x")
    observation["valueQuantity"]["code"] = "{beats}/min"  # S1: OpenEMR refuses it
    env.store.put(FHIR_STORE, observation)
    refused(env, env.share(observation_id="pb-hr-x"), 422, "observation-not-shareable")


@pytest.mark.parametrize("value", ["2026-01-01", "2026-01-01T09:30:00", "yesterday", None])
def test_a_reading_without_a_zoned_time_is_not_found(env, value) -> None:  # noqa: ANN001
    # T47: a reading whose effective time cannot be placed is not inside the shown window (no
    # date search finds it either), so it answers like a missing one, before any 422 could
    # tell anything about it. Nothing is sent.
    observation = stored_observation("observation-blood-pressure.json", BP_ID)
    observation["effectiveDateTime"] = value
    env.store.put(FHIR_STORE, observation)
    refused(env, env.share(), 404, "not-found")


@pytest.mark.parametrize(
    ("change", "missing"),
    [
        (lambda p: p.pop("birthDate"), "birthDate"),
        (lambda p: p.pop("gender"), "gender"),
        (lambda p: p["identifier"][0].pop("type"), "a PT-typed identifier"),
        (lambda p: p["name"][0].update(use="usual"), "a name with use 'official'"),
    ],
)
def test_an_incomplete_patient_is_refused_with_what_is_missing(env, change, missing) -> None:  # noqa: ANN001
    patient = stored_patient()
    change(patient)
    env.store.put(FHIR_STORE, patient)
    body = refused(env, env.share(), 422, "patient-incomplete")
    assert missing in body["issue"][0]["diagnostics"]
    assert "Example" not in json.dumps(body) and "PB-SYN" not in json.dumps(body)


def test_not_configured_is_503_and_the_probe_says_so(tmp_path, client_key, server_cert) -> None:
    env = build_share_env(tmp_path, client_key, server_cert, configured=False)
    body = refused(env, env.share(), 503, "share-not-configured")
    assert (
        body["issue"][0]["diagnostics"]
        == "Sharing with the clinic is not configured on the server."
    )
    status = env.harness.get("/status").get_json()
    assert (
        status["openemr"]["reachable"] is False and status["openemr"]["detail"] == "not-configured"
    )


def test_the_route_needs_the_session_token(env) -> None:
    # From the page's own origin but without the __session cookie.
    response = env.harness.client.post(
        "/share", json={"observationId": BP_ID}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 401 and env.openemr.requests == []
    # Without an allowed Origin it is refused before the cookie is even looked at.
    response = env.harness.client.post(
        "/share", json={"observationId": BP_ID}, headers=env.harness.auth() | {"Origin": "null"}
    )
    assert response.status_code == 403 and env.openemr.requests == []


def test_the_share_route_is_listed_as_a_view_not_a_fhir_interaction(env) -> None:
    statement = env.harness.get("/fhir/metadata").get_json()
    assert "`POST /share`" in statement["rest"][0]["documentation"]
    types = {r["type"] for r in statement["rest"][0]["resource"]}
    assert types == {"Observation"}
    interactions = {i["code"] for r in statement["rest"][0]["resource"] for i in r["interaction"]}
    assert "create" not in interactions
