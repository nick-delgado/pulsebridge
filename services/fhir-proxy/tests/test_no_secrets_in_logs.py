"""No log line, at any level, on any route, holds a password, a token, the patient id or a value.

Distinctive synthetic values are pushed through every route, including the error paths and an
unexpected exception, and every log record is checked both as the JSON line production writes
and as a standard formatted record with its traceback.
"""

from __future__ import annotations

import json

from fhir_proxy import traces
from fhir_proxy.testing import AUDIT_STORE, PASSWORD, PATIENT_ID, STORE_NAME, example, sample_bytes

WRONG_GUESS = "zz-distinctive-wrong-guess-7c1e"
BODY_MARKER = "zz-distinctive-body-marker-5b2a"


def test_nothing_sensitive_reaches_the_logs(harness, logs, seed_heart_rate, map_into_store) -> None:
    from datetime import UTC, datetime

    seed_heart_rate(datetime(2026, 1, 1, 5, 0, tzinfo=UTC), 2)
    name = f"{STORE_NAME}/messages/synthetic-message-0002"
    data = sample_bytes("valid_bp_reading")
    harness.fake.add_message(name, data, labels=map_into_store(name, data))
    harness.fake.put(AUDIT_STORE, example("auditevent-send-accepted.json"))
    harness.fake.put(AUDIT_STORE, example("auditevent-send-rejected.json"))
    broken = example("auditevent-send-no-ack.json")
    broken["id"] = "broken-for-probe"
    broken["outcome"] = "0"
    harness.fake.put(AUDIT_STORE, broken)

    # Login: wrong, right, rate-limited, then a token used everywhere.
    for _ in range(6):
        harness.client.post("/auth/login", json={"password": WRONG_GUESS})
    token = harness.login(PASSWORD, **{"X-Forwarded-For": "203.0.113.50"})
    auth = {"Authorization": f"Bearer {token}"}
    harness.client.get("/status", headers={"Authorization": f"Bearer {token}x"})
    harness.client.get(f"/status?access_token={token}")

    harness.sender.answer("/status", 200, {"googleHealth": "connected"})
    harness.sender.answer("/cuff/readings", 422, {"resourceType": "OperationOutcome", "issue": []})
    harness.sender.answer("/cuff/samples", 200, [])
    obs_id = next(rid for (t, rid) in harness.fake.fhir["main"] if t == "Observation")
    for path in (
        "/status",
        "/fhir/metadata",
        "/fhir/Observation?code=http://loinc.org|8867-4",
        f"/fhir/Observation?code=http://loinc.org|8867-4&{BODY_MARKER}=1",
        f"/trace/heart-rate/{obs_id}",
        f"/trace/cuff-attempts/{example('auditevent-send-accepted.json')['id']}",
        "/trace/cuff-attempts/broken-for-probe",
        "/messages",
        "/cuff/samples",
    ):
        harness.client.get(path, headers=auth)
    body = json.dumps({"systolic": 120, "diastolic": 80, "note": BODY_MARKER}).encode()
    harness.client.post("/cuff/readings", data=body, content_type="application/json", headers=auth)

    # An unexpected exception whose message quotes secrets must be logged by type only.
    def explode(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError(f"{PASSWORD} {token} {PATIENT_ID} {BODY_MARKER}")

    original = traces.heart_rate_trace
    traces.heart_rate_trace = explode
    try:
        response = harness.client.get(f"/trace/heart-rate/{obs_id}", headers=auth)
    finally:
        traces.heart_rate_trace = original
    assert response.status_code == 500
    assert PASSWORD not in response.get_data(as_text=True)

    text = logs.text()
    assert text, "the probe produced no log lines at all"
    for secret in (PASSWORD, WRONG_GUESS, token, token.split(".")[2], PATIENT_ID, BODY_MARKER):
        assert secret not in text, f"leaked: {secret[:12]}..."
    for value in ("63.5", "SYNTHIA", "TESTPATIENT", "fake-identity-token"):
        assert value not in text
    events = {e["event"] for e in logs.events()}
    assert {
        "login-failed",
        "login-rate-limited",
        "login-succeeded",
        "token-refused",
        "bug",
    } <= events
    bug = next(e for e in logs.events() if e["event"] == "bug")
    assert bug["error_type"] == "RuntimeError"
