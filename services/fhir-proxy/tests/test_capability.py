"""The CapabilityStatement lists exactly what is implemented and states the security truthfully."""

from __future__ import annotations

import re

from fhir_proxy import search
from fhir_proxy.capability import implemented_interactions


def _statement(harness) -> dict:  # noqa: ANN001
    response = harness.get("/fhir/metadata")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/fhir+json"
    return response.get_json()


def test_capability_statement_equals_the_routes(harness) -> None:
    statement = _statement(harness)
    assert statement["resourceType"] == "CapabilityStatement"
    assert statement["fhirVersion"] == "4.0.1"
    assert statement["kind"] == "instance"
    assert statement["format"] == ["json"]
    # FHIR interactions: what the app's URL map actually serves under /fhir/<Type>.
    served = set()
    for rule in harness.app.url_map.iter_rules():
        match = re.fullmatch(r"/fhir/([A-Z][A-Za-z]+)", rule.rule)
        if match and "GET" in rule.methods:
            served.add((match.group(1), "search-type"))
        if re.fullmatch(r"/fhir/([A-Z][A-Za-z]+)/<[^>]+>", rule.rule):
            served.add((rule.rule.split("/")[2], "read"))
    assert (
        implemented_interactions(statement)
        == served
        == {
            ("Observation", "search-type"),
            ("Observation", "read"),
        }
    )
    # Every other route is named in the documentation, and nothing that is not a route is.
    documented = set(re.findall(r"`(GET|POST) ([^`]+)`", statement["rest"][0]["documentation"]))
    others = {
        (method, rule.rule)
        for rule in harness.app.url_map.iter_rules()
        if not rule.rule.startswith("/fhir/")
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert documented == others


def test_search_parameters_equal_what_the_validator_accepts(harness) -> None:
    (resource,) = _statement(harness)["rest"][0]["resource"]
    declared = {p["name"]: p["type"] for p in resource["searchParam"]}
    assert declared == search.SEARCH_PARAMETERS == {"code": "token", "date": "date"}
    # Every declared parameter is accepted, and an undeclared standard one is refused.
    for name, value in {
        "code": "http://loinc.org|8867-4",
        "date": "gt2026-01-01T00:00:00Z",
        "_sort": "date",
        "_count": "10",
    }.items():
        pairs = [("code", "http://loinc.org|8867-4")] if name != "code" else []
        search.parse_observation_query(pairs + [(name, value)])
    response = harness.get("/fhir/Observation?code=http://loinc.org|8867-4&value-quantity=gt60")
    assert response.status_code == 400


def test_security_is_stated_truthfully(harness) -> None:
    security = _statement(harness)["rest"][0]["security"]
    assert security["cors"] is False  # same origin, no CORS headers
    service = security["service"]
    # No code from restful-security-service is claimed, SMART-on-FHIR least of all.
    assert all("coding" not in s for s in service)
    assert "not SMART on FHIR" in service[0]["text"]
    assert "bot check" in security["description"]
    assert "__session cookie" in security["description"]
    assert "24 hours" in security["description"]
    assert "password" not in str(security).lower()
    assert "SMART-on-FHIR" not in str(_statement(harness))


def test_result_parameters_are_not_search_params_and_the_date_is_the_start_date(harness) -> None:
    from datetime import UTC, datetime

    statement = _statement(harness)
    (resource,) = statement["rest"][0]["resource"]
    names = {p["name"] for p in resource["searchParam"]}
    assert names == {"code", "date"}
    assert "_sort" in resource["documentation"] and "_count" in resource["documentation"]
    assert statement["date"] == datetime.now(UTC).date().isoformat()


def test_the_session_mentions_carry_the_prefix(tmp_path) -> None:
    from fhir_proxy.testing import build_harness

    h = build_harness(tmp_path, path_prefix="/api")
    statement = h.client.get("/api/fhir/metadata", headers=h.auth()).get_json()["rest"][0]
    description = statement["security"]["description"]
    assert "/api/auth/session" in description and "/api/auth/" in description
    assert " /auth/" not in description
    # The three session routes are listed as the ones that need no session.
    for path in ("/api/auth/config", "/api/auth/session", "/api/auth/logout"):
        assert f"{path}`" in statement["documentation"]
    assert statement["documentation"].count("(no session needed)") == 3
