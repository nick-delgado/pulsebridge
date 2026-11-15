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
    "PUBLIC_BASE_URL": "https://proxy.example.test",
}


def test_defaults() -> None:
    config = ServiceConfig.from_env(ENV)
    assert config.session_ttl_seconds == 28800
    assert config.trusted_proxy_hops == 1
    assert config.cuff_forward_timeout == 55 and config.request_timeout == 120
    assert config.request_budget == 50  # under Firebase Hosting's 60 s
    assert config.public_base_url == "https://proxy.example.test"
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
        ({"CUFF_FORWARD_TIMEOUT_SECONDS": "5"}, "CUFF_FORWARD_TIMEOUT_SECONDS"),
        ({"REQUEST_BUDGET_SECONDS": "115"}, "REQUEST_BUDGET_SECONDS"),
        ({"PUBLIC_BASE_URL": "http://proxy.example.test"}, "PUBLIC_BASE_URL"),
        ({"PUBLIC_BASE_URL": "https://proxy.example.test/api"}, "PUBLIC_BASE_URL"),
        ({"PUBLIC_BASE_URL": "https://proxy.example.test/?q=1"}, "PUBLIC_BASE_URL"),
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


def _extension(tmp_path, monkeypatch, body: str, name: str):  # noqa: ANN001, ANN202
    package = tmp_path / "ext" / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from fhir_proxy.routes import Route\n"
        "def handler():\n"
        "    return {'replaced': True}\n"
        "def register(proxy):\n" + body
    )
    monkeypatch.syspath_prepend(str(tmp_path / "ext"))
    return name


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (
            "    proxy.add_route(Route('POST', '/auth/login', 'login2', 'view', 'x'), handler)\n",
            "exist",
        ),
        (
            "    proxy.add_route(Route('GET', '/other', 'login', 'view', 'x'), handler)\n",
            "endpoint",
        ),
        (
            "    proxy.add_route(Route('GET', '/other', 'status', 'view', 'x'), handler)\n",
            "endpoint",
        ),
        (
            "    proxy.add_route(Route('GET', '/open', 'open', 'login', 'x'), handler)\n",
            "session-token",
        ),
        (
            "    proxy.add_route(Route('POST', '/share', 'share', 'view', 'x'), handler)\n"
            "    proxy.add_route(Route('POST', '/share', 'share2', 'view', 'x'), handler)\n",
            "exist",
        ),
        (
            "    proxy.add_route(Route('POST', '/share', 'share', 'view', 'x'), handler)\n"
            "    proxy.add_route(Route('GET', '/share-b', 'share', 'view', 'x'), handler)\n",
            "endpoint",
        ),
    ],
)
def test_an_extension_cannot_replace_or_open_routes(tmp_path, monkeypatch, line, message) -> None:
    name = _extension(tmp_path, monkeypatch, line, f"ext_{abs(hash(line)) % 10**8}")
    with pytest.raises(ValueError, match=message):
        build_harness(tmp_path, extension_module=name)


def test_the_public_set_comes_from_the_core_routes_only(tmp_path, monkeypatch) -> None:
    from fhir_proxy.extensions import ProxyContext

    # Even a context that skipped add_route's checks could not make a route public.
    real = ProxyContext.add_route
    monkeypatch.setattr(
        ProxyContext, "add_route", lambda self, route, handler: self.routes.append((route, handler))
    )
    name = _extension(
        tmp_path,
        monkeypatch,
        "    proxy.add_route(Route('GET', '/open', 'open', 'login', 'x'), handler)\n",
        "ext_public",
    )
    h = build_harness(tmp_path, extension_module=name)
    monkeypatch.setattr(ProxyContext, "add_route", real)
    assert h.client.get("/open").status_code == 401


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "client_secret",
        "CLIENT_ASSERTION",
        "password",
        "Token",
        "refresh_token",
        "code",
    ],
)
def test_trace_step_redacts_every_secret_key_whatever_its_case(key: str) -> None:
    step = trace_step(
        "token",
        "x",
        method="POST",
        path="/oauth2/default/token",
        request_body={key: "sekrit-value", "nested": [{key: "sekrit-value"}]},
        status=200,
        response_body={key: "sekrit-value"},
        result="ok",
        duration_ms=1,
    )
    assert "sekrit-value" not in str(step)


@pytest.mark.parametrize(
    "path", ["https://openemr.example.test/x", "/x?code=abc", "//evil.example.test/x", "x", "/a#b"]
)
def test_trace_step_path_has_no_scheme_host_or_query(path: str) -> None:
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


def test_changing_the_context_s_core_table_does_not_help(tmp_path, monkeypatch) -> None:
    name = _extension(
        tmp_path,
        monkeypatch,
        "    proxy.core_routes = ()\n"
        "    proxy.add_route(Route('POST', '/auth/login', 'login2', 'view', 'x'), handler)\n",
        "ext_core_mutation",
    )
    with pytest.raises(ValueError, match="exist"):
        build_harness(tmp_path, extension_module=name)


@pytest.mark.parametrize(
    "path",
    [
        "/fhir/Observation/<int:n>",
        "/fhir/Observation/<path:p>",
        "/trace/<path:rest>",
        "/<path:any>",
    ],
)
def test_a_pattern_that_can_match_a_core_url_is_refused(tmp_path, monkeypatch, path: str) -> None:
    name = _extension(
        tmp_path,
        monkeypatch,
        f"    proxy.add_route(Route('GET', '{path}', 'grab', 'view', 'x'), handler)\n",
        f"ext_shadow_{abs(hash(path)) % 10**8}",
    )
    with pytest.raises(ValueError, match="core route"):
        build_harness(tmp_path, extension_module=name)


def test_the_app_refuses_to_start_if_a_core_url_is_shadowed(tmp_path, monkeypatch) -> None:
    from fhir_proxy import extensions

    # Even if the registration check were skipped, the start-up check catches the overlap.
    monkeypatch.setattr(extensions, "shadows_core", lambda route: False)
    name = _extension(
        tmp_path,
        monkeypatch,
        "    rule = '/fhir/Observation/<int:n>'\n"
        "    proxy.add_route(Route('GET', rule, 'grab', 'view', 'x'), handler)\n",
        "ext_startup_check",
    )
    with pytest.raises(RuntimeError, match="shadows"):
        build_harness(tmp_path, extension_module=name)
