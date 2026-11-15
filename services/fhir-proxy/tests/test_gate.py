"""The gate: every route but login needs a token; CORS; security headers; hostile input."""

from __future__ import annotations

import re

import pytest
from fhir_proxy.testing import ORIGIN, PASSWORD, build_harness


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
    assert ("POST", "/auth/login") in table


def test_every_route_but_login_refuses_a_request_without_a_token(harness) -> None:
    checked = 0
    for method, path in _routes(harness):
        if path == "/auth/login":
            continue
        response = harness.client.open(path, method=method, json={})
        assert response.status_code == 401, (method, path)
        assert response.headers["WWW-Authenticate"] == 'Bearer realm="pulsebridge"'
        assert response.get_json()["resourceType"] == "OperationOutcome"
        checked += 1
    assert checked >= 8
    # Nothing was asked of the stores or of cuff-sim on the way.
    assert harness.fake.requests == []
    assert harness.sender.calls == []


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Bearer ",
        "bearer abc.def.ghi",
        "Basic dXNlcjpwYXNz",
        "Bearer abc.def.ghi extra",
        "Bearer abc.def",
        "Token abc.def.ghi",
    ],
)
def test_malformed_authorization_header_is_401(harness, header: str) -> None:
    response = harness.client.get("/status", headers={"Authorization": header})
    assert response.status_code == 401
    assert "error=" in response.headers["WWW-Authenticate"]


def test_unknown_paths_also_need_a_token(harness) -> None:
    assert harness.client.get("/admin").status_code == 401
    assert harness.client.get("/fhir/Patient").status_code == 401
    token = harness.login()
    assert (
        harness.get("/fhir/Patient", headers={"Authorization": f"Bearer {token}"}).status_code
        == 404
    )


@pytest.mark.parametrize("name", ["access_token", "token", "id_token"])
def test_a_token_in_the_query_string_is_refused(harness, name: str) -> None:
    token = harness.login()
    response = harness.client.get(f"/status?{name}={token}")
    assert response.status_code == 400
    assert 'error="invalid_request"' in response.headers["WWW-Authenticate"]
    # Refused even together with a valid header.
    response = harness.client.get(
        f"/status?{name}={token}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 400


def test_security_headers_on_every_response(harness) -> None:
    token = harness.login()
    responses = [
        harness.client.get("/status"),  # 401
        harness.client.post("/auth/login", json={"password": "wrong"}),  # 401
        harness.client.get("/fhir/metadata", headers={"Authorization": f"Bearer {token}"}),
        harness.client.options("/status", headers={"Origin": "https://evil.example.test"}),
    ]
    for response in responses:
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "Set-Cookie" not in response.headers
        assert "Origin" in response.headers.get_all("Vary")
        server = response.headers.get("Server", "")
        assert not re.search(r"[0-9]", server), "no server version"


# ---- CORS ------------------------------------------------------------------------------------


def test_preflight_for_the_allowed_origin_needs_no_token(harness) -> None:
    response = harness.client.options(
        "/cuff/readings",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert response.headers["Access-Control-Allow-Methods"] == "POST"
    assert "Access-Control-Allow-Credentials" not in response.headers
    assert response.headers["Access-Control-Allow-Origin"] != "*"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example.test", "Access-Control-Request-Method": "GET"},
        {"Origin": ORIGIN + ".evil.test", "Access-Control-Request-Method": "GET"},
        {"Origin": "null", "Access-Control-Request-Method": "GET"},
        {"Origin": ORIGIN, "Access-Control-Request-Method": "DELETE"},
        {
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-custom",
        },
    ],
)
def test_preflight_refused_for_other_origins_methods_and_headers(harness, headers) -> None:
    response = harness.client.options("/status", headers=headers)
    assert response.status_code == 403
    assert "Access-Control-Allow-Origin" not in response.headers


def test_preflight_for_an_unknown_path_looks_like_any_refusal(harness) -> None:
    unknown = harness.client.options(
        "/nope", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}
    )
    refused = harness.client.options(
        "/status",
        headers={"Origin": "https://evil.example.test", "Access-Control-Request-Method": "GET"},
    )
    assert unknown.status_code == refused.status_code == 403
    assert unknown.get_data() == refused.get_data()


def test_actual_responses_carry_cors_only_for_the_allowed_origin(harness) -> None:
    allowed = harness.client.post(
        "/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
    )
    assert allowed.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Retry-After" in allowed.headers["Access-Control-Expose-Headers"]
    other = harness.client.post(
        "/auth/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example.test"}
    )
    assert "Access-Control-Allow-Origin" not in other.headers


def test_several_origins_can_be_configured(tmp_path) -> None:
    h = build_harness(tmp_path, allowed_origins=(ORIGIN, "http://localhost:5173"))
    response = h.client.options(
        "/status",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"


# ---- prefix ------------------------------------------------------------------------------------


def test_path_prefix_for_the_firebase_rewrite(tmp_path) -> None:
    h = build_harness(tmp_path, path_prefix="/api")
    response = h.client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    token = response.get_json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert h.client.get("/api/fhir/metadata", headers=auth).status_code == 200
    assert h.client.get("/fhir/metadata", headers=auth).status_code == 404


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
    # Only the login happened; no store request was built from the id.
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
