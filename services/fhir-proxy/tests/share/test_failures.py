"""Failures once OpenEMR has been called: never a bare 5xx, always the envelope
``{"operationOutcome", "steps"}`` with the steps so far (web/README.md, "For T15"), and never a
claim about what was not sent."""

from __future__ import annotations

import json

import pytest
from fhir_proxy import deadline
from fhir_proxy.errors import REASON_SYSTEM
from fhir_proxy.share import service as share_service
from fhir_proxy.share.testing import (
    BP_ID,
    HR_ID,
    KID,
    raise_timeout,
    raise_unreachable,
)
from fhir_proxy.share.transport import Answer, AnswerTooLarge


def envelope(response, status: int, reason: str) -> list[dict]:  # noqa: ANN001
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.mimetype == "application/json"
    body = response.get_json()
    assert set(body) == {"operationOutcome", "steps"}
    outcome = body["operationOutcome"]
    # Exactly as Problem builds it.
    assert (
        set(outcome) == {"resourceType", "issue"} and outcome["resourceType"] == "OperationOutcome"
    )
    (issue,) = outcome["issue"]
    assert set(issue) == {"severity", "code", "details", "diagnostics"}
    assert issue["severity"] == "error"
    assert issue["code"] == ("timeout" if status == 504 else "exception")
    assert issue["details"] == {"coding": [{"system": REASON_SYSTEM, "code": reason}]}
    return body["steps"]


def answer(status: int, body: object = None, raw: bytes | None = None, **headers: str) -> Answer:
    data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    return Answer(status, {"content-type": "application/json", **headers}, data)


def names(steps: list[dict]) -> list[tuple]:
    return [(s["step"], s["result"], s["response"]["status"]) for s in steps]


def test_openemr_unreachable_at_the_token_step(env) -> None:
    env.openemr.override("POST", r"/token$", raise_unreachable)
    steps = envelope(env.share(), 502, "openemr-unreachable")
    assert names(steps) == [("token", "failed", 0)]
    assert "no answer (unreachable)" in steps[0]["label"]


def test_openemr_unreachable_part_way(env) -> None:
    env.openemr.override("GET", r"/Encounter$", raise_unreachable)
    steps = envelope(env.share(), 502, "openemr-unreachable")
    assert names(steps) == [
        ("token", "ok", 200),
        ("patient-search", "not-found", 200),
        ("patient-create", "ok", 201),
        ("encounter-search", "failed", 0),
    ]


def test_a_500_from_openemr_is_refused_with_the_steps_so_far(env) -> None:
    env.openemr.override("POST", r"/Patient$", answer(500, {"message": "boom"}))
    steps = envelope(env.share(), 502, "openemr-refused")
    assert names(steps)[-1] == ("patient-create", "failed", 500)
    assert not env.openemr.calls("GET", r"/Encounter")  # it stopped there


def test_a_timeout_on_the_observation_is_504_and_says_it_is_not_known(env) -> None:
    env.openemr.override("POST", r"/Observation$", raise_timeout)
    response = env.share()
    steps = envelope(response, 504, "openemr-timeout")
    assert names(steps)[-1] == ("observation-create", "failed", 0)
    assert (
        "not known whether that step took effect"
        in response.get_json()["operationOutcome"]["issue"][0]["diagnostics"]
    )


def test_the_budget_running_out_mid_sequence_is_504_with_no_unsent_step(env) -> None:
    def spend_the_budget(method, path, query, headers, body):  # noqa: ANN001, ANN202
        deadline.current().at = 0.0  # the request's budget is gone after this answer
        return env.openemr._route(method, path, dict(query), headers, body)  # noqa: SLF001

    env.openemr.override("GET", r"/Encounter$", spend_the_budget)
    response = env.share()
    steps = envelope(response, 504, "openemr-timeout")
    # encounter-search got its answer; encounter-create was never sent, so it is not a step.
    assert names(steps)[-1] == ("encounter-search", "not-found", 200)
    assert not env.openemr.calls("POST", r"/Encounter$")
    text = response.get_json()["operationOutcome"]["issue"][0]["diagnostics"]
    assert "before the encounter-create step could be sent" in text


def test_the_budget_spent_waiting_for_the_lock_is_a_refusal_that_nothing_was_sent(env) -> None:
    class SlowLock:
        def acquire(self, *args: object, **kwargs: object) -> bool:
            deadline.current().at = 0.0  # the wait used up the request's budget
            return True

        def release(self) -> None:
            return None

    env.service.lock = SlowLock()
    response = env.share()
    assert response.status_code == 503 and env.openemr.requests == []
    body = response.get_json()
    assert body["issue"][0]["details"]["coding"][0]["code"] == "share-busy"
    assert "nothing was sent" in body["issue"][0]["diagnostics"]


