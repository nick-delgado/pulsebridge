"""The trace routes: heart rate (reconstructed window) and cuff attempts (message as sent)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from fhir_proxy import hl7
from fhir_proxy.testing import (
    AUDIT_STORE,
    FHIR_STORE,
    STORE_NAME,
    build_harness,
    example,
    sample_bytes,
)
from fhir_proxy.traces import reconstruct_window
from pulsebridge_hl7v2.samples import load as load_sample

HTTPS_NAME = f"{STORE_NAME}/messages/synthetic-message-0002"
DERIVED_NAME = f"{STORE_NAME}/messages/c3ludGhldGljLWRlcml2ZWQtbWVzc2FnZS0wMDA2LTA="


def _observation_ids(harness, code: str) -> list[str]:  # noqa: ANN001
    return [
        rid
        for (rtype, rid), r in harness.fake.fhir[FHIR_STORE].items()
        if rtype == "Observation" and r["code"]["coding"][0]["code"] == code
    ]


# ---- heart rate ---------------------------------------------------------------------------------


def test_heart_rate_trace_returns_the_resources_and_the_reconstructed_window(
    harness, seed_heart_rate
) -> None:
    seed_heart_rate(datetime(2026, 1, 1, 14, 5, tzinfo=UTC), 1)
    (obs_id,) = _observation_ids(harness, "8867-4")
    response = harness.get(f"/trace/heart-rate/{obs_id}")
    assert response.status_code == 200
    body = response.get_json()
    types = [e["resource"]["resourceType"] for e in body["resources"]["entry"]]
    assert body["resources"]["type"] == "collection"
    assert types == ["Observation", "Provenance", "Device"]
    provenance = body["resources"]["entry"][1]["resource"]
    assert {"reference": f"Observation/{obs_id}"} in provenance["target"]
    # The window exactly as Google's roll-up wrote it (the seeded item), rebuilt from the store.
    assert body["source"] == {
        "kind": "reconstructed",
        "window": {
            "startTime": "2026-01-01T14:05:00Z",
            "endTime": "2026-01-01T14:10:00Z",
            "heartRate": {
                "beatsPerMinuteMin": 55,
                "beatsPerMinuteAvg": 63.5,
                "beatsPerMinuteMax": 71,
            },
        },
    }
    # The stored Observation is in the user's zone; the window is UTC, as Google sends it.
    stored = body["resources"]["entry"][0]["resource"]
    assert stored["effectivePeriod"]["start"] == "2026-01-01T09:05:00-05:00"


def test_reconstruction_matches_t06s_mapping_both_ways() -> None:
    from zoneinfo import ZoneInfo

    from pulsebridge_fhir import (
        DeviceInput,
        GoogleDataSource,
        PatientInput,
        heart_rate_transaction,
    )

    item = {
        "startTime": "2026-03-08T06:55:00Z",
        "endTime": "2026-03-08T07:00:00Z",
        "heartRate": {
            "beatsPerMinuteMin": 61,
            "beatsPerMinuteAvg": 73.41666666666667,
            "beatsPerMinuteMax": 88,
        },
    }
    bundle = heart_rate_transaction(
        [item],
        patient=PatientInput("PB-SYN-0001"),
        device=DeviceInput("Synthetic Watch"),
        data_source=GoogleDataSource(platform=None, recording_method=None),
        tz=ZoneInfo("America/New_York"),
        recorded=datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
        complete_before=None,
    )
    observation = next(
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"
    )
    assert reconstruct_window(observation) == item


@pytest.mark.parametrize("drop", ["valueQuantity", "component", "effectivePeriod"])
def test_incomplete_observation_gives_no_window(harness, seed_heart_rate, drop: str) -> None:
    seed_heart_rate(datetime(2026, 1, 1, 14, 5, tzinfo=UTC), 1)
    (obs_id,) = _observation_ids(harness, "8867-4")
    stored = harness.fake.fhir[FHIR_STORE][("Observation", obs_id)]
    del stored[drop]
    if drop == "effectivePeriod":
        stored["effectiveDateTime"] = "2026-01-01T09:05:00-05:00"
    body = harness.get(f"/trace/heart-rate/{obs_id}").get_json()
    assert body["source"] is None


def test_heart_rate_trace_of_another_patient_or_a_bp_reading_is_not_found(
    harness, seed_heart_rate
) -> None:
    seed_heart_rate(datetime(2026, 1, 1, 14, 5, tzinfo=UTC), 1)
    (obs_id,) = _observation_ids(harness, "8867-4")
    other = copy.deepcopy(harness.fake.fhir[FHIR_STORE][("Observation", obs_id)])
    other["id"] = "other-patients"
    other["subject"] = {"reference": "Patient/other"}
    harness.fake.put(FHIR_STORE, other)
    assert harness.get("/trace/heart-rate/other-patients").status_code == 404
    bp = copy.deepcopy(harness.fake.fhir[FHIR_STORE][("Observation", obs_id)])
    bp["id"] = "a-bp-reading"
    bp["code"] = {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]}
    harness.fake.put(FHIR_STORE, bp)
    assert harness.get("/trace/heart-rate/a-bp-reading").status_code == 404
    assert harness.get("/trace/heart-rate/does-not-exist").status_code == 404


def test_heart_rate_trace_with_a_hostile_device_reference_skips_the_device(
    harness, seed_heart_rate
) -> None:
    seed_heart_rate(datetime(2026, 1, 1, 14, 5, tzinfo=UTC), 1)
    (obs_id,) = _observation_ids(harness, "8867-4")
    harness.fake.fhir[FHIR_STORE][("Observation", obs_id)]["device"] = {"reference": "Device/.."}
    body = harness.get(f"/trace/heart-rate/{obs_id}").get_json()
    types = [e["resource"]["resourceType"] for e in body["resources"]["entry"]]
    assert "Device" not in types
    assert not harness.fake.calls("GET", "/Device/")


# ---- cuff attempts ------------------------------------------------------------------------------


def test_https_attempt_trace_has_the_stored_message_observation_and_provenance(
    harness, map_into_store
) -> None:
    data = sample_bytes("valid_bp_reading")
    labels = map_into_store(HTTPS_NAME, data)
    harness.fake.add_message(HTTPS_NAME, data, labels=labels)
    event = example("auditevent-send-accepted.json")
    harness.fake.put(AUDIT_STORE, event)
    body = harness.get(f"/trace/cuff-attempts/{event['id']}").get_json()
    assert body["messageSent"] == data.decode()
    assert "\r" in body["messageSent"]
    types = [e["resource"]["resourceType"] for e in body["resources"]["entry"]]
    assert types == ["AuditEvent", "Observation", "Provenance"]
    audit, observation, provenance = (e["resource"] for e in body["resources"]["entry"])
    # The AuditEvent exactly as stored: the raw ACK stays base64 there.
    assert any(
        d["type"] == "ack-message" and "valueBase64Binary" in d
        for e in audit["entity"]
        for d in e.get("detail", [])
    )
    assert observation["code"]["coding"][0]["code"] == "85354-9"
    source = next(e for e in provenance["entity"] if e["role"] == "source")
    assert source["what"]["identifier"]["value"] == HTTPS_NAME
    assert body["attempt"]["observation"] == f"Observation/{observation['id']}"
    assert body["attempt"]["application"]["outcome"] == "accepted"


def test_mllp_attempt_trace_confirms_the_derived_name(harness, map_into_store) -> None:
    data = sample_bytes("valid_bp_reading")
    labels = map_into_store(DERIVED_NAME, data)
    harness.fake.add_message(DERIVED_NAME, data, labels=labels)
    event = example("auditevent-send-accepted-mllp.json")
    harness.fake.put(AUDIT_STORE, event)
    body = harness.get(f"/trace/cuff-attempts/{event['id']}").get_json()
    assert body["attempt"]["messageLookup"] == "confirmed"
    assert body["attempt"]["messageName"] is None
    assert body["messageSent"] == data.decode()
    assert len(body["resources"]["entry"]) == 3


def test_mllp_attempt_whose_derived_name_is_missing_has_no_message(harness) -> None:
    event = example("auditevent-send-accepted-mllp.json")
    harness.fake.put(AUDIT_STORE, event)
    body = harness.get(f"/trace/cuff-attempts/{event['id']}").get_json()
    assert body["attempt"]["messageLookup"] == "not-found"
    assert body["messageSent"] is None
    assert [e["resource"]["resourceType"] for e in body["resources"]["entry"]] == ["AuditEvent"]


def test_rejected_sample_trace_shows_the_samples_own_bytes(harness) -> None:
    event = example("auditevent-send-rejected.json")
    harness.fake.put(AUDIT_STORE, event)
    body = harness.get(f"/trace/cuff-attempts/{event['id']}").get_json()
    # unparseable_no_msh is one of the samples with_patient leaves unchanged.
    assert body["messageSent"] == load_sample("unparseable_no_msh").payload.decode()
    assert body["attempt"]["commitAck"]["code"] == "AE"


def test_no_ack_sample_trace_rebuilds_the_pid_as_cuff_sim_did(harness) -> None:
    event = example("auditevent-send-no-ack.json")
    harness.fake.put(AUDIT_STORE, event)
    body = harness.get(f"/trace/cuff-attempts/{event['id']}").get_json()
    expected = sample_bytes("noack_missing_end_block").decode()
    assert body["messageSent"] == expected
    assert "PB-SYN-0001" in body["messageSent"]


def test_without_the_patient_variables_a_substituted_sample_is_not_guessed(tmp_path) -> None:
    h = build_harness(tmp_path, sample_patient=None)
    event = example("auditevent-send-no-ack.json")
    h.fake.put(AUDIT_STORE, event)
    body = h.get(f"/trace/cuff-attempts/{event['id']}").get_json()
    assert body["messageSent"] is None


def test_built_reading_that_was_never_stored_has_no_message_text(harness) -> None:
    event = example("auditevent-send-unreadable-reply.json")
    harness.fake.put(AUDIT_STORE, event)
    body = harness.get(f"/trace/cuff-attempts/{event['id']}").get_json()
    assert body["messageSent"] is None


def test_cuff_trace_refusals(harness) -> None:
    assert harness.get("/trace/cuff-attempts/no-such-attempt").status_code == 404
    broken = copy.deepcopy(example("auditevent-send-rejected.json"))
    broken["id"] = "broken-1"
    broken["outcome"] = "0"  # contradicts the AE
    harness.fake.put(AUDIT_STORE, broken)
    response = harness.get("/trace/cuff-attempts/broken-1")
    assert response.status_code == 422
    other = copy.deepcopy(example("auditevent-send-rejected.json"))
    other["id"] = "other-patient-1"
    other["entity"][0]["what"]["identifier"]["value"] = "PB-SYN-9999"
    harness.fake.put(AUDIT_STORE, other)
    assert harness.get("/trace/cuff-attempts/other-patient-1").status_code == 404


def test_observation_identifier_matches_the_mappers_for_every_mappable_sample(
    harness, map_into_store
) -> None:
    """The join key the proxy computes equals the one v2-to-fhir writes (no drift)."""
    for name in ("valid_bp_reading", "valid_bp_reading_offset"):
        data = sample_bytes(name)
        message_name = f"{STORE_NAME}/messages/{name}"
        map_into_store(message_name, data, prefix=name)
        key = hl7.observation_identifier(data, "PB-SYN-0001")
        written = [
            r
            for (t, _), r in harness.fake.fhir[FHIR_STORE].items()
            if t == "Observation" and any(i["value"] == key for i in r.get("identifier", []))
        ]
        assert len(written) == 1, name


@pytest.mark.parametrize(
    "data", [b"", b"PID|1", b"\xff\xfe\x00", b"MSH|^~\\&", b"MSH|^~\\&|A\nPID"]
)
def test_hl7_helpers_never_raise_on_hostile_bytes(data: bytes) -> None:
    assert hl7.observation_identifier(data, "PB-SYN-0001") is None or isinstance(
        hl7.observation_identifier(data, "PB-SYN-0001"), str
    )
    hl7.control_id(data)
