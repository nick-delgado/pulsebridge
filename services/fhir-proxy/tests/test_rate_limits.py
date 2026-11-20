"""T46's rate limits through the app, at their default values (``DEMO_LIMITS``) on the harness's
injected clock: each limit and its boundary, the 429 OperationOutcome with Retry-After, the key
(the client address; the token's ``jti``), where each limit sits in the order of checks, the
sampled logs, and the variables' ranges."""

from __future__ import annotations

import jwt
import pytest
from fhir_proxy import app as app_module
from fhir_proxy.config import VARIABLES, ConfigError, ServiceConfig
from fhir_proxy.errors import REASON_SYSTEM
from fhir_proxy.testing import DEMO_LIMITS, ORIGIN, SIGNING_KEY, build_harness

ADDRESS = {"X-Forwarded-For": "203.0.113.20"}


def demo(tmp_path, **overrides):  # noqa: ANN001, ANN201
    """The app with the production defaults of every rate limit."""
    return build_harness(tmp_path, **{**DEMO_LIMITS, **overrides})


def reason(response) -> str:  # noqa: ANN001
    return response.get_json()["issue"][0]["details"]["coding"][0]["code"]


def read(h, token: str | None = None, path: str = "/fhir/metadata", **headers: str):  # noqa: ANN001, ANN201
    return h.client.get(path, headers={**h.auth(token), **headers})


def claims_of(h, token: str) -> dict:  # noqa: ANN001
    # The harness's clock is not the real one, so PyJWT's own expiry check is switched off.
    return jwt.decode(
        token,
        SIGNING_KEY,
        algorithms=["HS256"],
        audience=h.config.token_issuer,
        options={"verify_exp": False, "verify_iat": False},
    )


def cuff_send(h, token: str | None = None):  # noqa: ANN001, ANN201
    return h.client.post(
        "/cuff/readings",
        data=b'{"systolic":120,"diastolic":80,"transport":"mllp"}',
        content_type="application/json",
        headers=h.auth(token),
    )


def share(h, token: str | None = None):  # noqa: ANN001, ANN201
    # No OpenEMR is configured, so the share itself answers 503 share-not-configured: the
    # limits are in the gate, before the route, and count the attempt either way.
    return h.client.post("/share", json={"observationId": "obs-1"}, headers=h.auth(token))


def assert_rate_limited(response, retry_after: str, text: str) -> None:  # noqa: ANN001
    assert response.status_code == 429
    assert response.headers["Retry-After"] == retry_after
    assert response.content_type == "application/fhir+json"
    assert response.get_json() == {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "throttled",
                "details": {"coding": [{"system": REASON_SYSTEM, "code": "rate-limited"}]},
                "diagnostics": app_module.RATE_LIMIT_TEXTS[text],
            }
        ],
    }


# ---- the defaults ----------------------------------------------------------------------------


def test_the_defaults_are_the_tasks_numbers() -> None:
    names = {
        "requests_per_address_per_minute": "REQUESTS_PER_ADDRESS_PER_MINUTE",
        "reads_per_session_per_minute": "READS_PER_SESSION_PER_MINUTE",
        "cuff_sends_per_session_per_10_minutes": "CUFF_SENDS_PER_SESSION_PER_10_MINUTES",
        "shares_per_session_per_10_minutes": "SHARES_PER_SESSION_PER_10_MINUTES",
        "shares_per_instance_per_hour": "SHARES_PER_INSTANCE_PER_HOUR",
    }
    assert DEMO_LIMITS == {
        "requests_per_address_per_minute": 300,
        "reads_per_session_per_minute": 120,
        "cuff_sends_per_session_per_10_minutes": 3,
        "shares_per_session_per_10_minutes": 2,
        "shares_per_instance_per_hour": 30,
    }
    fields = ServiceConfig.__dataclass_fields__
    for field, variable in names.items():
        assert fields[field].default == DEMO_LIMITS[field]
        assert VARIABLES[variable].default == str(DEMO_LIMITS[field])


