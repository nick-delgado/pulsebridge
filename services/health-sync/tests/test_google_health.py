"""The Google Health client and the OAuth refresh, over a scripted transport (no network)."""

import logging
from urllib.parse import parse_qs

import pytest
from health_sync.config import DEFAULT_SCOPE
from health_sync.credentials import OAuthCredentials
from health_sync.google_health import (
    LIST_URL,
    ROLLUP_URL,
    TOKEN_URL,
    GoogleHealthClient,
    GoogleHealthRejected,
    GoogleHealthUnavailable,
    HttpSender,
    ReconnectNeeded,
    TokenSource,
)
from health_sync.retry import Deadline, DeadlineReached, RetryPolicy
from health_sync.testing import (
    ACCESS_TOKEN,
    ACCESS_TOKEN_2,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    fake,
    formatted_logs,
    google_error,
    leaked,
    list_response,
    oauth_error,
    rollup_page,
    token_response,
    utc,
    window,
)
from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.testing import json_response
from pulsebridge_gcp.transport import HttpResponse

START, END = utc("2026-01-15T12:05:00Z"), utc("2026-01-15T15:05:00Z")


def make(world, *, max_attempts=4, deadline=150.0, scope=DEFAULT_SCOPE):
    http = HttpSender(
        world.google,
        RetryPolicy(max_attempts=max_attempts, sleep=world.clock.sleep),
        Deadline(deadline, clock=world.clock.monotonic),
        timeout=30.0,
    )
    creds = OAuthCredentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)
    tokens = TokenSource(creds, scope=scope, http=http, clock=world.clock.monotonic)
    return tokens, GoogleHealthClient(tokens, http)


# ---- the token request -----------------------------------------------------------------------


def test_refresh_posts_the_documented_form_to_googles_token_endpoint(world):
    world.google.add("POST", "/token", token_response())
    tokens, _ = make(world)
    assert tokens.token() == ACCESS_TOKEN
    (call,) = world.google.calls("POST", "/token")
    assert call.url == TOKEN_URL == "https://oauth2.googleapis.com/token"
    assert call.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs(call.body.decode("ascii"), strict_parsing=True) == {
        "client_id": [CLIENT_ID],
        "client_secret": [CLIENT_SECRET],
        "refresh_token": [REFRESH_TOKEN],
        "grant_type": ["refresh_token"],
    }
    assert "Authorization" not in call.headers


def test_one_access_token_is_reused_until_it_is_about_to_expire(world):
    world.google.add(
        "POST",
        "/token",
        token_response(ACCESS_TOKEN, expires_in=3599),
        token_response(ACCESS_TOKEN_2),
    )
    world.google.add("POST", ":rollUp", rollup_page([]))
    tokens, client = make(world, deadline=None)  # the test jumps an hour ahead
    client.rollup(START, END)
    client.rollup(START, END)
    assert world.token_calls() == 1
    world.clock.mono += 3599 - 60  # the margin: replaced 60 s before Google's expiry
    client.rollup(START, END)
    assert world.token_calls() == 2
    auth = [c.headers["Authorization"] for c in world.google.calls("POST", ":rollUp")]
    assert auth == [f"Bearer {ACCESS_TOKEN}"] * 2 + [f"Bearer {ACCESS_TOKEN_2}"]


def test_a_401_refreshes_once_and_then_succeeds(world):
    world.google.add("POST", "/token", token_response(ACCESS_TOKEN), token_response(ACCESS_TOKEN_2))
    world.google.add("POST", ":rollUp", json_response(401, {}), rollup_page([]))
    tokens, client = make(world)
    assert client.rollup(START, END).items == []
    assert world.token_calls() == 2
    auth = [c.headers["Authorization"] for c in world.google.calls("POST", ":rollUp")]
    assert auth == [f"Bearer {ACCESS_TOKEN}", f"Bearer {ACCESS_TOKEN_2}"]


