"""Nothing secret or private in stdout, stderr, logs, browser pages or exception text, on every
path. Each scenario runs the whole flow with distinctive synthetic values and searches
everything captured (Python and file-descriptor level, so a stray traceback or http.server log
line would be caught) for each value, also URL-encoded."""

import logging

import pytest
from connect_google_health import cli
from connect_google_health.testing import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    FHIR_PATH,
    HEALTH_USER_ID,
    PRIVATE_VALUES,
    REFRESH_TOKEN,
    FakeBrowser,
    FakeSecretStore,
    Harness,
    config,
    identity_response,
    leaked,
    token_response,
)
from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.testing import FakeTransport, json_response
from pulsebridge_gcp.transport import HttpResponse


def _google(*token, identity=None) -> FakeTransport:
    google = FakeTransport()
    google.add("POST", "/token", *token)
    google.add("GET", "/v4/users/me/identity", identity or identity_response())
    return google


def _fhir(response) -> FakeTransport:
    fhir = FakeTransport()
    fhir.add("POST", FHIR_PATH, response)
    return fhir


class Exploding:
    """A store whose every call raises an unexpected exception quoting secrets."""

    def access_latest(self, secret):
        raise RuntimeError(f"boom {CLIENT_SECRET} {REFRESH_TOKEN}")

    add_version = disable_versions_before = access_latest


def _echoing_error_body():
    # A token-endpoint error body that quotes everything back, as a hostile server might.
    return json_response(
        400,
        {
            "error": f"bad {CLIENT_SECRET}",
            "error_description": f"{AUTH_CODE} {CLIENT_ID} {CLIENT_SECRET}",
        },
    )


SCENARIOS = {
    "success": lambda: Harness(),
    "success-prompted-client": lambda: Harness(
        store=FakeSecretStore.empty(), prompts=[CLIENT_ID, CLIENT_SECRET]
    ),
    "browser-not-opened": lambda: Harness(browser=FakeBrowser(opens=False)),
    "state-mismatch": lambda: Harness(
        browser=FakeBrowser(answer=lambda s: f"state=x{AUTH_CODE}&code={AUTH_CODE}")
    ),
    "access-denied": lambda: Harness(
        browser=FakeBrowser(answer=lambda s: f"state={s}&error=access_denied")
    ),
    "error-echoing-a-secret": lambda: Harness(
        browser=FakeBrowser(answer=lambda s: f"state={s}&error={CLIENT_SECRET}")
    ),
    "missing-code": lambda: Harness(browser=FakeBrowser(answer=lambda s: f"state={s}")),
    "token-invalid-grant": lambda: Harness(
        google=_google(json_response(400, {"error": "invalid_grant"}))
    ),
    "token-error-echoing-secrets": lambda: Harness(google=_google(_echoing_error_body())),
    "token-network-error": lambda: Harness(
        google=_google(RetryableError("POST", None, server_message=CLIENT_SECRET))
    ),
    "token-non-json": lambda: Harness(
        google=_google(HttpResponse(200, f"<p>{ACCESS_TOKEN} {REFRESH_TOKEN}</p>".encode()))
    ),
    "token-no-refresh-token": lambda: Harness(google=_google(token_response(refresh_token=None))),
    "token-scope-not-granted": lambda: Harness(google=_google(token_response(scope="other"))),
    "identity-fails": lambda: Harness(
        google=_google(
            token_response(),
            identity=json_response(500, {"error": {"message": HEALTH_USER_ID}}),
        )
    ),
    "fhir-refuses": lambda: Harness(
        fhir=_fhir(
            json_response(
                400,
                {
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "invalid",
                            "diagnostics": f"Syntheticfamily {HEALTH_USER_ID}",
                        }
                    ],
                },
            )
        )
    ),  # fmt: skip
    "fhir-unreachable": lambda: Harness(
        fhir=_fhir(RetryableError("POST", None, server_message=HEALTH_USER_ID))
    ),
    "unexpected-exception": lambda: Harness(store=Exploding()),
    "paste-mode": lambda: Harness(prompts=[lambda h: h.paste_redirect()]),
    "paste-mode-wrong-state": lambda: Harness(
        prompts=[lambda h: h.paste_redirect(f"state=x&code={AUTH_CODE}")]
    ),
}

PASTE = {"paste-mode", "paste-mode-wrong-state"}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_nothing_private_is_output(name, capfd, caplog):
    caplog.set_level(logging.DEBUG)
    harness = SCENARIOS[name]()
    try:
        code = cli.main_with_config(config(paste=name in PASTE), harness.deps())
    finally:
        harness.browser.join()
    assert isinstance(code, int)
    captured = capfd.readouterr()
    everything = "\n".join(
        [harness.output(), captured.out, captured.err, caplog.text]
        + [r.getMessage() for r in caplog.records]
    )
    values = PRIVATE_VALUES
    if name in PASTE:
        # Paste mode must print Google's address for the user to open, which holds the client
        # ID (a public value in OAuth). It is the only place the client ID may appear.
        auth_url = harness.printed_authorization_url()
        assert CLIENT_ID in auth_url
        everything = everything.replace(auth_url, "<authorization address>")
    assert leaked(everything, values) == []


def test_the_failure_scenarios_really_fail():
    """So the search above covers the error paths, not just the happy one."""
    expected = {
        "success": 0, "success-prompted-client": 0, "browser-not-opened": 0, "paste-mode": 0,
        "identity-fails": 3, "fhir-refuses": 3, "fhir-unreachable": 3,
    }  # fmt: skip
    for name, make in SCENARIOS.items():
        harness = make()
        try:
            code = cli.main_with_config(config(paste=name in PASTE), harness.deps())
        finally:
            harness.browser.join()
        assert code == expected.get(name, cli.EXIT_NOT_CONNECTED), name


def test_unexpected_exception_shows_type_and_place_only():
    harness = Harness(store=Exploding())
    assert cli.main_with_config(config(), harness.deps()) == cli.EXIT_NOT_CONNECTED
    err = harness.err.getvalue()
    assert "Unexpected error: RuntimeError at test_no_leaks.py:" in err
    assert "boom" not in err


def test_a_credentials_error_gets_the_gcloud_hint():
    class RefreshError(Exception):  # the name google.auth uses
        pass

    class Expired:
        def access_latest(self, secret):
            raise RefreshError(f"Reauthentication is needed {CLIENT_SECRET}")

    harness = Harness(store=Expired())
    assert cli.main_with_config(config(), harness.deps()) == cli.EXIT_NOT_CONNECTED
    err = harness.err.getvalue()
    assert "gcloud auth application-default login" in err
    assert CLIENT_SECRET not in err and "Reauthentication" not in err
