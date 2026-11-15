"""The whole connect flow, end to end, with a scripted browser driving the real loopback
listener, a fake Secret Manager, and FakeTransport for Google and the FHIR store."""

import getpass
from urllib.parse import parse_qs, urlsplit

import pytest
from connect_google_health import cli, pkce
from connect_google_health.oauth import DEFAULT_SCOPE
from connect_google_health.testing import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    FHIR_PATH,
    HEALTH_USER_ID,
    NOW,
    REFRESH_TOKEN,
    FakeBrowser,
    FakeSecretStore,
    Harness,
    config,
    fake,
    fhir_response,
    token_response,
)
from pulsebridge_gcp.testing import json_response

REFRESH = "pb-google-health-refresh-token"
# Secret Manager secret names (not values).
CLIENT_ID_SECRET = "pb-google-health-client-id"  # noqa: S105
CLIENT_SECRET_SECRET = "pb-google-health-client-secret"  # noqa: S105


def run(harness: Harness, **overrides) -> int:
    try:
        return cli.main_with_config(config(**overrides), harness.deps())
    finally:
        harness.browser.join()


def token_form(harness: Harness) -> dict:
    (request,) = harness.google.calls("POST", "/token")
    return {k: v[0] for k, v in parse_qs(request.body.decode()).items()}


# ---- the happy path -------------------------------------------------------------------------


def test_connects_stores_the_refresh_token_and_writes_the_patient():
    h = Harness()
    assert run(h) == cli.EXIT_OK

    # The browser was pointed at the local /start link, which redirected to Google.
    (opened,) = h.browser.opened
    assert opened.startswith("http://127.0.0.1:") and opened.endswith("/start")
    (auth_url,) = h.browser.authorization_urls
    params = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
    port = urlsplit(opened).port
    assert params["redirect_uri"] == f"http://127.0.0.1:{port}"
    assert params["client_id"] == CLIENT_ID
    assert params["scope"] == DEFAULT_SCOPE
    assert (params["access_type"], params["prompt"]) == ("offline", "consent")

    # The exchange used the same redirect address and the verifier behind the challenge.
    form = token_form(h)
    assert form["code"] == AUTH_CODE
    assert form["redirect_uri"] == params["redirect_uri"]
    assert form["client_secret"] == CLIENT_SECRET
    assert params["code_challenge_method"] == "S256"
    assert pkce.code_challenge_s256(form["code_verifier"]) == params["code_challenge"]

    # The refresh token is the new latest version; the client was not rewritten.
    assert h.store.values(REFRESH) == [REFRESH_TOKEN]
    assert h.store.values(CLIENT_ID_SECRET) == [CLIENT_ID]
    assert ("add", CLIENT_ID_SECRET) not in h.store.calls
    out = h.out.getvalue()
    assert f"Stored the refresh token as version 1 of secret {REFRESH}." in out

    # Identity call with the new access token.
    (identity,) = h.google.calls("GET", "/v4/users/me/identity")
    assert identity.url == "https://health.googleapis.com/v4/users/me/identity"
    assert identity.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"

    # The Patient: T06's setup Bundle through executeBundle.
    (write,) = h.fhir.requests
    assert write.method == "POST"
    assert write.url == (
        "https://healthcare.googleapis.com/v1/projects/demo-project-1/locations/us-central1"
        "/datasets/pulsebridge/fhirStores/fhir/fhir"
    )
    bundle = write.json()
    assert bundle["type"] == "transaction"
    assert len(bundle["entry"]) == 1  # the Patient only: no Device (see patient.py)
    (entry,) = bundle["entry"]
    assert entry["request"] == {
        "method": "PUT",
        "url": "Patient?identifier=urn:pulsebridge:patient|PB-SYN-0001",
    }
    patient = entry["resource"]
    assert [(i["system"], i["value"]) for i in patient["identifier"]] == [
        ("urn:pulsebridge:patient", "PB-SYN-0001"),
        ("urn:pulsebridge:google-health-user-id", HEALTH_USER_ID),
    ]
    assert patient["name"] == [
        {"use": "official", "family": "Syntheticfamily", "given": ["Syntheticgiven"]}
    ]
    assert (patient["gender"], patient["birthDate"]) == ("female", "1980-04-12")
    assert "Patient record created" in out
    assert h.err.getvalue() == ""


