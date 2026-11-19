"""The Fraud Defense client: the request it builds and the verdict rule, over the fake."""

from __future__ import annotations

import pytest
from fhir_proxy.bot_check import BotCheckUnavailable, FraudDefense, token_has_shape
from fhir_proxy.testing import SITE_KEY, FakeFraudDefense


def client(fake: FakeFraudDefense, **change: object) -> FraudDefense:
    settings = dict(
        project="example-project",
        site_key=SITE_KEY,
        action="view_demo",
        min_score=0.5,
        hostnames=["dashboard.example.test"],
        timeout=7.0,
    )
    settings.update(change)
    return FraudDefense(fake, **settings)


def test_the_request_is_the_documented_one() -> None:
    fake = FakeFraudDefense()
    token = fake.issue()
    verdict = client(fake).assess(token)
    assert verdict.passed and verdict.reason == "passed" and verdict.score == 0.9
    sent = fake.requests[0]
    assert sent["url"].endswith("/v1/projects/example-project/assessments")
    assert sent["event"] == {"token": token, "siteKey": SITE_KEY, "expectedAction": "view_demo"}
    assert sent["timeout"] == 7.0


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({}, "invalid-token"),  # valid left out: false
        ({"tokenProperties": {"valid": False, "invalidReason": "DUPE"}}, "invalid-token"),
        (
            {
                "tokenProperties": {
                    "valid": True,
                    "action": "x",
                    "hostname": "dashboard.example.test",
                }
            },
            "wrong-action",
        ),
        (
            {
                "tokenProperties": {"valid": True, "action": "view_demo", "hostname": "x.test"},
                "riskAnalysis": {"score": 0.9},
            },
            "wrong-hostname",
        ),
        (
            {
                "tokenProperties": {
                    "valid": True,
                    "action": "view_demo",
                    "hostname": "dashboard.example.test",
                },
                "riskAnalysis": {},  # score left out: 0.0
            },
            "low-score",
        ),
    ],
)
def test_the_verdict_rule(answer: dict, reason: str) -> None:
    verdict = client(FakeFraudDefense()).judge(answer)
    assert not verdict.passed
    assert verdict.reason == reason


def test_a_hostname_is_compared_without_regard_to_case() -> None:
    answer = {
        "tokenProperties": {
            "valid": True,
            "action": "view_demo",
            "hostname": "Dashboard.Example.Test",
        },
        "riskAnalysis": {"score": 0.5},
    }
    assert client(FakeFraudDefense()).judge(answer).passed


def test_only_an_enum_like_invalid_reason_is_kept() -> None:
    judge = client(FakeFraudDefense()).judge
    assert judge({"tokenProperties": {"invalidReason": "EXPIRED"}}).invalid_reason == "EXPIRED"
    assert judge({"tokenProperties": {"invalidReason": "<b>x</b>"}}).invalid_reason is None


@pytest.mark.parametrize(
    "answer",
    [
        [],
        "x",
        {"tokenProperties": []},
        {"tokenProperties": {"valid": "true"}},
        {"tokenProperties": {"valid": True}, "riskAnalysis": {"score": "0.9"}},
        {"tokenProperties": {"valid": True}, "riskAnalysis": {"score": True}},
        {"tokenProperties": {"valid": True, "action": 5}},
    ],
)
def test_an_answer_of_the_wrong_shape_is_no_verdict(answer: object) -> None:
    with pytest.raises(BotCheckUnavailable, match="bad-answer"):
        client(FakeFraudDefense()).judge(answer)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("timeout", "timeout"),
        ("unreachable", "unreachable"),
        (403, "http-403"),
        ("not-json", "bad-answer"),
    ],
)
def test_failures_are_fixed_codes(failure: object, code: str) -> None:
    fake = FakeFraudDefense()
    fake.failure = failure
    with pytest.raises(BotCheckUnavailable) as raised:
        client(fake).assess(fake.issue())
    assert str(raised.value) == code


def test_a_token_made_with_another_key_is_an_invalid_token() -> None:
    # The reference: KEY_MISMATCH, "The key used to generate the token does not match the
    # siteKey", inside a normal (200) assessment.
    fake = FakeFraudDefense()
    verdict = client(fake, site_key="another-site-key-00000000000").assess(fake.issue())
    assert not verdict.passed
    assert (verdict.reason, verdict.invalid_reason) == ("invalid-token", "KEY_MISMATCH")


def test_an_unlisted_invalid_reason_is_still_a_failure() -> None:
    # "Ensure that applications can handle values not explicitly listed."
    verdict = client(FakeFraudDefense()).judge(
        {"tokenProperties": {"valid": False, "invalidReason": "SOME_FUTURE_REASON"}}
    )
    assert (verdict.passed, verdict.invalid_reason) == (False, "SOME_FUTURE_REASON")


@pytest.mark.parametrize(
    ("token", "ok"),
    [
        ("03" + "A" * 18, True),
        ("a" * 19, False),
        ("a-b_c" * 1000, True),
        ("a" * 8192, True),
        ("a" * 8193, False),
        ("a.b" * 10, False),
        ("a+b/" * 10, False),
        ("a" * 30 + "\n", False),
        (None, False),
        (123, False),
    ],
)
def test_the_token_shape(token: object, ok: bool) -> None:
    assert token_has_shape(token) is ok
