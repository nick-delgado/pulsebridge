"""The share sequence against the fake OpenEMR: what is sent, in which order, and what comes back.

Every outgoing request is checked literally (method, path, query, body). The resources are
synthetic (T06's examples with distinctive internal ids)."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import jwt
import pytest
from fhir_proxy.share.testing import (
    BP_ID,
    CLIENT_ID,
    DEVICE_REF,
    GOOGLE_HEALTH_ID,
    HR_ID,
    KID,
    PATIENT_STORE_ID,
    SCOPES,
    SITE,
    stored_observation,
)
from fhir_proxy.testing import FHIR_STORE

V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
VITAL_SIGNS = [
    {
        "coding": [
            {
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "vital-signs",
                "display": "Vital Signs",
            }
        ],
        "text": "Vital Signs",
    }
]
PATIENT_SENT = {
    "resourceType": "Patient",
    "identifier": [
        {
            "use": "official",
            "type": {
                "coding": [
                    {"system": V2_0203, "code": "PT", "display": "Patient external identifier"}
                ]
            },
            "system": "urn:pulsebridge:patient",
            "value": "PB-SYN-0001",
        }
    ],
    "name": [{"use": "official", "family": "Example", "given": ["Avery"]}],
    "gender": "female",
    "birthDate": "1980-04-12",
}
# OpenEMR's ids as the fake hands them out: token 1, then Patient, Encounter, Observation.
PATIENT_UUID = "a337dc1b-0000-4000-8000-000000000002"
ENCOUNTER_UUID = "a337dc1b-0000-4000-8000-000000000003"
OBSERVATION_UUID = "a337dc1b-0000-4000-8000-000000000004"
BP_SENT = {
    "resourceType": "Observation",
    "status": "final",
    "category": VITAL_SIGNS,
    "code": {
        "coding": [
            {
                "system": "http://loinc.org",
                "code": "85354-9",
                "display": "Blood pressure panel with all children optional",
            }
        ],
        "text": "Blood pressure",
    },
    "effectiveDateTime": "2026-01-01T09:30:00-05:00",
    "component": [
        {
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "8480-6",
                        "display": "Systolic blood pressure",
                    }
                ],
                "text": "Systolic",
            },
            "valueQuantity": {
                "value": 121,
                "unit": "mmHg",
                "system": "http://unitsofmeasure.org",
                "code": "mm[Hg]",
            },
        },
        {
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "8462-4",
                        "display": "Diastolic blood pressure",
                    }
                ],
                "text": "Diastolic",
            },
            "valueQuantity": {
                "value": 79,
                "unit": "mmHg",
                "system": "http://unitsofmeasure.org",
                "code": "mm[Hg]",
            },
        },
    ],
    "subject": {"reference": f"Patient/{PATIENT_UUID}"},
    "encounter": {"reference": f"Encounter/{ENCOUNTER_UUID}"},
}
HR_SENT = {
    "resourceType": "Observation",
    "status": "final",
    "category": VITAL_SIGNS,
    "code": {
        "coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}],
        "text": "Heart rate",
    },
    # OpenEMR 8.5.0 reads only effectiveDateTime: the window is sent at its start.
    "effectiveDateTime": "2026-01-01T09:05:00-05:00",
    "valueQuantity": {
        "value": 68.2,
        "unit": "beats/minute",
        "system": "http://unitsofmeasure.org",
        "code": "/min",
    },
    "method": {
        "text": "Mean of the device's samples over effectivePeriod; "
        "components give the minimum and maximum"
    },
    "component": [
        {
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "96935-2",
                        "display": "Heart rate unspecified time minimum by Pedometer",
                    }
                ],
                "text": "Heart rate minimum",
            },
            "valueQuantity": {
                "value": 64,
                "unit": "beats/minute",
                "system": "http://unitsofmeasure.org",
                "code": "/min",
            },
        },
        {
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "55426-1",
                        "display": "Heart rate unspecified time maximum by Pedometer",
                    }
                ],
                "text": "Heart rate maximum",
            },
            "valueQuantity": {
                "value": 75,
                "unit": "beats/minute",
                "system": "http://unitsofmeasure.org",
                "code": "/min",
            },
        },
    ],
    "subject": {"reference": f"Patient/{PATIENT_UUID}"},
    "encounter": {"reference": f"Encounter/{ENCOUNTER_UUID}"},
}
ENCOUNTER_SENT = {
    "resourceType": "Encounter",
    "status": "finished",
    "class": {
        "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
        "code": "AMB",
        "display": "ambulatory",
    },
    "subject": {"reference": f"Patient/{PATIENT_UUID}"},
    "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T23:59:59Z"},
}


def _requests(env) -> list[tuple]:  # noqa: ANN001
    out = []
    for r in env.openemr.requests:
        body = r["body"]
        if r["path"] == "/oauth2/default/token":
            parsed: object = dict(parse_qsl(body.decode()))
        else:
            parsed = json.loads(body) if body else None
        out.append((r["method"], r["path"], r["query"], parsed))
    return out


def test_first_blood_pressure_share_sends_exactly_this(env) -> None:
    response = env.share(observation_id=BP_ID)
    assert response.status_code == 200 and response.mimetype == "application/json"
    body = response.get_json()
    assert body["outcome"] == "shared"
    sent = _requests(env)
    # 1. The token request: client credentials with a signed assertion, the document's scopes.
    method, path, query, form = sent[0]
    assert (method, path, query) == ("POST", "/oauth2/default/token", [])
    assert set(form) == {"grant_type", "scope", "client_assertion_type", "client_assertion"}
    assert form["grant_type"] == "client_credentials"
    assert form["scope"] == SCOPES
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    header = jwt.get_unverified_header(form["client_assertion"])
    claims = jwt.decode(form["client_assertion"], options={"verify_signature": False})
    assert header["alg"] == "RS384" and header["kid"] == KID
    assert claims["iss"] == claims["sub"] == CLIENT_ID
    assert claims["aud"] == f"{SITE}/oauth2/default/token"
    assert claims["exp"] - claims["iat"] == 60 and len(claims["jti"]) >= 16
    # 2-6. The FHIR calls, literally.
    assert sent[1:] == [
        ("GET", "/apis/default/fhir/Patient", [("identifier", "PB-SYN-0001")], None),
        ("POST", "/apis/default/fhir/Patient", [], PATIENT_SENT),
        (
            "GET",
            "/apis/default/fhir/Encounter",
            [("patient", PATIENT_UUID), ("date", "2026-01-01")],
            None,
        ),
        ("POST", "/apis/default/fhir/Encounter", [], ENCOUNTER_SENT),
        ("POST", "/apis/default/fhir/Observation", [], BP_SENT),
        ("GET", f"/apis/default/fhir/Patient/{PATIENT_UUID}", [], None),
        ("GET", f"/apis/default/fhir/Encounter/{ENCOUNTER_UUID}", [], None),
        ("GET", f"/apis/default/fhir/Observation/{OBSERVATION_UUID}", [], None),
    ]
    # Every FHIR call carries the access token, and only those.
    token = env.openemr.requests[0]
    assert "authorization" not in token["headers"]
    assert all(
        r["headers"]["authorization"].startswith("Bearer synthetic-openemr-access-token-")
        for r in env.openemr.requests[1:]
    )
    # The steps, in order, with the contract's names and results.
    assert [(s["step"], s["result"], s["response"]["status"]) for s in body["steps"]] == [
        ("token", "ok", 200),
        ("patient-search", "not-found", 200),
        ("patient-create", "ok", 201),
        ("encounter-search", "not-found", 200),
        ("encounter-create", "ok", 201),
        ("observation-create", "ok", 201),
        ("patient-read", "ok", 200),
        ("encounter-read", "ok", 200),
        ("observation-read", "ok", 200),
    ]
    steps = {s["step"]: s for s in body["steps"]}
    assert steps["patient-search"]["request"] == {
        "method": "GET",
        "url": "/apis/default/fhir/Patient",
        "query": {"identifier": "PB-SYN-0001"},
        "body": None,
    }
    assert steps["encounter-search"]["request"]["query"] == {
        "patient": PATIENT_UUID,
        "date": "2026-01-01",
    }
    assert steps["observation-create"]["request"]["body"] == BP_SENT
    assert steps["observation-create"]["response"]["body"] == {"uuid": OBSERVATION_UUID}
    # The read-back: what was sent beside what OpenEMR stored.
    read_back = body["readBack"]
    assert read_back["patient"]["sent"] == PATIENT_SENT
    assert read_back["encounter"]["sent"] == ENCOUNTER_SENT
    assert read_back["observation"]["sent"] == BP_SENT
    assert read_back["observation"]["stored"]["id"] == OBSERVATION_UUID
    assert read_back["patient"]["stored"]["identifier"][0]["system"] == V2_0203  # S1: rewritten
    assert read_back["encounter"]["stored"]["id"] == ENCOUNTER_UUID


def test_heart_rate_share_sends_the_window_with_its_components(env) -> None:
    body = env.share(observation_id=HR_ID).get_json()
    assert body["outcome"] == "shared"
    posted = [
        b for m, p, _, b in _requests(env) if (m, p) == ("POST", "/apis/default/fhir/Observation")
    ]
    assert posted == [HR_SENT]
    # The Encounter day is effectivePeriod.start in UTC: 09:05 at -05:00 is 14:05Z on 1 January.
    assert "effectivePeriod" not in posted[0]
    assert (
        "GET",
        "/apis/default/fhir/Encounter",
        [("patient", PATIENT_UUID), ("date", "2026-01-01")],
        None,
    ) in _requests(env)


def test_nothing_internal_is_sent_to_openemr(env) -> None:
    for observation_id in (BP_ID, HR_ID):
        assert env.share(observation_id=observation_id).status_code == 200
    everything = json.dumps(
        [(r["path"], r["query"], r["body"].decode(), r["headers"]) for r in env.openemr.requests]
    )
    for internal in (
        PATIENT_STORE_ID,
        BP_ID,
        HR_ID,
        DEVICE_REF,
        GOOGLE_HEALTH_ID,
        "urn:pulsebridge:observation",
        "urn:pulsebridge:device",
        "google-health",
        "Provenance",
        "performer",
        '"meta"',
        '"identifier": [{"use": "official", "system": "urn:pulsebridge:observation',
    ):
        assert internal not in everything, internal
    for posted in (
        b for m, p, _, b in _requests(env) if p.endswith("/Observation") and m == "POST"
    ):
        assert not {"performer", "device", "meta", "identifier", "id"} & set(posted)


def test_an_unexpected_field_in_the_store_does_not_reach_openemr(env) -> None:
    observation = stored_observation("observation-blood-pressure.json", BP_ID)
    observation["note"] = [{"text": "pb-internal-note"}]
    observation["extension"] = [{"url": "urn:pulsebridge:x", "valueString": "pb-internal-ext"}]
    observation["derivedFrom"] = [{"reference": "Observation/pb-internal-other"}]
    observation["component"][0]["extension"] = [{"url": "urn:x", "valueString": "pb-internal-c"}]
    # Inside allowed elements too: element ids and extensions are dropped at any depth.
    observation["code"]["extension"] = [{"url": "urn:x", "valueString": "pb-internal-code"}]
    observation["component"][1]["valueQuantity"]["id"] = "pb-internal-element-id"
    observation["category"][0]["coding"][0]["extension"] = [
        {"url": "urn:x", "valueCode": "pb-internal-cat"}
    ]
    env.store.put(FHIR_STORE, observation)
    assert env.share(observation_id=BP_ID).status_code == 200
    assert not any("pb-internal" in b for b in env.sent_bodies())


def test_a_known_patient_is_found_and_not_created_again(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    env.openemr.requests.clear()
    body = env.share(observation_id=HR_ID).get_json()
    assert [(s["step"], s["result"]) for s in body["steps"][:3]] == [
        ("token", "reused"),  # the token from the first share
        ("patient-search", "found"),
        ("encounter-search", "found"),
    ]
    assert not env.openemr.calls("POST", r"/Patient$") and not env.openemr.calls(
        "POST", r"/Encounter$"
    )
    assert len(env.openemr.patients) == 1 and len(env.openemr.encounters) == 1
    assert body["readBack"]["patient"]["sent"] is None  # nothing was sent for the Patient
    assert body["readBack"]["encounter"]["sent"] is None  # the day's Encounter was reused
    assert body["readBack"]["patient"]["stored"]["id"] == PATIENT_UUID


def test_another_utc_day_gets_its_own_encounter(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    late = stored_observation("observation-blood-pressure.json", "pb-internal-obs-bp-2")
    late["effectiveDateTime"] = "2026-01-01T21:30:00-05:00"  # 02:30Z on 2 January
    env.store.put(FHIR_STORE, late)
    body = env.share(observation_id="pb-internal-obs-bp-2").get_json()
    search = next(s for s in body["steps"] if s["step"] == "encounter-search")
    assert search["request"]["query"]["date"] == "2026-01-02" and search["result"] == "not-found"
    created = next(s for s in body["steps"] if s["step"] == "encounter-create")
    assert created["request"]["body"]["period"] == {
        "start": "2026-01-02T00:00:00Z",
        "end": "2026-01-02T23:59:59Z",
    }
    assert len(env.openemr.encounters) == 2


def test_several_encounters_found_the_oldest_is_reused_and_said(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    first = next(iter(env.openemr.encounters))
    env.openemr.encounters[first]["period"] = {"start": "2026-01-01T06:00:00Z"}
    newer = dict(
        env.openemr.encounters[first],
        id="a337dc1b-0000-4000-8000-000000000099",
        period={"start": "2026-01-01T08:00:00Z", "end": "2026-01-01T23:59:59Z"},
    )
    older = dict(
        env.openemr.encounters[first],
        id="a337dc1b-0000-4000-8000-000000000098",
        period={"start": "2026-01-01T00:30:00+00:00"},
    )
    env.openemr.encounters[newer["id"]] = newer
    env.openemr.encounters[older["id"]] = older
    body = env.share(observation_id=HR_ID).get_json()
    search = next(s for s in body["steps"] if s["step"] == "encounter-search")
    assert search["result"] == "found" and "3 found: the oldest is reused" in search["label"]
    posted = env.openemr.calls("POST", r"/Observation$")[-1]
    assert json.loads(posted["body"])["encounter"] == {"reference": f"Encounter/{older['id']}"}


def test_another_patients_encounter_is_never_reused(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    (encounter,) = env.openemr.encounters.values()
    encounter["subject"] = {"reference": "Patient/someone-else", "type": "Patient"}
    original = env.openemr._search  # noqa: SLF001

    def search(rtype, query):  # noqa: ANN001, ANN202 - a server that ignores the patient
        if rtype == "Encounter":
            return env.openemr._json(
                200,
                {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": encounter}]},
            )  # noqa: SLF001
        return original(rtype, query)

    env.openemr._search = search  # noqa: SLF001
    body = env.share(observation_id=HR_ID).get_json()
    assert (
        next(s for s in body["steps"] if s["step"] == "encounter-search")["result"] == "not-found"
    )


def test_a_repeat_share_is_already_shared_with_nothing_new_created(env) -> None:
    for observation_id in (BP_ID, HR_ID):
        assert env.share(observation_id=observation_id).get_json()["outcome"] == "shared"
    counts = (len(env.openemr.patients), len(env.openemr.encounters), len(env.openemr.observations))
    for observation_id in (BP_ID, HR_ID):
        body = env.share(observation_id=observation_id).get_json()
        assert body["outcome"] == "already-shared"
        create = next(s for s in body["steps"] if s["step"] == "observation-create")
        assert create["result"] == "already-shared" and create["response"]["status"] == 400
        existing = body["readBack"]["observation"]["stored"]["id"]
        assert f"Observation/{existing}" in create["label"]
        assert body["steps"][-1]["request"]["url"] == f"/apis/default/fhir/Observation/{existing}"
    assert (
        (len(env.openemr.patients), len(env.openemr.encounters), len(env.openemr.observations))
        == counts
        == (1, 1, 2)
    )


def test_already_exists_without_a_named_observation_says_so(env) -> None:
    env.openemr.name_duplicates = False  # 8.5.0 names the id only when it can map it
    assert env.share(observation_id=BP_ID).status_code == 200
    response = env.share(observation_id=BP_ID)
    body = response.get_json()
    assert response.status_code == 200 and body["outcome"] == "already-shared"
    create = next(s for s in body["steps"] if s["step"] == "observation-create")
    assert "did not name exactly one existing Observation" in create["label"]
    assert "observation-read" not in [s["step"] for s in body["steps"]]
    assert body["readBack"]["observation"]["stored"] is None


def duplicate(code: str, *ids: str) -> object:
    texts = {
        f"t{n}": f'A "{code}" reading already exists for this encounter at this '
        f"effectiveDateTime{f' (Observation/{i})' if i else ''}; POST does not overwrite a stored "
        "reading -- update that Observation with PUT"
        for n, i in enumerate(ids)
    }
    return {"validationErrors": texts}


def test_already_exists_naming_several_observations_trusts_none(env) -> None:
    env.openemr.override(
        "POST",
        r"/Observation$",
        env.openemr._json(  # noqa: SLF001
            400,
            duplicate(
                "85354-9",
                "a337dc1b-0000-4000-8000-000000000101",
                "a337dc1b-0000-4000-8000-000000000102",
            ),
        ),
    )
    body = env.share(observation_id=BP_ID).get_json()
    assert body["outcome"] == "already-shared"
    assert "observation-read" not in [s["step"] for s in body["steps"]]


@pytest.mark.parametrize(
    "text",
    [
        # Another code than the one sent (the heart rate's), or not OpenEMR's phrase at all.
        duplicate("8867-4", "a337dc1b-0000-4000-8000-000000000101"),
        {"validationErrors": {"code": "A reading already exists for this encounter"}},
        {"validationErrors": {"x": "Patient already exists (Observation/abc)"}},
        {
            "message": 'see: A "85354-9" reading already exists for this encounter at this '
            "effectiveDateTime (Observation/abc); POST"
        },
    ],
)
def test_only_openemrs_phrase_for_the_sent_code_counts_as_already_shared(env, text) -> None:  # noqa: ANN001
    env.openemr.override("POST", r"/Observation$", env.openemr._json(400, text))  # noqa: SLF001
    response = env.share(observation_id=BP_ID)
    assert response.status_code == 502
    assert response.get_json()["steps"][-1]["result"] == "failed"


def test_an_already_shared_id_that_is_not_this_reading_is_never_shown(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    (stored_id,) = env.openemr.observations
    for change in (
        {"subject": {"reference": "Patient/someone-else"}},
        {"encounter": {"reference": "Encounter/another"}},
        {"code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]}},
    ):
        original = dict(env.openemr.observations[stored_id])
        env.openemr.observations[stored_id] = {**original, **change}
        body = env.share(observation_id=BP_ID).get_json()
        assert body["outcome"] == "already-shared"
        read = body["steps"][-1]
        assert read["step"] == "observation-read" and read["result"] == "failed"
        assert "not this reading" in read["label"]
        assert body["readBack"]["observation"]["stored"] is None
        assert read["response"]["body"] is not None  # the trace shows what came back
        env.openemr.observations[stored_id] = original


def test_a_400_that_is_not_already_exists_is_a_refusal(env) -> None:
    env.openemr.override(
        "POST",
        r"/Observation$",
        env.openemr._json(  # noqa: SLF001
            400, {"validationErrors": {"x": "Observation.category is required"}}
        ),
    )
    response = env.share(observation_id=BP_ID)
    assert response.status_code == 502
    assert response.get_json()["steps"][-1]["result"] == "failed"


def test_no_openemr_address_reaches_the_page(env) -> None:
    text = env.share(observation_id=BP_ID).get_data(as_text=True)
    assert "10.10.0.2" not in text
    body = json.loads(text)
    search = body["steps"][1]["response"]["body"]
    assert search["link"][0]["url"] == "/apis/default/fhir/Patient"


def test_a_failed_read_back_keeps_the_outcome_and_says_stored_null(env) -> None:
    env.openemr.override("GET", r"/Observation/", env.openemr._json(500, {"message": "x"}))  # noqa: SLF001
    response = env.share(observation_id=BP_ID)
    body = response.get_json()
    assert response.status_code == 200 and body["outcome"] == "shared"
    assert (
        body["steps"][-1]["step"] == "observation-read" and body["steps"][-1]["result"] == "failed"
    )
    assert body["readBack"]["observation"]["stored"] is None
    assert body["readBack"]["patient"]["stored"]["id"] == PATIENT_UUID


def test_a_read_back_of_another_resource_is_not_shown(env) -> None:
    env.openemr.override(
        "GET",
        r"/Patient/",
        env.openemr._json(  # noqa: SLF001
            200, {"resourceType": "Patient", "id": "someone-else"}
        ),
    )
    body = env.share(observation_id=BP_ID).get_json()
    assert body["readBack"]["patient"]["stored"] is None
    assert next(s for s in body["steps"] if s["step"] == "patient-read")["result"] == "failed"


def test_the_guard_stops_a_leak_before_anything_is_sent(env, monkeypatch) -> None:
    from fhir_proxy.share import resources

    real = resources.observation_for_openemr

    def leaky(stored, patient_ref, encounter_ref):  # noqa: ANN001, ANN202 - a regression
        sent = real(stored, patient_ref, encounter_ref)
        sent["device"] = stored["device"]
        return sent

    monkeypatch.setattr(resources, "observation_for_openemr", leaky)
    response = env.share(observation_id=BP_ID)
    assert response.status_code == 500 and env.openemr.requests == []
    assert response.get_json()["issue"][0]["details"]["coding"][0]["code"] == "share-blocked"


def test_a_hostile_id_in_the_duplicate_text_is_never_read(env) -> None:
    env.openemr.override(
        "POST",
        r"/Observation$",
        env.openemr._json(  # noqa: SLF001
            400, duplicate("85354-9", "..")
        ),
    )
    body = env.share(observation_id=BP_ID).get_json()
    assert body["outcome"] == "already-shared"
    assert "observation-read" not in [s["step"] for s in body["steps"]]
