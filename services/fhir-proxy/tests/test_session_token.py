"""The session token (D9): HS256 JWT, key derived from the password with scrypt, 8 hours."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fhir_proxy import session_token
from fhir_proxy.session_token import (
    InvalidToken,
    TokenSigner,
    derive_signing_key,
    passwords_match,
)
from fhir_proxy.testing import OTHER_PASSWORD, PASSWORD

ISSUER = "pulsebridge-fhir-proxy"
T0 = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


class Now:
    def __init__(self) -> None:
        self.value = T0

    def __call__(self) -> datetime:
        return self.value


def signer(now: Now, ttl: int = 28800) -> TokenSigner:
    return TokenSigner(issuer=ISSUER, ttl_seconds=ttl, clock=now)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _forge(payload: dict, key: bytes, header: dict | None = None) -> str:
    head = _b64(json.dumps(header or {"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = _b64(hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def test_issued_token_verifies_and_expires_after_eight_hours() -> None:
    now = Now()
    s = signer(now)
    issued = s.issue(PASSWORD)
    assert issued.expires_at == T0 + timedelta(hours=8)
    claims = s.verify(issued.token, PASSWORD)
    assert claims["iss"] == claims["aud"] == ISSUER
    assert claims["exp"] - claims["iat"] == 8 * 3600


def test_expiry_boundary_is_exclusive() -> None:
    now = Now()
    s = signer(now)
    token = s.issue(PASSWORD).token
    now.value = T0 + timedelta(hours=8) - timedelta(seconds=1)
    s.verify(token, PASSWORD)  # one second before expiry: still valid
    now.value = T0 + timedelta(hours=8)
    with pytest.raises(InvalidToken, match="expired"):
        s.verify(token, PASSWORD)


def test_lifetime_is_configurable() -> None:
    now = Now()
    s = signer(now, ttl=600)
    token = s.issue(PASSWORD).token
    now.value = T0 + timedelta(seconds=600)
    with pytest.raises(InvalidToken, match="expired"):
        s.verify(token, PASSWORD)


def test_token_signed_under_the_previous_password_is_refused() -> None:
    now = Now()
    s = signer(now)
    token = s.issue(PASSWORD).token
    with pytest.raises(InvalidToken, match="bad-signature"):
        s.verify(token, OTHER_PASSWORD)


def test_altered_payload_is_refused() -> None:
    now = Now()
    s = signer(now)
    head, body, sig = s.issue(PASSWORD).token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["exp"] += 86400
    forged = f"{head}.{_b64(json.dumps(claims).encode())}.{sig}"
    with pytest.raises(InvalidToken, match="bad-signature"):
        s.verify(forged, PASSWORD)


def test_altered_signature_is_refused() -> None:
    now = Now()
    s = signer(now)
    head, body, sig = s.issue(PASSWORD).token.split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    with pytest.raises(InvalidToken, match="bad-signature"):
        s.verify(f"{head}.{body}.{flipped}", PASSWORD)


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
    now = Now()
    key = derive_signing_key(PASSWORD, ISSUER)
    payload = {
        "iss": ISSUER,
        "aud": ISSUER,
        "iat": int(T0.timestamp()),
        "exp": int(T0.timestamp()) + 60,
    }
    with pytest.raises(InvalidToken, match="wrong-algorithm"):
        signer(now).verify(_forge(payload, key, header), PASSWORD)


def test_alg_none_with_empty_signature_is_refused() -> None:
    head = _b64(json.dumps({"alg": "none"}).encode())
    body = _b64(json.dumps({"iss": ISSUER, "aud": ISSUER, "iat": 1, "exp": 9999999999}).encode())
    with pytest.raises(InvalidToken):
        signer(Now()).verify(f"{head}.{body}.", PASSWORD)


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b", "a.b.c.d", "a b.c.d", "x" * 3000, "é.é.é", "a..b"],
)
def test_wrong_format_is_refused(token: str) -> None:
    with pytest.raises(InvalidToken, match="malformed"):
        signer(Now()).verify(token, PASSWORD)


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "someone-else"},
        {"aud": "someone-else"},
        {"exp": None},
        {"iat": None},
    ],
)
def test_claims_are_checked(change: dict) -> None:
    key = derive_signing_key(PASSWORD, ISSUER)
    payload = {
        "iss": ISSUER,
        "aud": ISSUER,
        "iat": int(T0.timestamp()),
        "exp": int(T0.timestamp()) + 60,
    }
    payload.update(change)
    payload = {k: v for k, v in payload.items() if v is not None}
    with pytest.raises(InvalidToken, match="bad-claims"):
        signer(Now()).verify(_forge(payload, key), PASSWORD)


def test_token_with_a_longer_lifetime_than_configured_is_refused() -> None:
    key = derive_signing_key(PASSWORD, ISSUER)
    iat = int(T0.timestamp())
    payload = {"iss": ISSUER, "aud": ISSUER, "iat": iat, "exp": iat + 30 * 86400}
    with pytest.raises(InvalidToken, match="bad-claims"):
        signer(Now()).verify(_forge(payload, key), PASSWORD)


def test_signing_key_is_scrypt_of_the_password_salted_with_the_issuer() -> None:
    expected = hashlib.scrypt(
        PASSWORD.encode(),
        salt=b"pulsebridge/fhir-proxy/session-token/v1|" + ISSUER.encode(),
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    assert derive_signing_key(PASSWORD, ISSUER) == expected
    assert derive_signing_key(PASSWORD, "another-deployment") != expected
    # A token is HMAC under that key, and PyJWT agrees.
    token = signer(Now()).issue(PASSWORD).token
    jwt.decode(
        token, expected, algorithms=["HS256"], audience=ISSUER, options={"verify_exp": False}
    )


def test_password_comparison_uses_compare_digest_on_fixed_length_digests(monkeypatch) -> None:
    seen = []
    real = hmac.compare_digest

    def spy(a, b):  # noqa: ANN001, ANN202
        seen.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(session_token.hmac, "compare_digest", spy)
    assert passwords_match(PASSWORD, PASSWORD)
    assert not passwords_match("x", PASSWORD)
    assert not passwords_match(PASSWORD + "x", PASSWORD)
    assert seen == [(32, 32), (32, 32), (32, 32)]
