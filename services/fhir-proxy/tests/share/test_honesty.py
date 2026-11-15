"""What the page is told must match what reached OpenEMR, even when the proxy itself fails half
way through a call, and nothing OpenEMR echoes back can carry a credential to the page."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import pytest
from fhir_proxy.share.testing import BP_ID, CLIENT_ID, HR_ID, SCOPES, client_document, make_key
from fhir_proxy.share.transport import Answer

STEPS = [
    ("token", "POST", r"/token$"),
    ("patient-search", "GET", r"/Patient$"),
    ("patient-create", "POST", r"/Patient$"),
    ("encounter-search", "GET", r"/Encounter$"),
    ("encounter-create", "POST", r"/Encounter$"),
    ("observation-create", "POST", r"/Observation$"),
]


class Unreadable:
    """An answer the transport returned, but whose body cannot even be looked at: the proxy
    fails between handing the request over and recording its step."""

    status = 201
    headers: dict = {}

    @property
    def body(self) -> bytes:
        raise RuntimeError("the proxy failed while reading this answer")


@pytest.mark.parametrize(("step", "method", "pattern"), STEPS)
def test_a_request_already_sent_is_always_in_the_trace(env, step, method, pattern) -> None:  # noqa: ANN001
    env.openemr.override(method, pattern, lambda *a: Unreadable())
    response = env.share()
    assert response.status_code == 502
    body = response.get_json()
    assert (
        body["operationOutcome"]["issue"][0]["details"]["coding"][0]["code"]
        == "share-internal-error"
    )
    last = body["steps"][-1]
    # The request reached OpenEMR, so it is listed: status 0 (no answer read), failed, and the
    # label says its outcome is not known. The page then never says "the reading was not sent".
    assert (last["step"], last["response"]["status"], last["result"]) == (step, 0, "failed")
    assert "whether it took effect is not known" in last["label"]
    assert last["request"]["method"] == method
    assert len(body["steps"]) == len(env.openemr.requests)  # one step per request sent


@pytest.mark.parametrize("step", ["patient-read", "encounter-read", "observation-read"])
def test_a_read_back_that_fails_mid_call_is_listed_and_the_share_stands(env, step) -> None:  # noqa: ANN001
    kind = step.split("-")[0].title()
    env.openemr.override("GET", rf"/{kind}/", lambda *a: Unreadable())
    body = env.share().get_json()
    assert body["outcome"] == "shared"
    read = next(s for s in body["steps"] if s["step"] == step)
    assert read["response"]["status"] == 0 and read["result"] == "failed"
    assert "not known" in read["label"]
    assert body["readBack"][step.split("-")[0]]["stored"] is None


def test_a_body_nested_too_deep_is_an_answer_not_understood(env) -> None:
    deep = Answer(201, {"content-type": "application/json"}, b"[" * 100_000)
    env.openemr.override("POST", r"/Observation$", deep)
    response = env.share()
    assert response.status_code == 502
    last = response.get_json()["steps"][-1]
    assert (last["step"], last["result"], last["response"]["status"]) == (
        "observation-create",
        "failed",
        201,
    )
    assert last["response"]["body"] is None
    assert "could not be understood" in last["label"]


def test_a_deep_body_in_a_search_is_refused_not_a_crash(env) -> None:
    env.openemr.override("GET", r"/Patient$", Answer(200, {}, b"[" * 100_000))
    response = env.share()
    assert response.status_code == 502
    assert response.get_json()["steps"][-1]["step"] == "patient-search"


# ---- credentials echoed by OpenEMR ------------------------------------------------------------


def test_an_assertion_echoed_in_a_token_error_is_not_shown(env) -> None:
    def echo(method, path, query, headers, body):  # noqa: ANN001, ANN202
        assertion = dict(parse_qsl(body.decode()))["client_assertion"]
        return Answer(
            400,
            {},
            json.dumps(
                {
                    "error": "invalid_request",
                    "error_description": f"could not use {assertion}",
                    "hint": assertion,
                    "nested": [{"detail": f"jwt={assertion}"}],
                }
            ).encode(),
        )

    env.openemr.override("POST", r"/token$", echo)
    response = env.share()
    text = response.get_data(as_text=True)
    (sent,) = env.openemr.calls("POST", r"/token$")
    assertion = dict(parse_qsl(sent["body"].decode()))["client_assertion"]
    assert assertion not in text
    for piece in assertion.split("."):
        assert piece not in text
    assert "could not use [not shown]" in text


def test_an_access_token_echoed_in_a_fhir_error_is_not_shown(env) -> None:
    def echo(method, path, query, headers, body):  # noqa: ANN001, ANN202
        auth = {k.lower(): v for k, v in headers.items()}["authorization"]
        token = auth.removeprefix("Bearer ")
        return Answer(
            500, {}, json.dumps({"message": f"bad header {auth}", "token": token}).encode()
        )

    env.openemr.override("GET", r"/Encounter$", echo)
    response = env.share()
    text = response.get_data(as_text=True)
    assert response.status_code == 502
    for token in env.openemr.tokens:
        assert token not in text
    assert "bad header Bearer [not shown]" in text


def test_a_reused_token_echoed_is_not_shown_either(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200

    def echo(method, path, query, headers, body):  # noqa: ANN001, ANN202
        auth = {k.lower(): v for k, v in headers.items()}["authorization"]
        return Answer(500, {}, json.dumps({"message": auth}).encode())

    env.openemr.override("GET", r"/Patient$", echo)
    text = env.share(observation_id=HR_ID).get_data(as_text=True)
    assert not any(t in text for t in env.openemr.tokens)


# ---- the document is read on every share ---------------------------------------------------------


def test_the_document_is_read_again_on_every_share(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    # A re-registration replaced the document; OpenEMR knows both clients (the old one is not
    # refused, so nothing but a fresh read can make the proxy notice).
    new_key = make_key()
    env.openemr.register("synthetic-client-id-0002", new_key, "kid-2", SCOPES)
    env.doc_path.write_text(
        json.dumps(
            client_document(
                env.settings,
                key=new_key,
                certificate_pem=env.cert_pem,
                client_id="synthetic-client-id-0002",
                kid="kid-2",
            )
        )
    )
    body = env.share(observation_id=HR_ID).get_json()
    assert body["steps"][0]["result"] == "ok"  # a new client: the old client's token is not used
    import jwt

    form = dict(parse_qsl(env.openemr.calls("POST", r"/token$")[-1]["body"].decode()))
    assert jwt.decode(form["client_assertion"], options={"verify_signature": False})["iss"] == (
        "synthetic-client-id-0002"
    )
    assert CLIENT_ID != "synthetic-client-id-0002"


# ---- hostile ids in search results --------------------------------------------------------------


@pytest.mark.parametrize("hostile", ["..", "a/b", "x?y=1", "", "a" * 65])
def test_a_hostile_patient_id_in_a_search_is_never_used(env, hostile) -> None:  # noqa: ANN001
    env.openemr.override(
        "GET",
        r"/Patient$",
        Answer(
            200,
            {},
            json.dumps(
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "Patient",
                                "id": hostile,
                                "identifier": [{"value": "PB-SYN-0001"}],
                            }
                        }
                    ],
                }
            ).encode(),
        ),
    )
    response = env.share()
    assert response.status_code == 502  # found, but nothing usable: stop, never create
    assert len(env.openemr.requests) == 2


@pytest.mark.parametrize("hostile", ["..", "a/b", "x?y=1"])
def test_a_hostile_encounter_id_in_a_search_is_never_used(env, hostile) -> None:  # noqa: ANN001
    assert env.share(observation_id=BP_ID).status_code == 200
    (patient_id,) = env.openemr.patients
    env.openemr.override(
        "GET",
        r"/Encounter$",
        Answer(
            200,
            {},
            json.dumps(
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "Encounter",
                                "id": hostile,
                                "subject": {"reference": f"Patient/{patient_id}"},
                            }
                        }
                    ],
                }
            ).encode(),
        ),
    )
    body = env.share(observation_id=HR_ID).get_json()
    assert (
        next(s for s in body["steps"] if s["step"] == "encounter-search")["result"] == "not-found"
    )
    assert not any(hostile in r["path"] for r in env.openemr.requests if hostile)


def test_an_encounter_without_a_subject_is_not_ours(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    (encounter,) = env.openemr.encounters.values()
    encounter.pop("subject")
    found = {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": encounter}]}
    env.openemr.override("GET", r"/Encounter$", Answer(200, {}, json.dumps(found).encode()))
    body = env.share(observation_id=HR_ID).get_json()
    assert (
        next(s for s in body["steps"] if s["step"] == "encounter-search")["result"] == "not-found"
    )


def test_an_encounter_whose_reference_only_ends_with_ours_is_not_ours(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    (encounter,) = env.openemr.encounters.values()
    (patient_id,) = env.openemr.patients
    encounter["subject"] = {"reference": f"https://elsewhere.example/fhir/Patient/{patient_id}"}
    found = {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": encounter}]}
    env.openemr.override("GET", r"/Encounter$", Answer(200, {}, json.dumps(found).encode()))
    body = env.share(observation_id=HR_ID).get_json()
    assert (
        next(s for s in body["steps"] if s["step"] == "encounter-search")["result"] == "not-found"
    )
