"""The session token (D16): an HS256 JWT signed with a random key, 24 hours by default."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fhir_proxy.session_token import InvalidToken, TokenSigner
from fhir_proxy.testing import OTHER_SIGNING_KEY, SIGNING_KEY

ISSUER = "pulsebridge-fhir-proxy"
T0 = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


class Now:
    def __init__(self) -> None:
        self.value = T0

    def __call__(self) -> datetime:
        return self.value


def signer(now: Now, ttl: int = 86400) -> TokenSigner:
    return TokenSigner(issuer=ISSUER, ttl_seconds=ttl, clock=now)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _forge(payload: dict, key: bytes, header: dict | None = None) -> str:
    head = _b64(json.dumps(header or {"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = _b64(hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def _claims(**change: object) -> dict:
    payload = {
        "iss": ISSUER,
        "aud": ISSUER,
        "iat": int(T0.timestamp()),
        "exp": int(T0.timestamp()) + 60,
        "jti": "synthetic-jti-0000000001",
    }
    payload.update(change)
    return {k: v for k, v in payload.items() if v is not None}


def test_issued_token_verifies_and_expires_after_twenty_four_hours() -> None:
    now = Now()
    s = signer(now)
    issued = s.issue(SIGNING_KEY)
    assert issued.expires_at == T0 + timedelta(hours=24)
    claims = s.verify(issued.token, SIGNING_KEY)
    assert claims["iss"] == claims["aud"] == ISSUER
    assert claims["exp"] - claims["iat"] == 24 * 3600
    # No personal data: exactly these claims.
    assert set(claims) == {"iss", "aud", "iat", "exp", "jti"}


def test_the_signature_is_hmac_sha256_under_the_key_itself() -> None:
    token = signer(Now()).issue(SIGNING_KEY).token
    head, body, sig = token.split(".")
    expected = hmac.new(SIGNING_KEY, f"{head}.{body}".encode(), hashlib.sha256).digest()
    assert sig == _b64(expected)
    jwt.decode(
        token, SIGNING_KEY, algorithms=["HS256"], audience=ISSUER, options={"verify_exp": False}
    )


def test_each_token_has_its_own_jti() -> None:
    s = signer(Now())
    jtis = {s.verify(s.issue(SIGNING_KEY).token, SIGNING_KEY)["jti"] for _ in range(20)}
    assert len(jtis) == 20


def test_expiry_boundary_is_exclusive() -> None:
    now = Now()
    s = signer(now)
    token = s.issue(SIGNING_KEY).token
    now.value = T0 + timedelta(hours=24) - timedelta(seconds=1)
    s.verify(token, SIGNING_KEY)  # one second before expiry: still valid
    now.value = T0 + timedelta(hours=24)
    with pytest.raises(InvalidToken, match="expired"):
        s.verify(token, SIGNING_KEY)


def test_lifetime_is_configurable() -> None:
    now = Now()
    s = signer(now, ttl=600)
    token = s.issue(SIGNING_KEY).token
    now.value = T0 + timedelta(seconds=600)
    with pytest.raises(InvalidToken, match="expired"):
        s.verify(token, SIGNING_KEY)


def test_token_signed_with_another_key_is_refused() -> None:
    now = Now()
    s = signer(now)
    token = s.issue(SIGNING_KEY).token
    with pytest.raises(InvalidToken, match="bad-signature"):
        s.verify(token, OTHER_SIGNING_KEY)


def test_altered_payload_is_refused() -> None:
    now = Now()
    s = signer(now)
    head, body, sig = s.issue(SIGNING_KEY).token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["exp"] += 86400
    forged = f"{head}.{_b64(json.dumps(claims).encode())}.{sig}"
    with pytest.raises(InvalidToken, match="bad-signature"):
        s.verify(forged, SIGNING_KEY)


def test_altered_signature_is_refused() -> None:
    now = Now()
    s = signer(now)
    head, body, sig = s.issue(SIGNING_KEY).token.split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    with pytest.raises(InvalidToken, match="bad-signature"):
        s.verify(f"{head}.{body}.{flipped}", SIGNING_KEY)


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none", "typ": "JWT"},
        {"alg": "HS512", "typ": "JWT"},
        {"alg": "RS256", "typ": "JWT"},
        {"typ": "JWT"},
    ],
)
def test_wrong_algorithm_is_refused_even_with_a_valid_key(header: dict) -> None:
    with pytest.raises(InvalidToken, match="wrong-algorithm"):
        signer(Now()).verify(_forge(_claims(), SIGNING_KEY, header), SIGNING_KEY)


def test_alg_none_with_empty_signature_is_refused() -> None:
    head = _b64(json.dumps({"alg": "none"}).encode())
    body = _b64(json.dumps({"iss": ISSUER, "aud": ISSUER, "iat": 1, "exp": 9999999999}).encode())
    with pytest.raises(InvalidToken):
        signer(Now()).verify(f"{head}.{body}.", SIGNING_KEY)


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b", "a.b.c.d", "a b.c.d", "x" * 3000, "é.é.é", "a..b"],
)
def test_wrong_format_is_refused(token: str) -> None:
    with pytest.raises(InvalidToken, match="malformed"):
        signer(Now()).verify(token, SIGNING_KEY)


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "someone-else"},
        {"aud": "someone-else"},
        {"exp": None},
        {"iat": None},
        {"jti": None},
        {"jti": 12345678901234567},
        {"jti": "short"},
        {"jti": "has spaces in it, not base64url"},
        {"iat": int(T0.timestamp()) + 3600},  # issued an hour in the future
    ],
)
def test_claims_are_checked(change: dict) -> None:
    with pytest.raises(InvalidToken, match="bad-claims"):
        signer(Now()).verify(_forge(_claims(**change), SIGNING_KEY), SIGNING_KEY)


def test_token_with_a_longer_lifetime_than_configured_is_refused() -> None:
    iat = int(T0.timestamp())
    payload = _claims(iat=iat, exp=iat + 30 * 86400)
    with pytest.raises(InvalidToken, match="bad-claims"):
        signer(Now()).verify(_forge(payload, SIGNING_KEY), SIGNING_KEY)