def test_a_second_401_fails_instead_of_looping(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", json_response(401, {}))
    _, client = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        client.rollup(START, END)
    assert (caught.value.status, caught.value.code) == (401, "unauthorized-after-refresh")
    assert len(world.google.calls("POST", ":rollUp")) == 2
    assert world.token_calls() == 2


# ---- reconnect needed ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        ("invalid_grant", 400, "refresh-token-invalid"),
        ("invalid_client", 401, "oauth-client-rejected"),
        ("unauthorized_client", 400, "oauth-client-rejected"),
        ("deleted_client", 401, "oauth-client-rejected"),
    ],
)
def test_an_unusable_grant_is_reconnect_needed_without_any_retry(world, code, status, reason):
    world.google.add("POST", "/token", oauth_error(code, status))
    tokens, _ = make(world)
    with pytest.raises(ReconnectNeeded) as caught:
        tokens.token()
    assert caught.value.code == reason
    assert world.token_calls() == 1
    assert world.clock.sleeps == []
    assert "synthetic description" not in str(caught.value)  # free text from Google is dropped


def test_other_token_endpoint_refusals_are_rejections_not_reconnects(world):
    world.google.add("POST", "/token", oauth_error("invalid_request"))
    tokens, _ = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        tokens.token()
    assert not isinstance(caught.value, ReconnectNeeded)
    assert caught.value.code == "invalid_request"


def test_a_scope_list_without_ours_is_a_warning_not_a_failure(world, caplog):
    """An exact-string mismatch in the token response must not stop the run; if the scope is
    really missing, the API answers 403 MISSING_OAUTH_SCOPE (next test)."""
    other = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
    world.google.add("POST", "/token", token_response(scope=other))
    tokens, _ = make(world)
    with caplog.at_level(logging.WARNING):
        assert tokens.token() == ACCESS_TOKEN
    assert tokens.scope_not_listed is True
    assert [r.fields["event"] for r in caplog.records] == ["scope-not-listed"]
    assert other not in formatted_logs(caplog.records)