def test_the_real_dependencies_prompt_without_echo():
    assert cli.Dependencies.__dataclass_fields__["prompt_hidden"].default is getpass.getpass


def test_writes_to_exactly_the_three_secrets():
    h = Harness(store=FakeSecretStore.empty())
    h.prompts = [CLIENT_ID, CLIENT_SECRET]
    assert run(h) == cli.EXIT_OK
    touched = {secret for _op, secret in h.store.calls}
    assert touched == {CLIENT_ID_SECRET, CLIENT_SECRET_SECRET, REFRESH}


def test_reports_the_expiry_google_gives_and_how_to_publish():
    h = Harness()
    run(h)
    out = h.out.getvalue()
    assert "expires in 7.0 days, at 2026-01-22 11:59 UTC" in out
    assert 'publishing status is "Testing"' in out
    assert '"Publish app"' in out


def test_without_an_expiry_it_says_it_cannot_know_the_status():
    h = Harness()
    h.google = type(h.google)()
    h.google.add("POST", "/token", token_response(refresh_token_expires_in=None))
    h.google.add("GET", "/v4/users/me/identity", json_response(200, {"healthUserId": "u1"}))
    run(h)
    out = h.out.getvalue()
    assert "cannot see your OAuth app's publishing status" in out
    assert 'If the status is "Testing", it expires in 7 days, around 2026-01-22 12:00 UTC' in out


def test_describe_expiry_long_lived_token_does_not_claim_testing():
    from connect_google_health.oauth import Tokens

    lines = cli.describe_expiry(Tokens("a", "r", 3599, None, 90 * 86400), NOW)
    assert "90.0 days" in lines[0]
    assert not any('"Testing"' in line for line in lines)


# ---- the client ID and secret ---------------------------------------------------------------


def test_absent_client_is_prompted_for_and_stored():
    h = Harness(store=FakeSecretStore.empty())
    h.prompts = [f"  {CLIENT_ID}\n", CLIENT_SECRET]
    assert run(h) == cli.EXIT_OK
    assert h.prompt_labels == [
        "OAuth client ID (input hidden): ",
        "OAuth client secret (input hidden): ",
    ]
    assert h.store.values(CLIENT_ID_SECRET) == [CLIENT_ID]  # stripped
    assert h.store.values(CLIENT_SECRET_SECRET) == [CLIENT_SECRET]
    assert token_form(h)["client_id"] == CLIENT_ID
    out = h.out.getvalue()
    assert f"stored as version 1 of secret {CLIENT_SECRET_SECRET}" in out


def test_a_disabled_latest_version_counts_as_absent():
    h = Harness()
    h.store.secrets[CLIENT_SECRET_SECRET][-1][1] = "DISABLED"
    h.prompts = [CLIENT_SECRET]
    assert run(h) == cli.EXIT_OK
    assert h.store.values(CLIENT_SECRET_SECRET) == [CLIENT_SECRET, CLIENT_SECRET]


def test_reset_client_prompts_even_when_stored():
    h = Harness()
    new_id, new_value = fake("new", "client", "id"), fake("new", "client", "value")
    h.prompts = [new_id, new_value]
    assert run(h, reset_client=True) == cli.EXIT_OK
    assert h.store.values(CLIENT_ID_SECRET) == [CLIENT_ID, new_id]
    assert token_form(h)["client_secret"] == new_value


def test_an_invalid_entry_is_asked_again_and_three_strikes_stop():
    h = Harness(store=FakeSecretStore.empty())
    h.prompts = ["", "has space", "\t"]
    assert run(h) == cli.EXIT_NOT_CONNECTED
    assert h.store.values(CLIENT_ID_SECRET) == []
    assert h.google.requests == []


def test_missing_secret_container_says_apply_terraform():
    h = Harness(store=FakeSecretStore())  # no secrets exist at all
    h.prompts = [CLIENT_ID]
    assert run(h) == cli.EXIT_NOT_CONNECTED
    assert "does not exist" in h.err.getvalue()


