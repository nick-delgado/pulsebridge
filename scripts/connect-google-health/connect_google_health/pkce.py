"""PKCE (RFC 7636) and the OAuth ``state`` value.

PKCE, "Proof Key for Code Exchange", ties the authorization code to this run of the script. We
make a random **code verifier**, send only its SHA-256 hash (the **code challenge**) with the
authorization request, and send the verifier itself with the code exchange. Google issues tokens
only if the two match, so a code intercepted on its way back (for example by another program on
the machine reading the loopback redirect) is useless without the verifier, which never left this
process.

The ``state`` value is the other half: a random string sent with the request and returned by
Google unchanged with the redirect. A redirect carrying any other ``state`` did not come from the
request this run made (it could be a forged redirect, a cross-site request forgery), and is
refused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

# RFC 7636 section 4.1: code_verifier = 43*128unreserved, where
# unreserved = ALPHA / DIGIT / "-" / "." / "_" / "~". Google's installed-app guide repeats it.
_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}", re.ASCII)

# 32 random bytes, base64url without padding, is exactly 43 characters: RFC 7636 section 4.1's
# own recommendation ("a 32-octet sequence ... base64url-encoded to produce a 43-octet URL safe
# string").
_VERIFIER_BYTES = 32
_STATE_BYTES = 32


def _b64url(data: bytes) -> str:
    """Base64url without padding (RFC 7636 Appendix A)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_code_verifier() -> str:
    return _b64url(secrets.token_bytes(_VERIFIER_BYTES))


def code_challenge_s256(verifier: str) -> str:
    """``BASE64URL(SHA256(ASCII(code_verifier)))``: RFC 7636 section 4.2, method ``S256``.

    The ``plain`` method (challenge = verifier) is never used: it protects nothing if the
    authorization request itself is seen.
    """
    if not _VERIFIER.fullmatch(verifier):
        raise ValueError("code verifier must be 43 to 128 unreserved characters (RFC 7636 4.1)")
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    return _b64url(secrets.token_bytes(_STATE_BYTES))


def state_matches(expected: str, received: str | None) -> bool:
    """Constant-time comparison, so the check leaks nothing about the expected value."""
    if received is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8"))
