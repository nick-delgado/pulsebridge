"""The trace redaction rule (agreed with the page) and the wire shapes the page parses.

The page's lists and parse rules are read from web/src/api/ itself, so a change on either side
fails here rather than drifting."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fhir_proxy.extensions import (
    FHIR_STEP_SECRET_KEYS,
    NOT_SHOWN,
    TOKEN_STEP_SECRET_KEYS,
    trace_step,
)
from fhir_proxy.share.testing import BP_ID, HR_ID, raise_timeout

WEB_API = Path(__file__).resolve().parents[4] / "web" / "src" / "api"


def ts_array(source: str, name: str) -> list[str]:
    block = re.search(rf"{name}[^=]*=\s*\[(.*?)\]", source, re.S)
    assert block, f"{name} not found"
    return re.findall(r"'([^']*)'", block.group(1))


def test_the_proxy_and_the_page_hide_the_same_names() -> None:
    contract = (WEB_API / "contract.ts").read_text()
    assert (
        set(ts_array(contract, "export const SHARE_TOKEN_STEP_SECRETS")) == TOKEN_STEP_SECRET_KEYS
    )
    assert set(ts_array(contract, "export const SHARE_FHIR_STEP_SECRETS")) == FHIR_STEP_SECRET_KEYS
    # The FHIR-step list must never hide a FHIR element name.
    assert not {"code", "token", "value", "system", "display"} & FHIR_STEP_SECRET_KEYS


def step(name: str, request_body: object, response_body: object, **kw: object) -> dict:
    return trace_step(
        name,
        "x",
        method="POST",
        path="/p",
        request_body=request_body,
        status=200,
        response_body=response_body,
        result="ok",
        duration_ms=1,
        **kw,
    )


def test_an_observation_step_keeps_its_fhir_codes(env) -> None:
    body = env.share(observation_id=HR_ID).get_json()
    create = next(s for s in body["steps"] if s["step"] == "observation-create")
    sent = create["request"]["body"]
    assert sent["code"]["coding"][0]["code"] == "8867-4"
    assert sent["valueQuantity"]["code"] == "/min"
    assert sent["component"][0]["code"]["coding"][0]["code"] == "96935-2"
    stored = next(s for s in body["steps"] if s["step"] == "observation-read")["response"]["body"]
    assert stored["code"]["coding"][0]["code"] == "8867-4"
    assert NOT_SHOWN not in json.dumps(body["steps"][1:])


def test_a_token_step_hides_the_assertion_and_the_token(env) -> None:
    body = env.share(observation_id=BP_ID).get_json()
    token = body["steps"][0]
    assert token["step"] == "token"
    assert token["request"]["body"]["client_assertion"] == NOT_SHOWN
    assert token["response"]["body"]["access_token"] == NOT_SHOWN
    # What is not a credential stays readable.
    assert token["request"]["body"]["grant_type"] == "client_credentials"
    assert token["response"]["body"]["expires_in"] == 300
    issued = [r for r in env.openemr.tokens]
    assert not any(t in json.dumps(body) for t in issued)


def test_headers_never_appear_in_a_step(env) -> None:
    body = env.share(observation_id=BP_ID).get_json()
    for s in body["steps"]:
        assert set(s) == {"step", "label", "request", "response", "result", "durationMs"}
        assert set(s["request"]) == {"method", "url", "query", "body"}
        assert set(s["response"]) == {"status", "body"}
    text = json.dumps(body)
    assert "Bearer " not in text and "authorization" not in text.lower()


@pytest.mark.parametrize("key", sorted(FHIR_STEP_SECRET_KEYS))
def test_fhir_steps_still_hide_names_that_are_never_fhir(key: str) -> None:
    shown = step("patient-read", {"a": [{key.upper(): "sekrit"}]}, {key: "sekrit"})
    assert "sekrit" not in json.dumps(shown)


@pytest.mark.parametrize("key", sorted(TOKEN_STEP_SECRET_KEYS))
def test_the_token_step_hides_every_oauth_credential(key: str) -> None:
    shown = step("token", {key: "sekrit"}, {"nested": {key.title(): "sekrit"}})
    assert "sekrit" not in json.dumps(shown)


def test_code_and_token_are_hidden_only_in_the_token_step() -> None:
    fhir = step("observation-create", {"code": "8867-4", "token": "t"}, {"code": "c"})
    assert fhir["request"]["body"] == {"code": "8867-4", "token": "t"}
    oauth = step("token", {"code": "abc", "token": "t"}, None)
    assert oauth["request"]["body"] == {"code": NOT_SHOWN, "token": NOT_SHOWN}


def test_query_is_an_object_and_the_url_stays_a_path() -> None:
    shown = step("encounter-search", None, None, query={"patient": "p", "date": ["ge1", "le2"]})
    assert shown["request"]["url"] == "/p"
    assert shown["request"]["query"] == {"patient": "p", "date": ["ge1", "le2"]}
    assert step("token", None, None)["request"]["query"] is None


@pytest.mark.parametrize(
    "query",
    [
        {"access_token": "x"},
        {"Token": "x"},
        {"client_assertion": "x"},
        {"a b": "x"},
        {"x": 1},
        {"x": "a" * 257},
        {"x": "line\nbreak"},
        {},
        {"x": []},
    ],
)
def test_a_query_never_carries_a_credential_or_odd_values(query: dict) -> None:
    with pytest.raises(ValueError):
        step("patient-search", None, None, query=query)


@pytest.mark.parametrize(
    "path",
    ["https://openemr.example.test/x", "/x?code=abc", "//evil.example.test/x", "x", "/a#b", "/A B"],
)
def test_the_url_guard_is_kept(path: str) -> None:
    with pytest.raises(ValueError):
        trace_step(
            "x",
            "x",
            method="GET",
            path=path,
            request_body=None,
            status=200,
            response_body=None,
            result="ok",
            duration_ms=1,
        )


# ---- the page's parse rules (web/src/api/parse.ts, errors.ts) ---------------------------------


def page_rules() -> tuple[re.Pattern[str], set[str], set[str]]:
    parse = (WEB_API / "parse.ts").read_text()
    errors = (WEB_API / "errors.ts").read_text()
    path = re.search(r"function isPathOnly.*?/(\^.*?\$)/\.test", parse, re.S)
    assert path, "isPathOnly changed shape"
    results = set(ts_array(parse, "const STEP_RESULTS"))
    reasons = set(ts_array(errors, "const SHARE_REASONS"))
    return (
        re.compile(path.group(1).replace("(?!\\/)", "(?!/)").replace("\\/", "/")),
        results,
        reasons,
    )


def check_steps(steps: list[dict]) -> None:
    path, results, _ = page_rules()
    for s in steps:
        assert isinstance(s["step"], str) and isinstance(s["label"], str)
        assert path.fullmatch(s["request"]["url"]), s["request"]["url"]  # not SHARE_URL_HIDDEN
        query = s["request"]["query"]
        assert query is None or all(
            isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v))
            for v in query.values()
        )
        assert isinstance(s["response"]["status"], int)
        assert s["result"] in results  # never read as 'unknown'
        assert isinstance(s["durationMs"], int) and s["durationMs"] >= 0


def test_a_200_parses_cleanly_through_the_pages_rules(env) -> None:
    for observation_id in (BP_ID, HR_ID, BP_ID):
        body = env.share(observation_id=observation_id).get_json()
        assert body["outcome"] in ("shared", "already-shared")
        check_steps(body["steps"])
        for name, rtype in (
            ("patient", "Patient"),
            ("encounter", "Encounter"),
            ("observation", "Observation"),
        ):
            part = body["readBack"][name]
            for side in ("sent", "stored"):
                assert part[side] is None or part[side]["resourceType"] == rtype


def test_the_envelope_parses_cleanly_through_the_pages_rules(env) -> None:
    _, _, reasons = page_rules()
    env.openemr.override("POST", r"/Observation$", raise_timeout)
    body = env.share().get_json()
    check_steps(body["steps"])
    outcome = body["operationOutcome"]
    assert outcome["resourceType"] == "OperationOutcome" and isinstance(outcome["issue"], list)
    assert outcome["issue"][0]["details"]["coding"][0]["code"] in reasons
