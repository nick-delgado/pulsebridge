"""The gate: every route but the session routes needs the __session cookie; no CORS; the
security headers; hostile input."""

from __future__ import annotations

import re

import pytest
from fhir_proxy.testing import ORIGIN, build_harness

PUBLIC = {("GET", "/auth/config"), ("POST", "/auth/session"), ("POST", "/auth/logout")}


def _concrete(rule: str) -> str:
    return re.sub(r"<[^>]+>", "some-id", rule)


def _routes(harness) -> list[tuple[str, str]]:  # noqa: ANN001
    out = []
    for rule in harness.app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            out.append((method, _concrete(rule.rule)))
    return sorted(out)


def test_the_app_serves_exactly_the_route_table(harness) -> None:
    table = sorted(
        (r.method, _concrete(r.path)) for r in harness.app.extensions["pulsebridge_routes"]
    )
    assert _routes(harness) == table
    assert set(table) >= PUBLIC
    assert ("POST", "/auth/login") not in table


def test_exactly_the_three_session_routes_are_public(harness) -> None:
    public = {(r.method, r.path) for r in harness.app.extensions["pulsebridge_routes"] if r.public}
    assert public == PUBLIC


def test_every_other_route_refuses_a_request_without_the_cookie(harness) -> None:
    checked = 0
    for method, path in _routes(harness):
        if (method, path) in PUBLIC:
            continue
        response = harness.client.open(path, method=method, json={}, headers={"Origin": ORIGIN})
        assert response.status_code == 401, (method, path)
        assert response.headers["WWW-Authenticate"].startswith("Cookie ")
        outcome = response.get_json()
        assert outcome["resourceType"] == "OperationOutcome"
        assert outcome["issue"][0]["details"]["coding"][0]["code"] == "session-required"
        checked += 1
    assert checked >= 8
    # Nothing was asked of the stores, of cuff-sim or of Google on the way.
    assert harness.fake.requests == []
    assert harness.sender.calls == []
    assert harness.fraud.assessments == 0


def test_unknown_paths_also_need_the_cookie(harness) -> None:
    assert harness.client.get("/admin").status_code == 401
    assert harness.client.get("/fhir/Patient").status_code == 401
    assert harness.get("/fhir/Patient").status_code == 404


@pytest.mark.parametrize("name", ["access_token", "token", "id_token", "__session"])
def test_a_token_in_the_query_string_is_refused(harness, name: str) -> None:
    token = harness.login()
    response = harness.client.get(f"/status?{name}={token}")
    assert response.status_code == 400
    assert response.get_json()["issue"][0]["details"]["coding"][0]["code"] == "token-in-query"
    # Refused even together with a valid cookie, and on the public routes too.
    assert (
        harness.client.get(f"/status?{name}={token}", headers=harness.auth(token)).status_code
        == 400
    )
    assert harness.client.get(f"/auth/config?{name}=x").status_code == 400


def test_security_headers_on_every_response(harness) -> None:
    token = harness.login()
    responses = [
        harness.client.get("/status"),  # 401
        harness.start_session("junk"),  # 403
        harness.client.post("/auth/session"),  # 403 origin
        harness.client.get("/auth/config"),
        harness.client.get("/fhir/metadata", headers=harness.auth(token)),
        harness.client.options("/status", headers={"Origin": "https://evil.example.test"}),
    ]
    for response in responses:
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "Set-Cookie" not in response.headers
        assert "Access-Control-Allow-Origin" not in response.headers
        server = response.headers.get("Server", "")
        assert not re.search(r"[0-9]", server), "no server version"


# ---- no CORS -----------------------------------------------------------------------------------


@pytest.mark.parametrize("origin", [ORIGIN, "https://evil.example.test"])
def test_a_preflight_gets_no_cors_answer(harness, origin: str) -> None:
    response = harness.client.options(
        "/cuff/readings",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code in (401, 403)
    assert not [name for name, _ in response.headers if name.lower().startswith("access-control")]


def test_several_origins_can_be_configured(tmp_path) -> None:
    h = build_harness(tmp_path, allowed_origins=(ORIGIN, "http://localhost:5173"))
    token = h.login()
    response = h.client.post(
        "/auth/logout", headers={"Origin": "http://localhost:5173", "Cookie": f"__session={token}"}
    )
    assert response.status_code == 204


# ---- prefix ------------------------------------------------------------------------------------


def test_path_prefix_for_the_firebase_rewrite(tmp_path) -> None:
    h = build_harness(tmp_path, path_prefix="/api")
    response = h.start_session()
    assert response.status_code == 200
    auth = h.auth(h.login())
    assert h.client.get("/api/fhir/metadata", headers=auth).status_code == 200
    assert h.client.get("/fhir/metadata", headers=auth).status_code == 404
    assert h.client.post("/auth/session", json={}, headers={"Origin": ORIGIN}).status_code == 401


# ---- hostile ids and paths ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/trace/heart-rate/..%2F..%2FPatient%2Fpat-1",
        "/trace/heart-rate/https:%2F%2Fevil.example.test%2Fx",
        "/trace/heart-rate/Patient%2Fpat-1",
        "/trace/heart-rate/a%00b",
        "/trace/heart-rate/" + "a" * 65,
        "/trace/heart-rate/abc_def",
        "/trace/cuff-attempts/..",
        "/trace/cuff-attempts/%2E%2E%2Fmetadata",
        "/trace/cuff-attempts/x%20y",
    ],
)
def test_hostile_ids_never_reach_the_store(harness, path: str) -> None:
    response = harness.get(path)
    assert response.status_code in (400, 404)
    assert response.get_json()["resourceType"] == "OperationOutcome"
    # Only the session happened; no store request was built from the id.
    assert [r for r in harness.fake.requests if "/fhir/" in r[1] and "Patient" not in r[1]] == []


def test_store_and_patient_cannot_be_chosen_by_the_caller(harness, patient) -> None:
    for query in (
        "patient=Patient/other",
        "subject=Patient/other",
        "_store=audit",
        "code=http://loinc.org|8867-4&patient=Patient/other",
    ):
        response = harness.get(f"/fhir/Observation?{query}")
        assert response.status_code == 400, query
    assert harness.fake.calls("GET", "Observation") == []
