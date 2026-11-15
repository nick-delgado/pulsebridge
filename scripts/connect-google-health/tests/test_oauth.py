"""Authorization URL, redirect parsing (listener and paste) and the code exchange."""

from urllib.parse import parse_qs, urlsplit

import pytest
from connect_google_health import oauth
from connect_google_health.testing import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    SECRET_VALUES,
    leaked,
    token_response,
)
from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.testing import FakeTransport, json_response
from pulsebridge_gcp.transport import HttpResponse

STATE = "synthetic-state-value-abcdefghijklmnopqrstuvwxyz"
REDIRECT = "http://127.0.0.1:50123"
CLIENT = oauth.ClientCredentials(CLIENT_ID, CLIENT_SECRET)
CODE = oauth.AuthorizationCode(AUTH_CODE)
VERIFIER = "v" * 43


# ---- authorization URL ----------------------------------------------------------------------


def test_authorization_url_has_exactly_the_documented_parameters():
    url = oauth.authorization_url(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT,
        scope=oauth.DEFAULT_SCOPE,
        state=STATE,
        code_challenge="synthetic-challenge",
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert parse_qs(parts.query, strict_parsing=True) == {
        "client_id": [CLIENT_ID],
        "redirect_uri": ["http://127.0.0.1:50123"],
        "response_type": ["code"],
        "scope": [
            "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
        ],
        "code_challenge": ["synthetic-challenge"],
        "code_challenge_method": ["S256"],
        "state": [STATE],
        "access_type": ["offline"],
        "prompt": ["consent"],
    }


def test_default_scope_is_the_one_health_sync_expects():
    from health_sync.config import DEFAULT_SCOPE as HEALTH_SYNC_SCOPE

    assert oauth.DEFAULT_SCOPE == HEALTH_SYNC_SCOPE


def test_loopback_redirect_uri_uses_the_ip_literal():
    assert oauth.loopback_redirect_uri(8085) == "http://127.0.0.1:8085"
    with pytest.raises(ValueError):
        oauth.loopback_redirect_uri(0)


# ---- the redirect ---------------------------------------------------------------------------


def test_redirect_with_matching_state_returns_the_code():
    code = oauth.parse_redirect_query(
        f"state={STATE}&code={AUTH_CODE}&scope=x", expected_state=STATE
    )
    assert code.value == AUTH_CODE
    assert AUTH_CODE not in repr(code)


@pytest.mark.parametrize(
    "query",
    [
        f"state=wrong&code={AUTH_CODE}",
        f"code={AUTH_CODE}",
        f"state={STATE}x&code={AUTH_CODE}",
        "state=wrong&error=access_denied",  # an error from someone else's request is not acted on
    ],
)
def test_state_mismatch_is_refused(query):
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_redirect_query(query, expected_state=STATE)
    assert caught.value.code == "state-mismatch"
    assert AUTH_CODE not in str(caught.value)


def test_access_denied_is_explained():
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_redirect_query(f"state={STATE}&error=access_denied", expected_state=STATE)
    assert caught.value.code == "access_denied"
    assert "test user" in str(caught.value)


def test_unknown_error_value_is_not_echoed():
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_redirect_query(f"state={STATE}&error={CLIENT_SECRET}", expected_state=STATE)
    assert caught.value.code == "refused"
    assert CLIENT_SECRET not in str(caught.value)


@pytest.mark.parametrize("query", [f"state={STATE}", f"state={STATE}&code="])
def test_missing_code(query):
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_redirect_query(query, expected_state=STATE)
    assert caught.value.code == "missing-code"


def test_repeated_code_is_refused():
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_redirect_query(
            f"state={STATE}&code={AUTH_CODE}&code=other", expected_state=STATE
        )
    assert caught.value.code == "repeated-parameter"


def test_code_with_a_space_is_refused():
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_redirect_query(f"state={STATE}&code=a+b", expected_state=STATE)
    assert caught.value.code == "malformed-code"


# ---- pasted redirect ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pasted",
    [
        f"{REDIRECT}/?state={STATE}&code={AUTH_CODE}",
        f"{REDIRECT}?state={STATE}&code={AUTH_CODE}",
        f"  {REDIRECT}/?state={STATE}&code={AUTH_CODE}&scope=s\n",
        f"{REDIRECT}/#state={STATE}&code={AUTH_CODE}",
    ],
)
def test_pasted_redirect_is_accepted(pasted):
    code = oauth.parse_pasted_redirect(pasted, redirect_uri=REDIRECT, expected_state=STATE)
    assert code.value == AUTH_CODE