def test_the_share_endpoint_the_gate_names_is_the_share_routes() -> None:
    from fhir_proxy.share import ROUTE

    assert ROUTE.endpoint == app_module.SHARE_ENDPOINT
    assert (ROUTE.method, ROUTE.path) == ("POST", "/share")


def test_the_capability_statement_says_requests_are_rate_limited(tmp_path) -> None:
    h = demo(tmp_path)
    security = read(h).get_json()["rest"][0]["security"]["description"]
    assert "rate limited" in security
    assert "429" in security
    assert "Retry-After" in security


# ---- reads per session -------------------------------------------------------------------------


def test_120_reads_a_minute_per_session_then_429(tmp_path) -> None:
    h = demo(tmp_path)
    assert all(read(h).status_code == 200 for _ in range(120))
    # Two tokens a second come back: the next is half a second away, rounded up to 1.
    assert_rate_limited(read(h), "1", "session-reads")
    h.clock.advance(0.5)
    assert read(h).status_code == 200
    assert read(h).status_code == 429
    h.clock.advance(60)
    assert sum(read(h).status_code == 200 for _ in range(130)) == 120


def test_every_get_counts_as_a_read(tmp_path) -> None:
    h = demo(tmp_path, reads_per_session_per_minute=60)
    h.sender.answer("/cuff/samples", 200, [])
    for path in ("/fhir/metadata", "/cuff/samples", "/messages", "/no-such-route"):
        for _ in range(15):
            assert read(h, path=path).status_code != 429
    assert_rate_limited(read(h), "1", "session-reads")


def test_a_head_request_counts_as_a_read(tmp_path) -> None:
    h = demo(tmp_path)
    for _ in range(120):
        assert h.client.head("/fhir/metadata", headers=h.auth()).status_code == 200
    assert read(h).status_code == 429


def test_sessions_are_limited_apart(tmp_path) -> None:
    h = demo(tmp_path)
    first, second = h.login(), h.login()
    for _ in range(120):
        read(h, first)
    assert read(h, first).status_code == 429
    assert read(h, second).status_code == 200


def test_the_per_session_key_is_the_tokens_jti(tmp_path) -> None:
    # Two different tokens that carry the same jti share one budget: the key is the claim, not
    # the cookie's text.
    h = demo(tmp_path)
    token = h.login()
    claims = claims_of(h, token)
    twin = jwt.encode({**claims, "iat": claims["iat"] + 1}, SIGNING_KEY, algorithm="HS256")
    assert twin != token
    for _ in range(60):
        assert read(h, token).status_code == 200
        assert read(h, twin).status_code == 200
    assert read(h, token).status_code == 429
    assert read(h, twin).status_code == 429


def test_a_refused_read_reaches_no_upstream(tmp_path) -> None:
    h = demo(tmp_path)
    h.sender.answer("/cuff/samples", 200, [])
    for _ in range(120):
        read(h)
    calls = len(h.sender.calls)
    assert read(h, path="/cuff/samples").status_code == 429
    assert len(h.sender.calls) == calls


# ---- cuff sends per session ----------------------------------------------------------------------


def test_3_cuff_sends_in_any_10_minutes_per_session(tmp_path) -> None:
    h = demo(tmp_path)
    h.sender.answer("/cuff/readings", 200, {"result": "accepted"})
    for _ in range(3):
        assert cuff_send(h).status_code == 200
        h.clock.advance(100)
    # Sends at 0, 100 and 200 s; now 300 s: the first ages out at 600 s.
    refused = cuff_send(h)
    assert_rate_limited(refused, "300", "session-cuff-sends")
    assert len(h.sender.calls) == 3  # the refused send was never forwarded
    h.clock.advance(299)
    assert cuff_send(h).status_code == 429
    h.clock.advance(1)
    assert cuff_send(h).status_code == 200
    assert reason(cuff_send(h)) == "rate-limited"  # the send at 100 s is still counted