# ---- the redirect ---------------------------------------------------------------------------


def test_state_mismatch_stores_nothing():
    h = Harness(browser=FakeBrowser(answer=lambda state: f"state=forged&code={AUTH_CODE}"))
    assert run(h) == cli.EXIT_NOT_CONNECTED
    assert "'state' did not match" in h.err.getvalue()
    assert h.google.requests == []
    assert h.store.values(REFRESH) == []


def test_access_denied_stores_nothing():
    h = Harness(browser=FakeBrowser(answer=lambda state: f"state={state}&error=access_denied"))
    assert run(h) == cli.EXIT_NOT_CONNECTED
    assert "error=access_denied" in h.err.getvalue()
    assert h.store.values(REFRESH) == []


def test_browser_that_cannot_open_prints_the_local_link_not_googles():
    h = Harness(browser=FakeBrowser(opens=False))
    assert run(h) == cli.EXIT_OK
    out = h.out.getvalue()
    assert "Open this link in a browser on THIS computer" in out
    assert h.browser.opened[0] in out
    assert "accounts.google.com" not in out
    assert CLIENT_ID not in out


def test_no_browser_flag_does_not_try_to_open_one():
    h = Harness()
    assert run(h, open_browser=False, timeout_seconds=1) == cli.EXIT_NOT_CONNECTED
    assert h.browser.opened == []
    assert "No redirect arrived within 1 seconds" in h.err.getvalue()


def test_paste_mode():
    h = Harness()
    h.prompts = [lambda harness: harness.paste_redirect()]
    assert run(h, paste=True) == cli.EXIT_OK
    assert h.browser.opened == []
    assert token_form(h)["redirect_uri"] == "http://127.0.0.1:50123"  # the random port
    assert h.prompt_labels[-1].startswith("Redirect address (input hidden")
    assert h.store.values(REFRESH) == [REFRESH_TOKEN]


def test_paste_mode_with_a_fixed_port():
    h = Harness()
    h.prompts = [lambda harness: harness.paste_redirect()]
    assert run(h, paste=True, port=8765) == cli.EXIT_OK
    assert token_form(h)["redirect_uri"] == "http://127.0.0.1:8765"


def test_paste_mode_refuses_a_wrong_state_and_does_not_echo_it():
    h = Harness()
    pasted = "http://127.0.0.1:50123/?state=forged&code=" + AUTH_CODE
    h.prompts = [pasted]
    assert run(h, paste=True) == cli.EXIT_NOT_CONNECTED
    assert pasted not in h.output()
    assert h.store.values(REFRESH) == []


def test_falls_back_to_paste_when_it_cannot_listen():
    h = Harness()
    h.prompts = [lambda harness: harness.paste_redirect()]
    deps = h.deps()

    def no_listener(**kwargs):
        raise OSError("address in use")

    deps.listener = no_listener
    assert cli.main_with_config(config(), deps) == cli.EXIT_OK
    assert "switching to paste mode" in h.out.getvalue()
    assert h.store.values(REFRESH) == [REFRESH_TOKEN]


# ---- the token response ---------------------------------------------------------------------


def _with_token_response(response) -> Harness:
    h = Harness()
    h.google = type(h.google)()
    h.google.add("POST", "/token", response)
    return h


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (token_response(refresh_token=None), "Google returned no refresh token"),
        (json_response(400, {"error": "invalid_grant"}), "invalid_grant"),
        (json_response(401, {"error": "invalid_client"}), "--reset-client"),
        (token_response(scope="https://www.googleapis.com/auth/other"), "without the requested"),
    ],
)
def test_token_failures_store_no_refresh_token(response, expected):
    h = _with_token_response(response)
    assert run(h) == cli.EXIT_NOT_CONNECTED
    assert expected in h.err.getvalue()
    assert h.store.values(REFRESH) == []
    assert h.fhir.requests == []


def test_a_token_response_without_scope_is_accepted():
    h = Harness()
    h.google = type(h.google)()
    h.google.add("POST", "/token", token_response(scope=None))
    h.google.add("GET", "/v4/users/me/identity", json_response(200, {"healthUserId": "u1"}))
    assert run(h) == cli.EXIT_OK