@pytest.mark.parametrize(
    ("pasted", "code"),
    [
        (f"http://127.0.0.1:50124/?state={STATE}&code={AUTH_CODE}", "paste-wrong-address"),
        (f"http://localhost:50123/?state={STATE}&code={AUTH_CODE}", "paste-wrong-address"),
        (f"https://127.0.0.1:50123/?state={STATE}&code={AUTH_CODE}", "paste-wrong-address"),
        (f"{REDIRECT}/other?state={STATE}&code={AUTH_CODE}", "paste-wrong-address"),
        (f"http://127.0.0.1:99999/?code={AUTH_CODE}", "paste-not-a-url"),
        (AUTH_CODE, "paste-wrong-address"),
        (f"{REDIRECT}/", "missing-code"),
        (f"{REDIRECT}/?state=wrong&code={AUTH_CODE}", "state-mismatch"),
    ],
)
def test_pasted_redirect_is_checked_and_never_echoed(pasted, code):
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_pasted_redirect(pasted, redirect_uri=REDIRECT, expected_state=STATE)
    assert caught.value.code == code
    assert AUTH_CODE not in str(caught.value)


# ---- the code exchange ----------------------------------------------------------------------


def test_token_request_body_is_exactly_the_documented_form():
    body = oauth.token_request_body(CLIENT, CODE, code_verifier=VERIFIER, redirect_uri=REDIRECT)
    assert (
        body
        == (
            f"client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}&code={AUTH_CODE}"
            f"&code_verifier={VERIFIER}&grant_type=authorization_code"
            "&redirect_uri=http%3A%2F%2F127.0.0.1%3A50123"
        ).encode()
    )


def _exchange(*responses):
    transport = FakeTransport()
    transport.add("POST", "/token", *responses)
    tokens = oauth.exchange_code(
        transport, CLIENT, CODE, code_verifier=VERIFIER, redirect_uri=REDIRECT
    )
    return tokens, transport


def test_exchange_posts_the_form_to_google_and_reads_the_tokens():
    tokens, transport = _exchange(token_response())
    (request,) = transport.requests
    assert request.method == "POST"
    assert request.url == "https://oauth2.googleapis.com/token"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs(request.body.decode()) == {
        "client_id": [CLIENT_ID],
        "client_secret": [CLIENT_SECRET],
        "code": [AUTH_CODE],
        "code_verifier": [VERIFIER],
        "grant_type": ["authorization_code"],
        "redirect_uri": [REDIRECT],
    }
    assert tokens.access_token == ACCESS_TOKEN
    assert tokens.refresh_token == REFRESH_TOKEN
    assert tokens.expires_in == 3599
    assert tokens.refresh_token_expires_in == 604799
    assert tokens.scopes == (oauth.DEFAULT_SCOPE,)
    assert leaked(repr(tokens), SECRET_VALUES) == []


def _failure(*responses) -> oauth.OAuthError:
    with pytest.raises(oauth.OAuthError) as caught:
        _exchange(*responses)
    assert leaked(str(caught.value), SECRET_VALUES) == []
    # No chained exception that could carry a received body into a traceback.
    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    return caught.value


def test_invalid_grant_is_explained():
    error = _failure(json_response(400, {"error": "invalid_grant", "error_description": AUTH_CODE}))
    assert error.code == "invalid_grant"
    assert "Run the script again" in str(error)


def test_invalid_client_points_at_reset_client():
    error = _failure(json_response(401, {"error": "invalid_client"}))
    assert error.code == "invalid_client"
    assert "--reset-client" in str(error)


def test_unknown_error_code_is_shown_as_refused():
    error = _failure(json_response(400, {"error": REFRESH_TOKEN}))
    assert error.code == "refused"