def test_an_unreadable_answer_is_refused(env) -> None:
    env.openemr.override("GET", r"/Patient$", answer(200, raw=b"<html>not json</html>"))
    steps = envelope(env.share(), 502, "openemr-refused")
    assert names(steps)[-1] == ("patient-search", "failed", 200)
    assert steps[-1]["response"]["body"] is None  # the text is never passed on


def test_a_redirect_is_not_followed(env) -> None:
    env.openemr.override(
        "GET", r"/Patient$", answer(302, None, location="https://evil.example.test/x")
    )
    steps = envelope(env.share(), 502, "openemr-refused")
    assert names(steps)[-1] == ("patient-search", "failed", 302)
    assert len(env.openemr.calls("GET")) == 1  # nothing else was asked, anywhere


def test_an_oversized_answer_is_refused(env) -> None:
    def too_large(*_: object) -> Answer:
        raise AnswerTooLarge(200)

    env.openemr.override("GET", r"/Patient$", too_large)
    response = env.share()
    steps = envelope(response, 502, "openemr-refused")
    # Not read, so no status is claimed: status 0, and "not known" in the label and the text.
    assert names(steps)[-1] == ("patient-search", "failed", 0)
    assert "answer too large to read (HTTP 200)" in steps[-1]["label"]
    assert "not known" in response.get_json()["operationOutcome"]["issue"][0]["diagnostics"]


def test_an_oversized_answer_to_the_observation_is_not_known_not_refused(env) -> None:
    def too_large(*_: object) -> Answer:
        raise AnswerTooLarge(500)

    env.openemr.override("POST", r"/Observation$", too_large)
    steps = envelope(env.share(), 502, "openemr-refused")
    assert names(steps)[-1] == ("observation-create", "failed", 0)


def test_patients_found_without_our_identifier_stop_rather_than_create(env) -> None:
    env.openemr.override(
        "GET",
        r"/Patient$",
        answer(
            200,
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": 1,
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Patient",
                            "id": "p1",
                            "identifier": [{"value": "OTHER"}],
                        }
                    }
                ],
            },
        ),
    )
    steps = envelope(env.share(), 502, "openemr-refused")
    assert names(steps)[-1] == ("patient-search", "failed", 200)
    assert not env.openemr.calls("POST", r"/Patient$")


@pytest.mark.parametrize(
    ("step", "pattern"),
    [
        ("patient-create", r"/Patient$"),
        ("encounter-create", r"/Encounter$"),
        ("observation-create", r"/Observation$"),
    ],
)
@pytest.mark.parametrize(
    "reply",
    [
        answer(201, {"pid": 3}),  # no id
        answer(201, {}),
        answer(201, raw=b"<html>created?</html>"),
        answer(200, raw=b"<html>ok</html>"),
        answer(200, {"uuid": "a337dc1b-0000-4000-8000-000000000777"}),  # not a 201
        answer(204),
        answer(201, {"uuid": ".."}),  # hostile ids are never used
        answer(201, {"uuid": "a/b"}),
        answer(201, {"uuid": "x?y=1"}),
        answer(201, {"uuid": ["a"]}),
    ],
)
def test_a_create_answered_without_a_readable_id_is_not_known(env, step, pattern, reply) -> None:  # noqa: ANN001
    env.openemr.override("POST", pattern, reply)
    response = env.share()
    steps = envelope(response, 502, "openemr-refused")
    last = steps[-1]
    assert (last["step"], last["result"], last["response"]["status"]) == (
        step,
        "failed",
        reply.status,
    )
    assert "could not be understood" in last["label"] and "not known" in last["label"]
    text = response.get_json()["operationOutcome"]["issue"][0]["diagnostics"]
    assert "not known whether that step took effect" in text
    # Nothing after it was sent, and no hostile value was used in a later path.
    assert env.openemr.requests[-1]["method"] == "POST"
    assert not any(
        r["path"].endswith(("/..", "/a/b")) or "?" in r["path"] for r in env.openemr.requests
    )


def test_a_bug_after_openemr_was_called_answers_the_envelope(env, logs, monkeypatch) -> None:
    def broken(*_: object) -> dict:
        raise KeyError("pb-sentinel-in-a-bug")

    monkeypatch.setattr(share_service.resources, "encounter_for_openemr", broken)
    steps = envelope(env.share(), 502, "share-internal-error")
    assert names(steps)[-1] == ("encounter-search", "not-found", 200)
    assert "pb-sentinel-in-a-bug" not in logs.text()
    assert any(e.get("event") == "bug" and e.get("error_type") == "KeyError" for e in logs.events())