def test_cuff_sends_and_reads_are_counted_apart(tmp_path) -> None:
    h = demo(tmp_path)
    h.sender.answer("/cuff/readings", 200, {"result": "accepted"})
    for _ in range(120):
        read(h)
    assert cuff_send(h).status_code == 200  # reads used up; sends are their own limit
    other = h.login()
    for _ in range(3):
        cuff_send(h, other)
    assert read(h, other).status_code == 200


def test_a_cuff_send_cuff_sim_refused_still_counts(tmp_path) -> None:
    h = demo(tmp_path)
    h.sender.answer("/cuff/readings", 422, {"resourceType": "OperationOutcome", "issue": []})
    for _ in range(3):
        assert cuff_send(h).status_code == 422
    assert cuff_send(h).status_code == 429


# ---- shares ----------------------------------------------------------------------------------


def test_2_shares_in_any_10_minutes_per_session(tmp_path) -> None:
    h = demo(tmp_path)
    assert share(h).status_code == 503  # reached the route: share-not-configured
    h.clock.advance(60)
    assert share(h).status_code == 503
    assert_rate_limited(share(h), "540", "session-shares")
    h.clock.advance(540)
    assert share(h).status_code == 503


def test_30_shares_an_hour_by_everyone_on_one_instance(tmp_path) -> None:
    h = demo(tmp_path)
    for n in range(15):
        token = h.login(**{"X-Forwarded-For": f"198.51.100.{n}"})
        assert [share(h, token).status_code for _ in range(2)] == [503, 503]
        h.clock.advance(60)
    # 30 shares at 0, 0, 60, 60, ..., 840, 840 s; now 900 s.
    late = h.login(**{"X-Forwarded-For": "198.51.100.99"})
    assert_rate_limited(share(h, late), "2700", "all-shares")
    h.clock.advance(2700)
    assert share(h, late).status_code == 503


def test_a_share_refused_for_everyone_is_not_counted_against_the_session(tmp_path) -> None:
    h = demo(tmp_path, shares_per_instance_per_hour=1)
    assert share(h, h.login()).status_code == 503
    late = h.login()
    # Without the give-back the third attempt would be refused by the session's own limit
    # (two attempts counted), which says something untrue about this session.
    texts = [share(h, late).get_json()["issue"][0]["diagnostics"] for _ in range(3)]
    assert texts == [app_module.RATE_LIMIT_TEXTS["all-shares"]] * 3
    assert share(h, late).headers["Retry-After"] == "3600"
    h.clock.advance(3600)
    assert [share(h, late).status_code for _ in range(3)] == [503, 429, 429]
    assert (
        share(h, late).get_json()["issue"][0]["diagnostics"]
        == (app_module.RATE_LIMIT_TEXTS["all-shares"])
    )


def test_the_session_limit_on_shares_is_checked_before_the_limit_on_everyone(tmp_path) -> None:
    h = demo(tmp_path, shares_per_instance_per_hour=3)
    token = h.login()
    share(h, token)
    share(h, token)
    for _ in range(5):  # refused by the session's limit: uses none of everyone's
        assert (
            share(h, token).get_json()["issue"][0]["diagnostics"]
            == (app_module.RATE_LIMIT_TEXTS["session-shares"])
        )
    assert share(h, h.login()).status_code == 503


# ---- requests per address --------------------------------------------------------------------


def test_300_requests_a_minute_per_address_on_every_route_together(tmp_path) -> None:
    h = demo(tmp_path)
    token = h.login(**ADDRESS)  # 1
    for _ in range(100):
        h.client.get("/auth/config", headers=ADDRESS)
        h.client.get("/status", headers=ADDRESS)  # no cookie: 401, counted all the same
    for _ in range(99):
        read(h, token, **ADDRESS)
    # 1 + 200 + 99 = 300 requests; five a second come back.
    refused = h.client.get("/auth/config", headers=ADDRESS)
    assert_rate_limited(refused, "1", "address")
    assert (
        h.client.get("/auth/config", headers={"X-Forwarded-For": "203.0.113.21"}).status_code == 200
    )
    h.clock.advance(0.2)
    assert h.client.get("/auth/config", headers=ADDRESS).status_code == 200