# ---- after the token is stored --------------------------------------------------------------


def test_fhir_failure_after_the_token_is_stored_exits_3_and_says_so():
    h = Harness()
    h.fhir = type(h.fhir)()
    h.fhir.add("POST", FHIR_PATH, json_response(403, {"error": {"message": HEALTH_USER_ID}}))
    assert run(h) == cli.EXIT_PATIENT_FAILED
    assert h.store.values(REFRESH) == [REFRESH_TOKEN]  # not rolled back
    err = h.err.getvalue()
    assert "The refresh token above IS stored" in err
    assert "403" in err
    assert "Connected. Stored the refresh token as version 1" in h.out.getvalue()


def test_identity_failure_after_the_token_is_stored_exits_3():
    h = Harness()
    h.google = type(h.google)()
    h.google.add("POST", "/token", token_response())
    h.google.add("GET", "/v4/users/me/identity", json_response(403, {}))
    assert run(h) == cli.EXIT_PATIENT_FAILED
    assert h.store.values(REFRESH) == [REFRESH_TOKEN]
    assert h.fhir.requests == []


def test_fhir_answer_that_does_not_confirm_the_write_exits_3():
    h = Harness()
    h.fhir = type(h.fhir)()
    h.fhir.add("POST", FHIR_PATH, json_response(200, {"resourceType": "Bundle"}))
    assert run(h) == cli.EXIT_PATIENT_FAILED


def test_skip_patient_makes_no_identity_or_fhir_call():
    h = Harness()
    assert run(h, fhir=None, demographics=None) == cli.EXIT_OK
    assert h.google.calls("GET") == []
    assert h.fhir.requests == []
    assert "Skipped (--skip-patient)" in h.out.getvalue()


def test_rerun_adds_a_new_version_and_writes_the_same_patient():
    first = Harness()
    assert run(first) == cli.EXIT_OK
    again = Harness(store=first.store)
    again.fhir = type(again.fhir)()
    again.fhir.add("POST", FHIR_PATH, fhir_response("200 OK"))
    assert run(again) == cli.EXIT_OK
    assert first.store.values(REFRESH) == [REFRESH_TOKEN, REFRESH_TOKEN]
    assert f"version 2 of secret {REFRESH}" in again.out.getvalue()
    assert "Patient record updated" in again.out.getvalue()
    # The same conditional update, byte for byte: it lands on the same Patient.
    assert first.fhir.requests[0].body == again.fhir.requests[0].body


def test_disable_previous_disables_only_older_refresh_token_versions():
    store = FakeSecretStore.with_client()
    store.secrets[REFRESH] = [["synthetic-old-one", "ENABLED"], ["synthetic-old-two", "ENABLED"]]
    h = Harness(store=store)
    assert run(h, disable_previous=True) == cli.EXIT_OK
    assert [state for _v, state in store.secrets[REFRESH]] == ["DISABLED", "DISABLED", "ENABLED"]
    assert "Disabled 2 older version(s)" in h.out.getvalue()
    # Without the flag, nothing is disabled.
    h2 = Harness(store=store)
    assert run(h2) == cli.EXIT_OK
    assert [state for _v, state in store.secrets[REFRESH]][-2:] == ["ENABLED", "ENABLED"]


def test_disable_previous_failure_is_only_a_warning():
    store = FakeSecretStore.with_client()
    store.fail_disable = True
    h = Harness(store=store)
    assert run(h, disable_previous=True) == cli.EXIT_OK
    assert "Warning: Permission denied" in h.out.getvalue()


def test_refresh_token_store_failure_is_not_connected():
    store = FakeSecretStore.with_client()
    store.fail_add.add(REFRESH)
    h = Harness(store=store)
    assert run(h) == cli.EXIT_NOT_CONNECTED
    assert h.fhir.requests == []


def test_interrupt_at_a_prompt():
    h = Harness(store=FakeSecretStore.empty())
    h.prompts = [KeyboardInterrupt()]
    assert run(h) == cli.EXIT_INTERRUPTED
