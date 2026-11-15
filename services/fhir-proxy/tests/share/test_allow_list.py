"""The Observation sent is built from allow-lists at every depth, and the leak guard stops a
share, before anything is sent, if PulseBridge's own systems would reach OpenEMR anyway."""

from __future__ import annotations

import json

import pytest
from fhir_proxy.share import resources
from fhir_proxy.share.testing import BP_ID, HR_ID, stored_observation
from fhir_proxy.testing import FHIR_STORE


def keys(value: object):  # noqa: ANN201 - every key at any depth
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys(v)


def sent(env, observation_id: str = BP_ID) -> dict:  # noqa: ANN001
    response = env.share(observation_id=observation_id)
    assert response.status_code == 200, response.get_data(as_text=True)
    (post,) = env.openemr.calls("POST", r"/Observation$")
    return json.loads(post["body"])


def test_codings_keep_only_system_code_and_display(env) -> None:
    observation = stored_observation("observation-blood-pressure.json", BP_ID)
    coding = observation["code"]["coding"][0]
    coding.update(version="2.77", userSelected=True, _code={"extension": []})
    observation["code"]["_text"] = {"extension": [{"url": "urn:x", "valueString": "pb-x"}]}
    observation["component"][0]["valueQuantity"]["comparator"] = "<"
    observation["component"][0]["valueQuantity"]["_value"] = {"id": "pb-x"}
    env.store.put(FHIR_STORE, observation)
    body = sent(env)
    assert body["code"]["coding"] == [
        {
            "system": "http://loinc.org",
            "code": "85354-9",
            "display": "Blood pressure panel with all children optional",
        }
    ]
    assert set(body["component"][0]["valueQuantity"]) == {"value", "unit", "system", "code"}
    assert not [k for k in keys(body) if k.startswith("_")]
    assert "pb-x" not in json.dumps(body)


def test_codings_from_other_systems_are_dropped_and_nothing_empty_is_sent(env) -> None:
    observation = stored_observation("observation-blood-pressure.json", BP_ID)
    observation["code"]["coding"].append({"system": "urn:pulsebridge:local", "code": "pb-x"})
    observation["category"].append({"coding": [{"system": "urn:other", "code": "x"}]})
    observation["category"][0]["coding"].append({"system": "http://snomed.info/sct", "code": "1"})
    env.store.put(FHIR_STORE, observation)
    body = sent(env)
    assert [c["system"] for c in body["code"]["coding"]] == ["http://loinc.org"]
    assert len(body["category"]) == 1  # the other-system category had nothing left
    assert [c["code"] for c in body["category"][0]["coding"]] == ["vital-signs"]
    assert "{}" not in json.dumps(body, separators=(",", ":")) and "[]" not in json.dumps(body)


def test_a_heart_rate_is_sent_at_its_window_start_without_a_period(env) -> None:
    body = sent(env, HR_ID)
    assert body["effectiveDateTime"] == "2026-01-01T09:05:00-05:00"  # the start, its offset kept
    assert "effectivePeriod" not in body
    assert body["method"] == {
        "text": "Mean of the device's samples over effectivePeriod; components give the "
        "minimum and maximum"
    }


def test_the_unit_builders_refuse_what_is_not_their_type() -> None:
    assert resources._quantity({"value": True, "system": resources.UCUM}) is None  # noqa: SLF001
    assert resources._quantity({"value": 1, "system": "urn:x"}) is None  # noqa: SLF001
    assert resources._concept({"coding": [{"system": "urn:x", "code": "a"}]}, frozenset()) is None  # noqa: SLF001
    assert resources._coding({"system": "http://loinc.org"}, resources.CODE_SYSTEMS) is None  # noqa: SLF001


@pytest.mark.parametrize(
    "where",
    [
        lambda o: o["code"].update(text="urn:pulsebridge:observation|abc"),
        lambda o: o["component"][0]["code"].update(text="see urn:pulsebridge:device"),
        lambda o: o["method"].update(text="from urn:pulsebridge:source-platform"),
    ],
)
def test_the_guard_refuses_our_own_systems_before_anything_is_sent(env, where) -> None:  # noqa: ANN001
    observation = stored_observation("observation-heart-rate.json", HR_ID)
    where(observation)
    env.store.put(FHIR_STORE, observation)
    response = env.share(observation_id=HR_ID)
    assert response.status_code == 500 and env.openemr.requests == []
    body = response.get_json()
    assert body["resourceType"] == "OperationOutcome" and "steps" not in body
    assert body["issue"][0]["details"]["coding"][0]["code"] == "share-blocked"
    assert "before anything was sent" in body["issue"][0]["diagnostics"]


def test_the_patients_pt_system_is_the_one_pulsebridge_value_allowed(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    (post,) = env.openemr.calls("POST", r"/Patient$")
    patient = json.loads(post["body"])
    assert patient["identifier"][0]["system"] == "urn:pulsebridge:patient"
    assert json.dumps(patient).count("urn:pulsebridge:") == 1