def test_the_address_limit_comes_first(tmp_path) -> None:
    h = demo(tmp_path)
    for _ in range(300):
        h.client.get("/auth/config", headers=ADDRESS)
    calls = h.fraud.assessments
    # Each of these would be refused or answered differently later in the gate; the address
    # limit answers first.
    attempts = [
        h.client.get("/status", headers=ADDRESS),  # no cookie (401)
        h.client.get("/fhir/metadata?access_token=x", headers=ADDRESS),  # token in the URL (400)
        h.client.post("/auth/logout", headers=ADDRESS),  # no Origin (403)
        h.start_session(**ADDRESS),  # a session attempt
    ]
    assert [r.status_code for r in attempts] == [429] * 4
    assert {reason(r) for r in attempts} == {"rate-limited"}
    assert h.fraud.assessments == calls


def test_requests_refused_later_in_the_gate_count_towards_the_address(tmp_path) -> None:
    h = demo(tmp_path)
    for _ in range(150):
        h.client.post("/auth/logout", headers=ADDRESS)  # no Origin: 403
        h.client.get("/fhir/metadata", headers={"Cookie": "__session=x", **ADDRESS})  # 401
    assert h.client.get("/auth/config", headers=ADDRESS).status_code == 429


def test_the_session_attempt_limit_still_applies(tmp_path) -> None:
    h = demo(tmp_path)
    assert [h.start_session(**ADDRESS).status_code for _ in range(6)] == [200] * 5 + [429]
    assert h.start_session(**ADDRESS).headers["Retry-After"] == "3600"  # the hourly limit's


def test_the_per_session_limits_come_after_the_session_check(tmp_path) -> None:
    h = demo(tmp_path)
    for _ in range(120):
        read(h)
    # A request without a valid session is 401, never 429 from a session's limit.
    assert h.client.get("/fhir/metadata", headers={"Cookie": "__session=junk"}).status_code == 401
    assert h.client.get("/fhir/metadata").status_code == 401


# ---- logs ------------------------------------------------------------------------------------


def test_refusals_are_logged_sampled(tmp_path, logs) -> None:
    h = demo(tmp_path)
    for _ in range(320):
        h.client.get("/auth/config", headers=ADDRESS)
    other = {"X-Forwarded-For": "203.0.113.30"}
    for _ in range(310):
        h.client.get("/auth/config", headers=other)
    limited = [e for e in logs.events() if e.get("event") == "rate-limited"]
    assert [(e["client_address"], e["limit"]) for e in limited] == [
        ("203.0.113.20", "address"),
        ("203.0.113.30", "address"),
    ]
    assert all(e["retry_after"] == 1 for e in limited)
    h.clock.advance(61)
    assert not [e for e in logs.events() if e.get("event") == "session-events-suppressed"]
    for _ in range(301):  # the bucket is full again: the 301st is the next refusal
        h.client.get("/auth/config", headers=ADDRESS)
    summaries = [e for e in logs.events() if e.get("event") == "session-events-suppressed"]
    assert [(e["kind"], e["count"]) for e in summaries] == [("rate-limited", 28)]


def test_a_session_refusal_logs_the_limit_and_never_the_token(tmp_path, logs) -> None:
    h = demo(tmp_path)
    token = h.login()
    for _ in range(121):
        read(h, token)
    (event,) = [e for e in logs.events() if e.get("event") == "rate-limited"]
    assert event["limit"] == "session-reads"
    claims = claims_of(h, token)
    assert token not in logs.text()
    assert claims["jti"] not in logs.text()


# ---- the variables -----------------------------------------------------------------------------


RANGES = {
    "REQUESTS_PER_ADDRESS_PER_MINUTE": (1200, 6000),
    "READS_PER_SESSION_PER_MINUTE": (60, 1200),
    "CUFF_SENDS_PER_SESSION_PER_10_MINUTES": (1, 30),
    "SHARES_PER_SESSION_PER_10_MINUTES": (1, 20),
    "SHARES_PER_INSTANCE_PER_HOUR": (1, 600),
}


