"""The reviewer's session token: a JWT (RFC 7519) signed with HMAC-SHA256 (``HS256``).

Construction (D9, docs/spikes/s6.md, and the orchestrator's call that rotating the password
must end every session):

1. **Signing key from the password, with a password KDF.** ``key = scrypt(password, salt,
   N=2^14, r=8, p=1, 32 bytes)`` (RFC 7914, Python's ``hashlib.scrypt``), with
   ``salt = "pulsebridge/fhir-proxy/session-token/v1|" + issuer``. A token is
   ``HMAC-SHA256(key, header.payload)``, so whoever holds a token can test password guesses
   offline against it. A plain hash or HKDF of the password would make each guess cost one
   hash; scrypt makes each cost about 16 MiB of memory and tens of milliseconds. The real
   defence is still the password itself (s6: 24 or more random characters, at least 16), which
   no offline search can reach; scrypt raises the cost for a weaker one. The salt is not secret:
   it separates this use of the password from any other, and the issuer part makes a table
   precomputed for one deployment useless for another that sets its own ``TOKEN_ISSUER``.
2. **One secret, not two.** The key is derived from the password in force, so a new password
   version gives a new key and every token signed under the old one fails its signature check:
   rotation revokes all sessions at once, with nothing to store (s6, "Rotation").
3. **A fixed algorithm.** Verification accepts ``HS256`` only (``algorithms=["HS256"]``), so a
   token whose header says ``none``, ``HS512`` or ``RS256`` is refused before any key is used:
   the "algorithm confusion" attacks on JWT need a verifier that lets the token choose.
4. **Claims.** ``iss`` and ``aud`` (both the issuer), ``iat``, ``exp`` (``iat`` + the lifetime,
   8 hours by default) and ``jti`` (random, for log correlation if ever needed; not stored).
   No personal data. The expiry is checked against an injected clock: valid while
   ``now < exp``.

The token is a bearer token sent in ``Authorization: Bearer <token>``; it is never a cookie (D9:
nothing for cross-site request forgery to ride on).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

ALGORITHM = "HS256"
SALT_LABEL = b"pulsebridge/fhir-proxy/session-token/v1|"
# scrypt cost (RFC 7914): N=2^14, r=8 uses 128 * N * r bytes = 16 MiB, under Python's default
# maxmem of 32 MiB, and takes tens of milliseconds. It runs once per password, not per request.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
# A session token is about 250 characters. Anything far longer is refused before parsing.
MAX_TOKEN_LENGTH = 2048
# The three base64url parts of a compact JWS (RFC 7515 section 7.1), nothing else.
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{1,1024}\.[A-Za-z0-9_-]{1,1024}\.[A-Za-z0-9_-]{1,512}")
# A token issued in the future by more than this (clock skew between instances) is refused.
MAX_FUTURE_SKEW = timedelta(seconds=60)


class InvalidToken(Exception):
    """The token is not valid. ``str()`` is a fixed reason code, never the token."""


@dataclass(frozen=True)
class IssuedToken:
    token: str
    expires_at: datetime


def derive_signing_key(password: str, issuer: str) -> bytes:
    """The HMAC key for tokens signed under ``password`` (see the module docstring)."""
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=SALT_LABEL + issuer.encode("ascii"),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )


def passwords_match(submitted: str, actual: str) -> bool:
    """Constant-time comparison (``hmac.compare_digest``).

    An ordinary ``==`` stops at the first differing character, so its timing tells an attacker
    how much of a guess was right. ``compare_digest`` takes the same time for every input of one
    length, but its documentation warns that it may leak the lengths, so both sides are first
    turned into fixed-length SHA-256 digests: the comparison then always runs on 32 bytes.
    """
    left = hashlib.sha256(submitted.encode("utf-8")).digest()
    right = hashlib.sha256(actual.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


class TokenSigner:
    """Issues and verifies session tokens for the password in force."""

    def __init__(
        self,
        *,
        issuer: str,
        ttl_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._issuer = issuer
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        # One derived key, for the last password seen, keyed by the password's SHA-256 (so the
        # password itself is not kept here as well).
        self._cached: tuple[bytes, bytes] | None = None

    def _key(self, password: str) -> bytes:
        fingerprint = hashlib.sha256(password.encode("utf-8")).digest()
        with self._lock:
            if self._cached is not None and hmac.compare_digest(self._cached[0], fingerprint):
                return self._cached[1]
        key = derive_signing_key(password, self._issuer)
        with self._lock:
            self._cached = (fingerprint, key)
        return key

    def issue(self, password: str) -> IssuedToken:
        now = self._clock().replace(microsecond=0)
        expires = now + self._ttl
        claims = {
            "iss": self._issuer,
            "aud": self._issuer,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
            "jti": secrets.token_urlsafe(12),
        }
        token = jwt.encode(claims, self._key(password), algorithm=ALGORITHM)
        return IssuedToken(token=token, expires_at=expires)

    def verify(self, token: str, password: str) -> dict:
        """The token's claims, or :class:`InvalidToken` with a reason code."""
        if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidToken("malformed")
        if not TOKEN_SHAPE.fullmatch(token):
            raise InvalidToken("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidToken("malformed") from None
        # Checked before any key is used, and again by PyJWT (algorithms=[...]).
        if header.get("alg") != ALGORITHM:
            raise InvalidToken("wrong-algorithm")
        try:
            claims = jwt.decode(
                token,
                self._key(password),
                algorithms=[ALGORITHM],
                audience=self._issuer,
                issuer=self._issuer,
                # Expiry and issue time are checked below against the injected clock.
                options={
                    "require": ["iss", "aud", "iat", "exp"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidSignatureError:
            raise InvalidToken("bad-signature") from None
        except jwt.PyJWTError:
            raise InvalidToken("bad-claims") from None
        exp, iat = claims.get("exp"), claims.get("iat")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (exp, iat)):
            raise InvalidToken("bad-claims")
        now = self._clock().timestamp()
        if now >= exp:
            raise InvalidToken("expired")
        if iat > now + MAX_FUTURE_SKEW.total_seconds() or exp - iat > self._ttl.total_seconds():
            raise InvalidToken("bad-claims")
        return claims
