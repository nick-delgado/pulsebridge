"""Anonymous demo sessions (D16): GET /auth/config, POST /auth/session, POST /auth/logout and
the __session cookie gate, over fake Fraud Defense and a fake counter bucket."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fhir_proxy.app import create_app
from fhir_proxy.testing import (
    ORIGIN,
    OTHER_SIGNING_KEY,
    SITE_KEY,
    FakeBucket,
    FakeFraudDefense,
    FakeHealthcare,
    FakeIdTokens,
    FakeSender,
    build_harness,
    make_config,
    session_from,
)

COOKIE_ATTRIBUTES = "Max-Age=86400; HttpOnly; Secure; SameSite=Strict"
BUSY_TEXT = "The demo is busy, try again tomorrow."


def reason(response) -> str:  # noqa: ANN001
    return response.get_json()["issue"][0]["details"]["coding"][0]["code"]


def today(h) -> str:  # noqa: ANN001
    return "bot-checks/" + h.clock.now.date().isoformat()


# ---- GET /auth/config --------------------------------------------------------------------------


def test_config_gives_the_public_site_key_and_action_without_a_session(harness) -> None:
    response = harness.client.get("/auth/config")
    assert response.status_code == 200
    assert response.get_json() == {"recaptcha_site_key": SITE_KEY, "recaptcha_action": "view_demo"}
    assert response.headers["Cache-Control"] == "no-store"
    assert harness.fraud.assessments == 0


def test_config_follows_the_configured_action(tmp_path) -> None:
    h = build_harness(tmp_path, recaptcha_action="open_dashboard")
    assert h.client.get("/auth/config").get_json()["recaptcha_action"] == "open_dashboard"


# ---- a pass ------------------------------------------------------------------------------------


def test_a_pass_sets_exactly_the_contracts_cookie(harness) -> None:
    response = harness.start_session()
    assert response.status_code == 200
    assert response.get_json() == {"expires_at": "2026-01-03T12:00:00Z"}  # START + 24 h
    cookies = response.headers.getlist("Set-Cookie")
    assert len(cookies) == 1
    token = session_from(response)
    assert cookies[0] == f"__session={token}; Path=/; {COOKIE_ATTRIBUTES}"
    assert response.headers["Cache-Control"] == "no-store"
    # The page reads expires_at only; the token is never in the body.
    assert token not in response.get_data(as_text=True)


def test_the_cookie_path_follows_the_path_prefix(tmp_path) -> None:
    h = build_harness(tmp_path, path_prefix="/api")
    response = h.start_session()
    assert response.status_code == 200
    token = session_from(response)
    assert response.headers["Set-Cookie"] == f"__session={token}; Path=/api; {COOKIE_ATTRIBUTES}"
    assert h.client.get("/api/status", headers=h.auth(token)).status_code == 200


def test_max_age_follows_the_session_lifetime(tmp_path) -> None:
    h = build_harness(tmp_path, session_ttl_seconds=600)
    response = h.start_session()
    assert "; Max-Age=600; " in response.headers["Set-Cookie"]
    assert response.get_json() == {"expires_at": "2026-01-02T12:10:00Z"}


def test_a_pass_makes_one_assessment_with_the_configured_key_and_action(harness) -> None:
    token = harness.fraud.issue()
    assert harness.start_session(token).status_code == 200
    assert harness.fraud.assessments == 1
    sent = harness.fraud.requests[0]
    assert sent["method"] == "POST"
    assert sent["url"] == (
        "https://recaptchaenterprise.googleapis.com/v1/projects/example-project/assessments"
    )
    assert sent["event"] == {"token": token, "siteKey": SITE_KEY, "expectedAction": "view_demo"}
    assert harness.bucket.count(today(harness)) == 1


def test_the_score_at_exactly_the_minimum_passes(tmp_path) -> None:
    h = build_harness(tmp_path, recaptcha_min_score=0.7)
    assert h.start_session(h.fraud.issue(score=0.7)).status_code == 200
    assert h.start_session(h.fraud.issue(score=0.69)).status_code == 403


def test_the_cookie_opens_the_other_routes(harness) -> None:
    token = harness.login()
    assert harness.client.get("/fhir/metadata", headers=harness.auth(token)).status_code == 200


# ---- failures of the bot check -----------------------------------------------------------------


@pytest.mark.parametrize(
    "facts",
    [
        {"valid": False, "invalid_reason": "EXPIRED"},
        {"valid": False, "invalid_reason": "BROWSER_ERROR"},
        {"action": "login"},  # a token made for another action
        {"action": ""},
        {"hostname": "copy-of-the-page.example.test"},  # our public key used on another site
        {"hostname": "dashboard.example.test.evil.test"},
        {"score": 0.49},
        {"score": 0.0},  # left out of the answer, as protobuf JSON does with a default value
    ],
)
def test_each_failure_is_403_bot_check_failed_and_says_nothing_more(harness, facts) -> None:
    passing = harness.start_session()  # for comparison: same body shape on a pass
    assert passing.status_code == 200
    failing = harness.start_session(harness.fraud.issue(**facts))
    assert failing.status_code == 403
    assert reason(failing) == "bot-check-failed"
    # The same bytes whatever failed: no score, no reason, no hint of which check it was.
    reference = harness.start_session(harness.fraud.issue(score=0.1))
    assert failing.get_data() == reference.get_data()
    text = failing.get_data(as_text=True)
    for leak in ("score", "0.49", "action", "hostname", "EXPIRED", "BROWSER", "invalid"):
        assert leak not in text
    assert "Set-Cookie" not in failing.headers


def test_an_unknown_token_fails(harness) -> None:
    response = harness.start_session("03" + "A" * 400)  # the right shape, never issued
    assert response.status_code == 403
    assert reason(response) == "bot-check-failed"
    assert harness.fraud.assessments == 1


def test_a_token_cannot_be_used_twice(harness) -> None:
    token = harness.fraud.issue()
    assert harness.start_session(token).status_code == 200
    again = harness.start_session(token)
    assert again.status_code == 403
    assert reason(again) == "bot-check-failed"


def test_the_hostname_may_be_any_configured_origins_host(tmp_path) -> None:
    h = build_harness(tmp_path, allowed_origins=(ORIGIN, "https://demo.example.test"))
    assert h.start_session(h.fraud.issue(hostname="demo.example.test")).status_code == 200
    assert h.start_session(h.fraud.issue(hostname="other.example.test")).status_code == 403


# ---- the cheap checks come first ----------------------------------------------------------------


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "short",
        "has spaces " * 10,
        "x" * 8193,
        "<script>" + "a" * 40,
        "a.b.c" * 10,  # a dot: never in a reCAPTCHA token
        "é" * 40,
    ],
)
def test_a_token_of_the_wrong_shape_costs_no_assessment_and_no_count(harness, junk) -> None:
    response = harness.start_session(junk)
    assert response.status_code == 403
    assert reason(response) == "bot-check-failed"
    assert harness.fraud.assessments == 0
    assert harness.bucket.requests == []


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"", "application/json"),
        (b"not json", "application/json"),
        (b"[]", "application/json"),
        (b"{}", "application/json"),
        (b'{"recaptcha_token": 5}', "application/json"),
        (b'{"recaptcha_token": null}', "application/json"),
        (b'{"recaptcha_token": "a", "extra": 1}', "application/json"),
        (b'{"token": "a"}', "application/json"),
        (b'{"recaptcha_token": "a", "recaptcha_token": "b"}', "application/json"),
        (b'{"recaptcha_token": NaN}', "application/json"),
        (b'{"recaptcha_token": "a"}', "text/plain"),
        (b'{"recaptcha_token": "a"}', "application/x-www-form-urlencoded"),
        (b"\xff\xfe", "application/json"),
    ],
)
def test_a_malformed_body_is_400(harness, data: bytes, content_type: str) -> None:
    response = harness.client.post(
        "/auth/session", data=data, content_type=content_type, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 400
    assert reason(response) == "invalid-body"
    assert harness.fraud.assessments == 0


def test_a_body_over_the_limit_is_413_without_an_assessment(harness) -> None:
    data = json.dumps({"recaptcha_token": "a" * 20000}).encode()
    response = harness.client.post(
        "/auth/session", data=data, content_type="application/json", headers={"Origin": ORIGIN}
    )
    assert response.status_code == 413
    assert harness.fraud.assessments == 0


# ---- the per-address limit ---------------------------------------------------------------------


def test_the_sixth_attempt_in_an_hour_is_429_before_any_assessment(harness) -> None:
    xff = {"X-Forwarded-For": "203.0.113.9"}
    for _ in range(5):
        assert harness.start_session(**xff).status_code == 200
    assert harness.fraud.assessments == 5
    refused = harness.start_session(**xff)
    assert refused.status_code == 429
    assert reason(refused) == "rate-limited"
    assert refused.headers["Retry-After"] == "3600"
    assert harness.fraud.assessments == 5  # none for the refused attempt
    assert harness.bucket.count(today(harness)) == 5


def test_failed_and_malformed_attempts_count_too(harness) -> None:
    xff = {"X-Forwarded-For": "203.0.113.9"}
    harness.start_session("junk", **xff)
    harness.start_session(harness.fraud.issue(score=0.1), **xff)
    harness.client.post("/auth/session", data=b"x", headers={"Origin": ORIGIN, **xff})
    for _ in range(2):
        assert harness.start_session(**xff).status_code == 200
    assert harness.start_session(**xff).status_code == 429


def test_the_window_slides_and_retry_after_counts_down(harness) -> None:
    xff = {"X-Forwarded-For": "203.0.113.9"}
    for _ in range(5):
        harness.start_session(**xff)
        harness.clock.advance(60)
    # Attempts at 0, 60, ..., 240 s; now 300 s: the oldest ages out at 3600 s.
    assert harness.start_session(**xff).headers["Retry-After"] == "3300"
    harness.clock.advance(3299)
    assert harness.start_session(**xff).status_code == 429
    harness.clock.advance(1)
    assert harness.start_session(**xff).status_code == 200


def test_addresses_are_limited_apart(harness) -> None:
    for _ in range(5):
        harness.start_session(**{"X-Forwarded-For": "203.0.113.9"})
    assert harness.start_session(**{"X-Forwarded-For": "203.0.113.10"}).status_code == 200


def test_the_limit_follows_the_configured_number(tmp_path) -> None:
    h = build_harness(tmp_path, sessions_per_address_per_hour=2)
    assert [h.start_session().status_code for _ in range(3)] == [200, 200, 429]


def test_the_client_address_rules_apply_two_hops_behind_hosting(tmp_path) -> None:
    h = build_harness(tmp_path, trusted_proxy_hops=2)
    hosting = "35.191.0.1"
    for n in range(5):
        # Hosting's own address changes per request; the visitor is second from the right.
        h.start_session(**{"X-Forwarded-For": f"198.51.100.7, {hosting[:-1]}{n}"})
    assert h.start_session(**{"X-Forwarded-For": f"198.51.100.7, {hosting}"}).status_code == 429
    assert h.start_session(**{"X-Forwarded-For": f"198.51.100.8, {hosting}"}).status_code == 200


def test_rate_limit_refusals_are_logged_sampled(tmp_path, logs) -> None:
    h = build_harness(tmp_path, sessions_per_address_per_hour=1)
    for address in ("203.0.113.1", "203.0.113.2"):
        for _ in range(30):
            h.start_session(**{"X-Forwarded-For": address})
    limited = [e for e in logs.events() if e.get("event") == "session-rate-limited"]
    assert len(limited) == 2  # one per address in the window, not 58
    h.clock.advance(61)
    h.start_session(**{"X-Forwarded-For": "203.0.113.1"})
    summaries = [e for e in logs.events() if e.get("event") == "session-events-suppressed"]
    assert summaries and summaries[0]["count"] == 56


# ---- the daily cap ---------------------------------------------------------------------------


def test_the_daily_cap_answers_demo_busy_without_an_assessment(tmp_path) -> None:
    h = build_harness(tmp_path, bot_checks_per_day=3)
    for n in range(3):
        assert h.start_session(**{"X-Forwarded-For": f"203.0.113.{n}"}).status_code == 200
    busy = h.start_session(**{"X-Forwarded-For": "203.0.113.99"})
    assert busy.status_code == 503
    assert reason(busy) == "demo-busy"
    assert busy.get_json()["issue"][0]["diagnostics"] == BUSY_TEXT
    assert busy.headers["Retry-After"] == "43200"  # 12:00 UTC to midnight
    assert h.fraud.assessments == 3
    assert h.bucket.count(today(h)) == 3


def test_the_cap_counts_assessments_not_sessions(tmp_path) -> None:
    h = build_harness(tmp_path, bot_checks_per_day=2)
    assert h.start_session(h.fraud.issue(score=0.1)).status_code == 403  # counted
    h.fraud.failure = 500
    assert h.start_session().status_code == 503  # made and failed: counted
    h.fraud.failure = None
    assert reason(h.start_session()) == "demo-busy"
    assert h.fraud.assessments == 2


def test_a_new_utc_day_starts_a_new_count(tmp_path) -> None:
    h = build_harness(tmp_path, bot_checks_per_day=1)
    h.clock.now = datetime(2026, 1, 2, 23, 59, 59, tzinfo=UTC)
    assert h.start_session(**{"X-Forwarded-For": "203.0.113.1"}).status_code == 200
    busy = h.start_session(**{"X-Forwarded-For": "203.0.113.2"})
    assert reason(busy) == "demo-busy"
    assert busy.headers["Retry-After"] == "1"
    h.clock.now += timedelta(seconds=1)
    assert h.start_session(**{"X-Forwarded-For": "203.0.113.3"}).status_code == 200
    assert h.bucket.count("bot-checks/2026-01-02") == 1
    assert h.bucket.count("bot-checks/2026-01-03") == 1


def _instance(tmp_path, bucket: FakeBucket, fraud: FakeFraudDefense, cap: int):  # noqa: ANN202
    config = make_config(tmp_path, bot_checks_per_day=cap)
    app = create_app(
        config,
        FakeHealthcare(),
        id_tokens=FakeIdTokens(),
        send=FakeSender(),
        recaptcha_transport=fraud,
        storage_transport=bucket,
        clock=lambda: datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    return app.test_client(use_cookies=False)


def _post(client, fraud: FakeFraudDefense, address: str):  # noqa: ANN001, ANN202
    return client.post(
        "/auth/session",
        json={"recaptcha_token": fraud.issue()},
        headers={"Origin": ORIGIN, "X-Forwarded-For": address},
    )


def test_the_cap_spans_instances_and_survives_a_restart(tmp_path) -> None:
    bucket, fraud = FakeBucket(), FakeFraudDefense()
    first = _instance(tmp_path, bucket, fraud, cap=3)
    second = _instance(tmp_path, bucket, fraud, cap=3)
    assert _post(first, fraud, "203.0.113.1").status_code == 200
    assert _post(second, fraud, "203.0.113.2").status_code == 200
    # A restart (scale to zero and back): a brand-new app, the same bucket.
    restarted = _instance(tmp_path, bucket, fraud, cap=3)
    assert _post(restarted, fraud, "203.0.113.3").status_code == 200
    assert reason(_post(first, fraud, "203.0.113.4")) == "demo-busy"
    assert reason(_post(restarted, fraud, "203.0.113.5")) == "demo-busy"
    assert fraud.assessments == 3


@pytest.mark.parametrize(
    ("reads", "writes"),
    [(403, None), (500, None), ("timeout", None), ("unreachable", None), (None, 403), (None, 503)],
)
def test_an_unreachable_counter_fails_closed(harness, reads, writes) -> None:
    harness.bucket.fail_reads, harness.bucket.fail_writes = reads, writes
    response = harness.start_session()
    assert response.status_code == 503
    assert reason(response) == "bot-check-unavailable"
    assert harness.fraud.assessments == 0
    assert "Set-Cookie" not in response.headers


def test_a_counter_object_that_is_not_a_count_fails_closed(harness) -> None:
    harness.bucket.put(today(harness), "lots")
    response = harness.start_session()
    assert reason(response) == "bot-check-unavailable"
    assert harness.fraud.assessments == 0


@pytest.mark.parametrize("failure", ["timeout", "unreachable", 400, 403, 429, 500, "not-json"])
def test_an_unreachable_fraud_defense_fails_closed(harness, failure) -> None:
    harness.fraud.failure = failure
    response = harness.start_session()
    assert response.status_code == 503
    assert reason(response) == "bot-check-unavailable"
    assert "Set-Cookie" not in response.headers


def test_a_counter_race_with_another_instance_loses_no_count(harness) -> None:
    raced = []

    def other_instance_writes_first(name: str) -> None:
        if not raced:  # once: between this instance's read and its write
            raced.append(name)
            harness.bucket.put(name, "7")

    harness.bucket.before_write = other_instance_writes_first
    assert harness.start_session().status_code == 200
    # The precondition failed, the proxy re-read 7 and wrote 8: the other write was not lost.
    assert harness.bucket.count(today(harness)) == 8
    assert raced == [today(harness)]


# ---- the gate: the cookie, expiry, the key -----------------------------------------------------


def test_without_the_cookie_every_protected_route_is_401_session_required(harness) -> None:
    response = harness.client.get("/status", headers={"Origin": ORIGIN})
    assert response.status_code == 401
    assert reason(response) == "session-required"
    assert response.headers["WWW-Authenticate"] == (
        'Cookie realm="pulsebridge", cookie-name="__session"'
    )


def test_a_bearer_token_is_no_longer_accepted(harness) -> None:
    token = harness.login()
    response = harness.client.get("/status", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert reason(response) == "session-required"


def test_another_cookie_name_is_not_accepted(harness) -> None:
    token = harness.login()
    for cookie in (f"session={token}", f"__Session={token}", f"__session_x={token}"):
        assert harness.client.get("/status", headers={"Cookie": cookie}).status_code == 401


def test_the_session_ends_at_its_expiry(harness) -> None:
    token = harness.login()
    harness.clock.advance(86400 - 1)
    assert harness.client.get("/status", headers=harness.auth(token)).status_code == 200
    harness.clock.advance(1)
    expired = harness.client.get("/status", headers=harness.auth(token))
    assert expired.status_code == 401
    assert reason(expired) == "session-required"


def test_a_token_signed_with_another_key_is_refused(harness) -> None:
    token = harness.login()
    harness.key_file.write_bytes(OTHER_SIGNING_KEY)  # a new secret version
    harness.clock.advance(31)  # past the file cache
    response = harness.client.get("/status", headers=harness.auth(token))
    assert response.status_code == 401
    # A new session works under the new key.
    assert harness.client.get("/status", headers=harness.auth()).status_code == 200


def test_a_forged_or_altered_cookie_is_refused(harness) -> None:
    token = harness.login()
    head, body, sig = token.split(".")
    for bad in (f"{head}.{body}.{sig[::-1]}", f"{head}.{body}", "garbage", ""):
        assert harness.client.get("/status", headers=harness.auth(bad)).status_code == 401


@pytest.mark.parametrize("content", [None, b"", b"too-short-key", b"x" * 2000])
def test_without_a_usable_signing_key_nothing_is_spent(harness, content) -> None:
    if content is None:
        harness.key_file.unlink()
    else:
        harness.key_file.write_bytes(content)
    response = harness.start_session()
    assert response.status_code == 503
    assert reason(response) == "session-not-configured"
    assert harness.fraud.assessments == 0 and harness.bucket.requests == []
    status = harness.client.get("/status", headers={"Cookie": "__session=a.b.c"})
    assert reason(status) == "session-not-configured"


# ---- Origin (the CSRF half that is not SameSite) -----------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "https://evil.example.test",
        ORIGIN + ".evil.test",
        ORIGIN + "/",
        "http://dashboard.example.test",
        "https://DASHBOARD.example.test",
        "null",
        "",
    ],
)
@pytest.mark.parametrize("path", ["/auth/session", "/auth/logout", "/cuff/readings", "/share"])
def test_every_post_needs_an_allowed_origin(harness, origin, path) -> None:
    token = harness.login()
    headers = {"Cookie": f"__session={token}"}
    if origin is not None:
        headers["Origin"] = origin
    response = harness.client.post(
        path, json={"recaptcha_token": harness.fraud.issue()}, headers=headers
    )
    assert response.status_code == 403
    assert reason(response) == "origin-refused"
    assert "Set-Cookie" not in response.headers
    assert harness.fraud.assessments == 1  # only the login's
    assert harness.sender.calls == []


def test_a_refused_origin_uses_no_per_address_attempt(tmp_path) -> None:
    h = build_harness(tmp_path, sessions_per_address_per_hour=1)
    for _ in range(10):
        h.client.post("/auth/session", json={"recaptcha_token": h.fraud.issue()})
    assert h.start_session().status_code == 200


def test_a_get_needs_no_origin(harness) -> None:
    token = harness.login()
    assert (
        harness.client.get("/status", headers={"Cookie": f"__session={token}"}).status_code == 200
    )


# ---- logout and the cookie rules -----------------------------------------------------------------


def test_logout_clears_the_cookie(tmp_path) -> None:
    h = build_harness(tmp_path, path_prefix="/api")
    token = h.login()
    response = h.client.post("/api/auth/logout", headers=h.auth(token))
    assert response.status_code == 204
    assert response.get_data() == b""
    assert response.headers.getlist("Set-Cookie") == [
        "__session=; Path=/api; Max-Age=0; HttpOnly; Secure; SameSite=Strict"
    ]


def test_logout_works_with_an_expired_session_or_none(harness) -> None:
    token = harness.login()
    harness.clock.advance(86400)
    assert harness.client.post("/auth/logout", headers=harness.auth(token)).status_code == 204
    assert harness.client.post("/auth/logout", headers={"Origin": ORIGIN}).status_code == 204


def test_no_other_cookie_ever_leaves_the_proxy(harness) -> None:
    from flask import Response

    def sets_a_cookie() -> Response:
        response = Response("{}", content_type="application/json")
        response.set_cookie("tracking", "1")
        return response

    harness.app.add_url_rule("/cookie-test", "cookie_test", sets_a_cookie)
    token = harness.login()
    response = harness.client.get("/cookie-test", headers=harness.auth(token))
    assert response.status_code == 200
    assert "Set-Cookie" not in response.headers
    # On the session route too, only the session's own cookie is sent.
    assert len(harness.start_session().headers.getlist("Set-Cookie")) == 1


def test_no_cors_headers_are_sent(harness) -> None:
    for response in (
        harness.client.get("/auth/config", headers={"Origin": ORIGIN}),
        harness.start_session(),
        harness.client.options("/auth/session", headers={"Origin": ORIGIN}),
    ):
        assert not [
            name for name, _ in response.headers if name.lower().startswith("access-control")
        ]


def test_a_session_logs_the_score_but_never_a_token(harness, logs) -> None:
    bot_token = harness.fraud.issue(score=0.9)
    response = harness.start_session(bot_token)
    session = session_from(response)
    harness.start_session(harness.fraud.issue(score=0.2))
    events = {e["event"]: e for e in logs.events()}
    assert events["session-created"]["score"] == 0.9
    assert events["bot-check-failed"]["reason"] == "low-score"
    assert events["bot-check-failed"]["score"] == 0.2
    text = logs.text()
    assert bot_token not in text and session not in text and session.split(".")[2] not in text