def test_missing_oauth_scope_from_the_api_is_reconnect_needed(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", google_error(403, "MISSING_OAUTH_SCOPE"))
    _, client = make(world)
    with pytest.raises(ReconnectNeeded) as caught:
        client.rollup(START, END)
    assert caught.value.code == "scope-not-granted"


def test_another_403_is_a_rejection_named_by_googles_reason(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", google_error(403, "DATA_ACCESS_DENIED"))
    _, client = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        client.rollup(START, END)
    assert not isinstance(caught.value, ReconnectNeeded)
    assert (caught.value.status, caught.value.code) == (403, "DATA_ACCESS_DENIED")


@pytest.mark.parametrize(
    "body",
    [
        {"expires_in": 3599, "token_type": "Bearer"},  # no access_token
        {"access_token": "x", "expires_in": "3599", "token_type": "Bearer"},  # string expiry
        {"access_token": "x", "expires_in": 0, "token_type": "Bearer"},
        {"access_token": "x", "expires_in": 3599, "token_type": "MAC"},
    ],
)
def test_a_malformed_token_response_is_rejected(world, body):
    world.google.add("POST", "/token", json_response(200, body))
    tokens, _ = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        tokens.token()
    assert caught.value.code == "malformed-response"


def test_a_rotated_refresh_token_is_flagged_never_stored(world):
    rotated = token_response()
    body = rotated.json() | {"refresh_token": "synthetic-rotated"}
    world.google.add("POST", "/token", json_response(200, body))
    tokens, _ = make(world)
    tokens.token()
    assert tokens.rotated_refresh_token_ignored is True


# ---- retries ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 408])
def test_retryable_statuses_back_off_and_then_succeed(world, status):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", google_error(status), google_error(status), rollup_page([]))
    _, client = make(world)
    assert client.rollup(START, END).pages == 1
    assert world.clock.sleeps == [1.0, 2.0]


def test_retries_are_bounded_by_max_attempts_then_unavailable(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", google_error(503))
    _, client = make(world, max_attempts=4)
    with pytest.raises(GoogleHealthUnavailable) as caught:
        client.rollup(START, END)
    assert (caught.value.status, caught.value.code) == (503, "retries-exhausted")
    assert len(world.google.calls("POST", ":rollUp")) == 4
    assert world.clock.sleeps == [1.0, 2.0, 4.0]


def test_retries_stop_when_the_next_wait_would_pass_the_deadline(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", google_error(429))
    _, client = make(world, max_attempts=10, deadline=5.0)
    with pytest.raises(GoogleHealthUnavailable):
        client.rollup(START, END)
    # Waits of 1 and 2 s fit in 5 s; the next (4 s) would end after the deadline.
    assert world.clock.sleeps == [1.0, 2.0]
    assert len(world.google.calls("POST", ":rollUp")) == 3


def test_retry_after_is_honoured_up_to_the_cap(world):
    world.google.add("POST", "/token", token_response())
    slow = HttpResponse(status=429, body=b"{}", headers={"retry-after": "7"})
    world.google.add("POST", ":rollUp", slow, rollup_page([]))
    _, client = make(world)
    client.rollup(START, END)
    assert world.clock.sleeps == [7.0]


def test_no_response_at_all_is_retried(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", RetryableError("POST x", None), rollup_page([]))
    _, client = make(world)
    client.rollup(START, END)
    assert world.clock.sleeps == [1.0]


def test_the_token_endpoint_is_retried_on_5xx(world):
    world.google.add("POST", "/token", oauth_error("server_error", 503), token_response())
    tokens, _ = make(world)
    assert tokens.token() == ACCESS_TOKEN
    assert world.token_calls() == 2


def test_other_4xx_from_the_api_fail_at_once(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", google_error(400, "INVALID_TIME_RANGE"))
    _, client = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        client.rollup(START, END)
    assert caught.value.code == "INVALID_TIME_RANGE"
    assert world.clock.sleeps == []


# ---- roll-up request and paging --------------------------------------------------------------


def test_rollup_request_body_and_paging_follow_the_discovery_document(world):
    world.google.add("POST", "/token", token_response())
    page1 = [window("2026-01-15T15:00:00Z", 60, 61.5, 63)]
    page2 = [window("2026-01-15T14:55:00Z", 58, 59, 60)]
    world.google.add("POST", ":rollUp", rollup_page(page1, "pg-2"), rollup_page(page2, ""))
    _, client = make(world)
    result = client.rollup(START, END)
    assert result.items == page1 + page2
    assert result.pages == 2
    calls = world.google.calls("POST", ":rollUp")
    assert all(c.url == ROLLUP_URL for c in calls)
    assert ROLLUP_URL == (
        "https://health.googleapis.com/v4/users/me/dataTypes/heart-rate/dataPoints:rollUp"
    )
    first = {
        "range": {"startTime": "2026-01-15T12:05:00Z", "endTime": "2026-01-15T15:05:00Z"},
        "windowSize": "300s",
        "pageSize": 4032,
    }
    assert [c.json() for c in calls] == [first, first | {"pageToken": "pg-2"}]
    assert calls[0].headers["Content-Type"] == "application/json"


def test_a_repeated_page_token_stops_instead_of_looping(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", rollup_page([], "same"))
    _, client = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        client.rollup(START, END)
    assert caught.value.code == "bad-page-token"
    assert len(world.google.calls("POST", ":rollUp")) == 2


@pytest.mark.parametrize(
    "response",
    [
        json_response(200, {"rollupDataPoints": {"not": "a list"}}),
        json_response(200, ["not", "an", "object"]),
        HttpResponse(status=200, body=b"not json"),
    ],
)
def test_a_malformed_rollup_response_is_rejected(world, response):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", response)
    _, client = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        client.rollup(START, END)
    assert caught.value.code == "malformed-response"


# ---- the data source call --------------------------------------------------------------------


def test_latest_data_source_asks_for_one_raw_point_in_the_range(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("GET", "/dataPoints", list_response())
    _, client = make(world)
    source = client.latest_data_source(START, END)
    assert (source.device_name, source.platform, source.recording_method) == (
        "Synthetic Watch 1",
        "FITBIT",
        "PASSIVELY_MEASURED",
    )
    (call,) = world.google.calls("GET", "/dataPoints")
    assert call.url == LIST_URL
    assert call.params == {
        "filter": 'heart_rate.sample_time.physical_time >= "2026-01-15T12:05:00Z" AND '
        'heart_rate.sample_time.physical_time < "2026-01-15T15:05:00Z"',
        "pageSize": "1",
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (list_response(None, None, None), (None, None, None)),
        (list_response("  ", "PLATFORM_UNSPECIFIED", "UNKNOWN"), (None, None, None)),
        (list_response("Bad\nname", "fitbit", "RECORDING_METHOD_UNSPECIFIED"), (None, None, None)),
        (json_response(200, {}), (None, None, None)),
        (json_response(200, {"dataPoints": []}), (None, None, None)),
    ],
)
def test_nothing_is_asserted_that_google_did_not_report(world, response, expected):
    world.google.add("POST", "/token", token_response())
    world.google.add("GET", "/dataPoints", response)
    _, client = make(world)
    source = client.latest_data_source(START, END)
    assert (source.device_name, source.platform, source.recording_method) == expected


def test_error_messages_never_carry_credentials(world):
    world.google.add("POST", "/token", oauth_error("invalid_grant"))
    tokens, _ = make(world)
    with pytest.raises(ReconnectNeeded) as caught:
        tokens.token()
    assert leaked(str(caught.value) + repr(caught.value)) == []
    assert leaked(repr(OAuthCredentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN))) == []


def test_an_unknown_oauth_error_code_is_shown_as_refused(world):
    leaky = fake("looks", "like", "a", "token", "x9Qz")
    world.google.add("POST", "/token", oauth_error(leaky))
    tokens, _ = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        tokens.token()
    assert caught.value.code == "refused"
    assert leaky not in str(caught.value)


def test_a_known_oauth_error_code_is_named(world):
    world.google.add("POST", "/token", oauth_error("invalid_scope"))
    tokens, _ = make(world)
    with pytest.raises(GoogleHealthRejected) as caught:
        tokens.token()
    assert caught.value.code == "invalid_scope"


def test_a_rotated_refresh_token_is_logged_as_a_warning_without_its_value(world, caplog):
    value = fake("rotated", "grant")
    world.google.add(
        "POST", "/token", json_response(200, token_response().json() | {"refresh_token": value})
    )
    tokens, _ = make(world)
    with caplog.at_level(logging.WARNING):
        tokens.token()
    logs = formatted_logs(caplog.records)
    assert "refresh-token-rotated" in logs
    assert value not in logs


def test_no_request_is_started_after_the_deadline(world):
    world.google.add("POST", "/token", token_response())
    tokens, _ = make(world, deadline=10.0)
    world.clock.mono += 9
    with pytest.raises(DeadlineReached) as caught:
        tokens.token()
    assert caught.value.where == "token-request"
    assert world.google.requests == []


def test_an_api_request_after_the_deadline_is_named_google_request(world):
    world.google.add("POST", "/token", token_response())
    tokens, client = make(world, deadline=10.0)
    tokens.token()
    world.clock.mono += 9
    with pytest.raises(DeadlineReached) as caught:
        client.rollup(START, END)
    assert caught.value.where == "google-request"


def test_no_backoff_wait_is_taken_that_leaves_no_room_for_the_next_request(world):
    """4.5 s budget: after the first 429 a 1 s wait leaves 3.5 s; after the second, a 2 s wait
    would leave 1.5 s, less than a request needs (2 s), so the run stops instead of waiting."""
    world.google.add("POST", "/token", google_error(429))
    tokens, _ = make(world, max_attempts=10, deadline=4.5)
    with pytest.raises(GoogleHealthUnavailable):
        tokens.token()
    assert world.clock.sleeps == [1.0]
    assert world.token_calls() == 2
