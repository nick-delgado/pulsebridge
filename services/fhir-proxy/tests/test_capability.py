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
    assert implemented_interactions(statement) == served == {("Observation", "search-type")}
    # Every other route is named in the documentation, and nothing that is not a route is.
    documented = set(re.findall(r"`(GET|POST) ([^`]+)`", statement["rest"][0]["documentation"]))
    others = {
        (method, rule.rule)
        for rule in harness.app.url_map.iter_rules()
        if not rule.rule.startswith("/fhir/") and rule.endpoint != "static"
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert documented == others


def test_search_parameters_equal_what_the_validator_accepts(harness) -> None:
    (resource,) = _statement(harness)["rest"][0]["resource"]
    declared = {p["name"]: p["type"] for p in resource["searchParam"]}
    assert declared == search.SEARCH_PARAMETERS
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
    assert security["cors"] is True
    service = security["service"]
    # No code from restful-security-service is claimed, SMART-on-FHIR least of all.
    assert all("coding" not in s for s in service)
    assert "not SMART on FHIR" in service[0]["text"]
    assert "shared demo password" in security["description"]
    assert "8 hours" in security["description"]
    assert "SMART-on-FHIR" not in str(_statement(harness))