def test_a_bug_before_openemr_was_called_is_a_plain_500(env, monkeypatch) -> None:
    def broken(*_: object) -> dict:
        raise KeyError("x")

    monkeypatch.setattr(share_service, "client_assertion", broken)
    response = env.share()
    assert response.status_code == 500 and env.openemr.requests == []


def test_invalid_client_rereads_the_document_once_then_fails(env) -> None:
    env.openemr.clients.clear()  # OpenEMR no longer knows the client
    steps = envelope(env.share(), 502, "openemr-refused")
    assert names(steps) == [("token", "failed", 401), ("token", "failed", 401)]
    assert "read again and the request sent once more" in steps[0]["label"]
    assert len(env.openemr.calls("POST", r"/token$")) == 2  # once, then exactly one retry


def test_invalid_client_then_a_new_registration_succeeds(env, client_key) -> None:
    from fhir_proxy.share.testing import SCOPES, make_key

    new_key = make_key()
    env.openemr.clients.clear()
    env.openemr.register("synthetic-client-id-0002", new_key, "kid-2", SCOPES)
    first = {"done": False}

    def re_register(method, path, query, headers, body):  # noqa: ANN001, ANN202
        # The VM wrote the new registration's document just after the first refusal.
        result = env.openemr._token(body)  # noqa: SLF001
        if not first["done"]:
            first["done"] = True
            from fhir_proxy.share.testing import client_document

            doc = client_document(
                env.settings,
                key=new_key,
                certificate_pem=env.cert_pem,
                client_id="synthetic-client-id-0002",
                kid="kid-2",
            )
            env.doc_path.write_text(json.dumps(doc))
        return result

    env.openemr.override("POST", r"/token$", re_register)
    response = env.share()
    assert response.status_code == 200, response.get_data(as_text=True)
    assert names(response.get_json()["steps"])[:2] == [
        ("token", "failed", 401),
        ("token", "ok", 200),
    ]


def test_invalid_client_and_an_unusable_document_after_it(env) -> None:
    env.openemr.clients.clear()

    def refuse_and_break(method, path, query, headers, body):  # noqa: ANN001, ANN202
        env.doc_path.write_text("{}")
        return env.openemr._token(body)  # noqa: SLF001

    env.openemr.override("POST", r"/token$", refuse_and_break)
    steps = envelope(env.share(), 502, "openemr-refused")
    assert names(steps) == [("token", "failed", 401)]


def test_another_token_error_is_not_retried(env) -> None:
    env.openemr.override("POST", r"/token$", answer(400, {"error": "invalid_scope"}))
    response = env.share()
    steps = envelope(response, 502, "openemr-refused")
    assert names(steps) == [("token", "failed", 400)]
    assert "invalid_scope" in response.get_json()["operationOutcome"]["issue"][0]["diagnostics"]


@pytest.mark.parametrize(
    "body",
    [
        {"access_token": "x", "token_type": "mac", "expires_in": 300},
        {"access_token": "", "token_type": "Bearer", "expires_in": 300},
        {"access_token": "x", "token_type": "Bearer", "expires_in": "300"},
        {"token_type": "Bearer", "expires_in": 300},
    ],
)
def test_a_token_answer_that_is_not_one_is_refused(env, body) -> None:  # noqa: ANN001
    env.openemr.override("POST", r"/token$", answer(200, body))
    assert names(envelope(env.share(), 502, "openemr-refused")) == [("token", "failed", 200)]


def test_a_refused_access_token_is_never_reused(env) -> None:
    assert env.share(observation_id=BP_ID).status_code == 200
    env.openemr.override("GET", r"/Patient$", answer(401, {"error": "invalid_token"}), times=1)
    envelope(env.share(observation_id=HR_ID), 502, "openemr-refused")
    before = len(env.openemr.calls("POST", r"/token$"))
    assert env.share(observation_id=HR_ID).status_code == 200
    assert len(env.openemr.calls("POST", r"/token$")) == before + 1


def test_every_call_has_a_time_limit_within_the_budget(env) -> None:
    assert env.share().status_code == 200
    assert env.transport.timeouts and all(0 < t <= 5 for t in env.transport.timeouts)


def test_the_kid_is_the_documents(env) -> None:
    from urllib.parse import parse_qsl

    import jwt as pyjwt

    env.share()
    form = dict(parse_qsl(env.openemr.calls("POST", r"/token$")[0]["body"].decode()))
    assert pyjwt.get_unverified_header(form["client_assertion"])["kid"] == KID
