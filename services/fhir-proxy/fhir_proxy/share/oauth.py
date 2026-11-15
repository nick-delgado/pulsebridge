"""OAuth 2.0 client credentials with a signed client assertion (``private_key_jwt``).

Terms (spike S1, "Terms used"):

- **Client credentials grant** (RFC 6749 section 4.4): a server asks for an access token for
  itself; no person logs in.
- **Client assertion** (RFC 7523, JWT bearer client authentication): instead of a shared
  secret the client sends a short JSON Web Token signed with its private key. OpenEMR checks
  the signature against the public key (JWKS) registered with the client. So the proxy holds a
  key, never a password, and what it sends cannot be replayed for long (``exp`` 60 s, a unique
  ``jti``).
- **Access token**: what OpenEMR hands back; it goes in ``Authorization: Bearer`` on each FHIR
  call. OpenEMR's client-credentials tokens last 300 s and come with no refresh token, so the
  proxy simply asks again when one expires.

The assertion (spike S1, recipe step 7; module openemr, "What fhir-proxy gets"):

    header  {"alg": "RS384", "typ": "JWT", "kid": <document kid>}
    claims  {"iss": client_id, "sub": client_id, "aud": token_url, "iat": now,
             "exp": now + 60, "jti": <random, 128 bits>}

``aud`` is OpenEMR's own token URL, which derives from ``site_addr_oath`` (configuration):
S1 saw any other audience refused. PyJWT signs it, with ``cryptography`` for RSA.

Neither the assertion nor the token is ever logged, put in a trace, or kept anywhere but the
in-memory cache below (the token) and the one request that carries it.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import jwt

from fhir_proxy.share.document import ClientDocument

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_LIFETIME_SECONDS = 60
# A token is reused until this long before it expires, so it never expires mid-share (a share
# takes a few seconds; the request budget is 50 s).
TOKEN_MARGIN_SECONDS = 60
MAX_TOKEN_LIFETIME_SECONDS = 3600
_TOKEN = re.compile(r"[\x21-\x7e]{1,8192}", re.ASCII)


def client_assertion(document: ClientDocument, now: datetime) -> str:
    issued = int(now.timestamp())
    claims = {
        "iss": document.client_id,
        "sub": document.client_id,
        "aud": document.token_url,
        "iat": issued,
        "exp": issued + ASSERTION_LIFETIME_SECONDS,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(
        claims, document.private_key, algorithm="RS384", headers={"kid": document.kid}
    )


def token_request_form(document: ClientDocument, assertion: str) -> dict[str, str]:
    """The form fields of ``POST /oauth2/default/token`` (S1 recipe step 8)."""
    return {
        "grant_type": "client_credentials",
        # Exactly what the client was registered with: an unregistered scope is silently left
        # out of the token (S1), and the document always lists the registered set.
        "scope": document.scope,
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": assertion,
    }


@dataclass(frozen=True)
class TokenAnswer:
    access_token: str = field(repr=False)
    expires_in: int


def read_token_answer(body: object) -> TokenAnswer | None:
    """A successful token answer (RFC 6749 section 5.1), or None when it is not one."""
    if not isinstance(body, dict):
        return None
    token = body.get("access_token")
    kind = body.get("token_type")
    expires = body.get("expires_in")
    if not (isinstance(token, str) and _TOKEN.fullmatch(token)):
        return None
    if not (isinstance(kind, str) and kind.lower() == "bearer"):
        return None
    if not (isinstance(expires, int) and not isinstance(expires, bool) and expires > 0):
        return None
    return TokenAnswer(token, min(expires, MAX_TOKEN_LIFETIME_SECONDS))


def oauth_error(body: object) -> str | None:
    """RFC 6749 section 5.2's ``error`` code of a refused token request, when there is one."""
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        code = body["error"]
        return code if re.fullmatch(r"[a-z_]{1,64}", code) else None
    return None


@dataclass
class _Cached:
    token: str = field(repr=False)
    usable_until: float
    obtained_at: float


class TokenCache:
    """Access tokens in memory, per (client ID, token URL, scope), thread-safe.

    A token is reused for its lifetime minus :data:`TOKEN_MARGIN_SECONDS` (300 s tokens: 240 s
    of reuse), so a share does not fetch one each time (T15 AC). It is keyed on the client ID,
    so a re-registration's new client never uses the old client's token. Nothing here is ever
    logged; ``repr`` never shows a token."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._tokens: dict[tuple[str, str, str], _Cached] = {}

    @staticmethod
    def key(document: ClientDocument) -> tuple[str, str, str]:
        return (document.client_id, document.token_url, document.scope)

    def get(self, document: ClientDocument) -> tuple[str, float] | None:
        """``(token, seconds since it was obtained)`` while it is usable, else None."""
        now = self._monotonic()
        with self._lock:
            cached = self._tokens.get(self.key(document))
            if cached is None:
                return None
            if now >= cached.usable_until:
                del self._tokens[self.key(document)]
                return None
            return cached.token, now - cached.obtained_at

    def put(self, document: ClientDocument, answer: TokenAnswer) -> None:
        usable = answer.expires_in - TOKEN_MARGIN_SECONDS
        if usable <= 0:
            return  # too short-lived to reuse: fetched again next time
        now = self._monotonic()
        with self._lock:
            self._tokens[self.key(document)] = _Cached(answer.access_token, now + usable, now)

    def forget(self, client_id: str) -> None:
        """Drop every token of a client (OpenEMR refused it, or the client was refused)."""
        with self._lock:
            for key in [k for k in self._tokens if k[0] == client_id]:
                del self._tokens[key]