@pytest.fixture
def base_env(tmp_path) -> dict[str, str]:  # noqa: ANN001
    return {
        "GCP_PROJECT_ID": "synthetic-project",
        "HEALTHCARE_LOCATION": "us-central1",
        "HEALTHCARE_DATASET_ID": "phr",
        "FHIR_STORE_ID": "fhir",
        "AUDIT_FHIR_STORE_ID": "audit",
        "HL7V2_STORE_ID": "hl7",
        "PATIENT_ID": "SYN-1",
        "SESSION_SIGNING_KEY_FILE": str(tmp_path / "key"),
        "ALLOWED_ORIGINS": ORIGIN,
        "CUFF_SIM_URL": "https://cuff-sim.example.test",
        "RECAPTCHA_SITE_KEY": "synthetic-site-key-000000000000000000000",
        "DEMO_COUNTER_BUCKET": "synthetic-demo-counters",
        "PUBLIC_BASE_URL": "https://proxy.example.test",
    }


@pytest.mark.parametrize(("name", "bounds"), RANGES.items())
def test_each_limit_is_a_variable_with_a_bounded_range(base_env, name, bounds) -> None:
    low, high = bounds
    # REQUESTS_PER_ADDRESS_PER_MINUTE's range starts at 60; its low bound here is the most
    # reads a session may make, which it may not be below (tested below).
    env = {
        **base_env,
        "READS_PER_SESSION_PER_MINUTE": "1200",
        "REQUESTS_PER_ADDRESS_PER_MINUTE": "6000",
    }
    field = name.lower()
    for good in (low, high):
        assert getattr(ServiceConfig.from_env({**env, name: str(good)}), field) == good
    for bad in (str(low - 1), str(high + 1), "1.5", "-1", "1e3"):
        with pytest.raises(ConfigError) as caught:
            ServiceConfig.from_env({**env, name: bad})
        assert name in str(caught.value)


def test_the_address_range_starts_at_60(base_env) -> None:
    env = {**base_env, "READS_PER_SESSION_PER_MINUTE": "60"}
    assert ServiceConfig.from_env({**env, "REQUESTS_PER_ADDRESS_PER_MINUTE": "60"})
    # Refused by its own range, not only because it is below the reads limit.
    with pytest.raises(
        ConfigError, match="REQUESTS_PER_ADDRESS_PER_MINUTE must be a whole number from 60 to 6000"
    ):
        ServiceConfig.from_env({**env, "REQUESTS_PER_ADDRESS_PER_MINUTE": "59"})


def test_the_address_limit_may_not_be_below_a_sessions_reads(base_env) -> None:
    env = {**base_env, "READS_PER_SESSION_PER_MINUTE": "200"}
    assert ServiceConfig.from_env({**env, "REQUESTS_PER_ADDRESS_PER_MINUTE": "200"})
    with pytest.raises(ConfigError, match="at least READS_PER_SESSION_PER_MINUTE"):
        ServiceConfig.from_env({**env, "REQUESTS_PER_ADDRESS_PER_MINUTE": "199"})


def test_unset_limits_take_the_defaults(base_env) -> None:
    config = ServiceConfig.from_env(base_env)
    assert {name: getattr(config, name) for name in DEMO_LIMITS} == DEMO_LIMITS


def test_the_configured_numbers_are_the_ones_applied(tmp_path) -> None:
    h = demo(
        tmp_path,
        reads_per_session_per_minute=70,
        cuff_sends_per_session_per_10_minutes=1,
        shares_per_session_per_10_minutes=1,
    )
    h.sender.answer("/cuff/readings", 200, {"result": "accepted"})
    assert sum(read(h).status_code == 200 for _ in range(80)) == 70
    assert [cuff_send(h).status_code for _ in range(2)] == [200, 429]
    assert [share(h).status_code for _ in range(2)] == [503, 429]
