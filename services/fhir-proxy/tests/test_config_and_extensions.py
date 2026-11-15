"""Configuration from the environment, and the extension point T15's share plugs into."""

from __future__ import annotations

import pytest
from fhir_proxy.config import ConfigError, ServiceConfig
from fhir_proxy.extensions import redact, trace_step
from fhir_proxy.testing import build_harness

ENV = {
    "GCP_PROJECT_ID": "example-project",
    "HEALTHCARE_LOCATION": "us-central1",
    "HEALTHCARE_DATASET_ID": "example-dataset",
    "FHIR_STORE_ID": "main",
    "AUDIT_FHIR_STORE_ID": "audit",
    "HL7V2_STORE_ID": "example-store",
    "PATIENT_ID": "PB-SYN-0001",
    "REVIEWER_PASSWORD_FILE": "/secrets/reviewer-password",
    "ALLOWED_ORIGINS": "https://dashboard.example.test",
    "CUFF_SIM_URL": "https://cuff-sim.example.test",
}


def test_defaults() -> None:
    config = ServiceConfig.from_env(ENV)
    assert config.session_ttl_seconds == 28800
    assert config.trusted_proxy_hops == 1
    assert config.cuff_forward_timeout == 95 and config.request_timeout == 120
    assert config.login_failures_per_address == 5 and config.login_window_seconds == 60
    assert config.health_sync_url is None and config.sample_patient is None


@pytest.mark.parametrize(
    ("change", "names"),
    [
        ({"PATIENT_ID": ""}, "PATIENT_ID"),
        ({"AUDIT_FHIR_STORE_ID": "main"}, "AUDIT_FHIR_STORE_ID"),
        ({"ALLOWED_ORIGINS": "*"}, "ALLOWED_ORIGINS"),
        ({"ALLOWED_ORIGINS": "https://dashboard.example.test/"}, "ALLOWED_ORIGINS"),
        ({"ALLOWED_ORIGINS": "http://dashboard.example.test"}, "ALLOWED_ORIGINS"),
        ({"ALLOWED_ORIGINS": "https://Dashboard.example.test"}, "ALLOWED_ORIGINS"),
        ({"CUFF_SIM_URL": "http://cuff-sim.example.test"}, "CUFF_SIM_URL"),
        ({"CUFF_SIM_URL": "https://user:pw@cuff-sim.example.test"}, "CUFF_SIM_URL"),
        ({"CUFF_SIM_URL": "https://cuff-sim.example.test/path"}, "CUFF_SIM_URL"),
        ({"CUFF_FORWARD_TIMEOUT_SECONDS": "60"}, "CUFF_FORWARD_TIMEOUT_SECONDS"),
        ({"CUFF_FORWARD_TIMEOUT_SECONDS": "115"}, "CUFF_FORWARD_TIMEOUT_SECONDS"),
        ({"SESSION_TTL_SECONDS": "1e9"}, "SESSION_TTL_SECONDS"),
        ({"TRUSTED_PROXY_HOPS": "9"}, "TRUSTED_PROXY_HOPS"),
        ({"PATH_PREFIX": "no-leading-slash"}, "PATH_PREFIX"),
        ({"REVIEWER_PASSWORD_FILE": "relative/path"}, "REVIEWER_PASSWORD_FILE"),
        ({"PATIENT_FAMILY_NAME": "ONLYONE"}, "PATIENT_ASSIGNING_AUTHORITY"),
        ({"GCP_PROJECT_ID": "Bad Project"}, "GCP_PROJECT_ID"),
    ],
)
def test_bad_values_are_refused_by_name_never_by_value(change: dict, names: str) -> None:
    with pytest.raises(ConfigError) as error:
        ServiceConfig.from_env({**ENV, **change})
    assert names in str(error.value)
    for value in change.values():
        if value and value not in names:
            assert value not in str(error.value)


def test_loopback_http_is_allowed_for_local_development() -> None:
    config = ServiceConfig.from_env(
        {**ENV, "ALLOWED_ORIGINS": "http://localhost:5173,https://dashboard.example.test"}
    )
    assert config.allowed_origins == ("http://localhost:5173", "https://dashboard.example.test")


def test_extension_routes_are_behind_the_token_check(tmp_path, monkeypatch) -> None:
    # A stand-in for T15's package, found and registered the way fhir_proxy.share would be.
    package = tmp_path / "ext" / "fake_share_extension"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "import pytest\n"
        "from fhir_proxy.routes import Route\n"
        "def register(proxy):\n"
        "    def share():\n"
        "        return {'outcome': 'shared', 'steps': []}\n"
        "    proxy.add_route(Route('POST', '/share', 'share', 'view', 'Share'), share)\n"
        "    proxy.set_openemr_probe(lambda: {'reachable': True, 'detail': None})\n"
        "    with pytest.raises(ValueError, match='session-token'):\n"
        "        proxy.add_route(Route('POST', '/open', 'open', 'login', 'x'), share)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "ext"))
    h = build_harness(tmp_path, extension_module="fake_share_extension")
    assert h.client.post("/share", json={}).status_code == 401
    assert h.client.post("/share", json={}, headers=h.auth()).status_code == 200
    h.sender.answer("/status", 200, {"googleHealth": "connected"})
    assert h.get("/status").get_json()["openemr"]["reachable"] is True
    doc = h.get("/fhir/metadata").get_json()["rest"][0]["documentation"]
    assert "`POST /share`" in doc


def test_trace_step_never_shows_secrets() -> None:
    step = trace_step(
        "token",
        "Token request",
        method="POST",
        path="/oauth2/default/token",
        request_body={"grant_type": "client_credentials", "client_assertion": "eyJ.secret.sig"},
        status=200,
        response_body={"access_token": "secret-token", "expires_in": 300},
        result="ok",
        duration_ms=12,
    )
    text = str(step)
    assert "eyJ.secret.sig" not in text and "secret-token" not in text
    assert step["request"]["body"]["client_assertion"] == "[not shown]"
    assert redact([{"nested": {"refresh_token": "x"}}]) == [
        {"nested": {"refresh_token": "[not shown]"}}
    ]
    with pytest.raises(ValueError):
        trace_step(
            "x",
            "x",
            method="GET",
            path="https://openemr.example.test/x",
            request_body=None,
            status=200,
            response_body=None,
            result="ok",
            duration_ms=1,
        )