def test_network_error():
    error = _failure(RetryableError("POST token", None, server_message=CLIENT_SECRET))
    assert error.code == "network-error"


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(200, b"<html>" + ACCESS_TOKEN.encode() + b"</html>"),
        HttpResponse(200, b"\xff\xfe"),
        json_response(200, [ACCESS_TOKEN]),
        HttpResponse(400, b"not json " + CLIENT_SECRET.encode()),
    ],
)
def test_non_json_answers(response):
    error = _failure(response)
    assert error.code in ("malformed-response", "refused")


def test_server_error_status():
    assert _failure(json_response(503, {"error": "x"})).code == "unexpected-status"


@pytest.mark.parametrize(
    "overrides",
    [
        {"access_token": None},
        {"token_type": "MAC"},
        {"expires_in": 0},
        {"expires_in": True},
        {"refresh_token": "has space"},
        {"refresh_token": 12345},
    ],
)
def test_malformed_token_response(overrides):
    assert _failure(token_response(**overrides)).code == "malformed-response"


@pytest.mark.parametrize("overrides", [{"refresh_token": None}, {"refresh_token": ""}])
def test_no_refresh_token_is_explained(overrides):
    error = _failure(token_response(**overrides))
    assert error.code == "no-refresh-token"
    assert "myaccount.google.com" in str(error)


def test_absent_optional_fields_are_none():
    tokens, _ = _exchange(token_response(scope=None, refresh_token_expires_in=None))
    assert tokens.scopes is None
    assert tokens.refresh_token_expires_in is None


# ---- review follow-ups ----------------------------------------------------------------------

TOO_MANY = "&".join(f"f{i}=x" for i in range(30))


def test_too_many_fields_is_a_malformed_redirect_on_both_paths():
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_redirect_query(f"state={STATE}&{TOO_MANY}", expected_state=STATE)
    assert caught.value.code == "malformed-redirect"
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.parse_pasted_redirect(
            f"{REDIRECT}/?state={STATE}&{TOO_MANY}", redirect_uri=REDIRECT, expected_state=STATE
        )
    assert caught.value.code == "malformed-redirect"


@pytest.mark.parametrize(
    ("query", "matches"),
    [
        (f"state={STATE}&code=x", True),
        (f"state={STATE}&error=access_denied", True),
        ("state=other&code=x", False),
        ("code=x", False),
        (f"state={STATE}&state={STATE}", False),
        (f"state={STATE}&{TOO_MANY}", False),
    ],
)
def test_redirect_state_matches(query, matches):
    assert oauth.redirect_state_matches(query, expected_state=STATE) is matches


@pytest.mark.parametrize("quote", ['"', "'"])
def test_one_pair_of_quotes_around_a_pasted_address_is_removed(quote):
    pasted = f"{quote}{REDIRECT}/?state={STATE}&code={AUTH_CODE}{quote}"
    code = oauth.parse_pasted_redirect(pasted, redirect_uri=REDIRECT, expected_state=STATE)
    assert code.value == AUTH_CODE


def test_mismatched_quotes_are_not_removed():
    with pytest.raises(oauth.OAuthError):
        oauth.parse_pasted_redirect(
            f"\"{REDIRECT}/?state={STATE}&code={AUTH_CODE}'", redirect_uri=REDIRECT,
            expected_state=STATE,
        )  # fmt: skip


@pytest.mark.parametrize(
    ("granted", "missing"),
    [
        ((oauth.DEFAULT_SCOPE,), []),
        (("googlehealth.health_metrics_and_measurements.readonly",), []),  # short form
        (("openid", oauth.DEFAULT_SCOPE), []),
        (("https://www.googleapis.com/auth/other",), [oauth.DEFAULT_SCOPE]),
        ((), [oauth.DEFAULT_SCOPE]),
    ],
)
def test_missing_scopes_compares_without_the_google_prefix(granted, missing):
    assert oauth.missing_scopes(oauth.DEFAULT_SCOPE, granted) == missing
    short = "googlehealth.health_metrics_and_measurements.readonly"
    assert oauth.missing_scopes(short, granted) == ([short] if missing else [])
