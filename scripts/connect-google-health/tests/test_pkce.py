"""PKCE (RFC 7636) and the state value."""

import re

import pytest
from connect_google_health import pkce

# RFC 7636 Appendix B, the worked S256 example: 32 random octets, their base64url encoding (the
# verifier) and the S256 challenge.
RFC7636_OCTETS = bytes(
    [116, 24, 223, 180, 151, 153, 224, 37, 79, 250, 96, 125, 216, 173, 187, 186,
     22, 212, 37, 77, 105, 214, 191, 240, 91, 88, 5, 88, 83, 132, 141, 121]
)  # fmt: skip
RFC7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_encoding_matches_rfc7636_appendix_b(monkeypatch):
    monkeypatch.setattr(pkce.secrets, "token_bytes", lambda n: RFC7636_OCTETS[:n])
    assert pkce.new_code_verifier() == RFC7636_VERIFIER


def test_s256_matches_rfc7636_appendix_b():
    assert pkce.code_challenge_s256(RFC7636_VERIFIER) == RFC7636_CHALLENGE


def test_new_verifier_is_43_unreserved_characters_and_random():
    verifiers = {pkce.new_code_verifier() for _ in range(50)}
    assert len(verifiers) == 50
    for verifier in verifiers:
        assert re.fullmatch(r"[A-Za-z0-9\-._~]{43}", verifier)
        # and it is accepted by the challenge function, unpadded
        assert "=" not in pkce.code_challenge_s256(verifier)


@pytest.mark.parametrize("bad", ["a" * 42, "a" * 129, "a" * 42 + "+", "a" * 42 + "/"])
def test_challenge_refuses_a_verifier_outside_rfc7636(bad):
    with pytest.raises(ValueError):
        pkce.code_challenge_s256(bad)


def test_state_is_random_and_compared_exactly():
    state = pkce.new_state()
    assert len(state) >= 43 and state != pkce.new_state()
    assert pkce.state_matches(state, state)
    assert not pkce.state_matches(state, state[:-1])
    assert not pkce.state_matches(state, state + "x")
    assert not pkce.state_matches(state, None)
    assert not pkce.state_matches(state, "")
