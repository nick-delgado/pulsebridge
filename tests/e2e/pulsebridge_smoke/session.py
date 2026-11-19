"""A demo session for the smoke test, signed here with the stack's own signing key (D16).

The proxy has no password any more: a visitor gets a session after an invisible bot check in a
browser, which a command-line test cannot pass, and the proxy has no bypass for it on purpose.
So the smoke test does what the proxy itself does after a pass: it signs a session token with
the key in Secret Manager. Whoever can read that key can already end or forge every session, so
this needs no new trust; it needs ``roles/secretmanager.secretAccessor`` on
``<prefix>-session-signing-key`` for the operator's own ``gcloud`` login, as reading the old
reviewer password did.

**The token** is what ``fhir_proxy.session_token`` issues: a JWT (RFC 7519) in compact JWS form
(RFC 7515), header ``{"alg": "HS256", "typ": "JWT"}``, claims ``iss`` and ``aud`` (both the
proxy's ``TOKEN_ISSUER``), ``iat``, ``exp`` and a random ``jti``, signed with HMAC-SHA256
(RFC 7518 section 3.2). It is built with the standard library on purpose, not with the proxy's
code: the smoke test checks that the deployed proxy accepts a standard token, so it must not
share the proxy's implementation. Its lifetime is short (15 minutes, under the proxy's
``SESSION_TTL_SECONDS``, which the proxy enforces).

**The key** is read with ``gcloud secrets versions access latest
--format=get(payload.data)``: gcloud's reference says plain output "will be formatted as UTF-8
which can corrupt binary secrets" and gives this form for the raw bytes, as URL-safe base64
(docs.cloud.google.com/sdk/gcloud/reference/secrets/versions/access, read 19 November 2026).
So the key never touches a file and survives being 32 raw random bytes. The proxy's rule for
what the key is (``fhir_proxy.signing_key``) is applied here too: the bytes, with one trailing
newline dropped when at least 32 bytes remain; fewer than 32 bytes is refused. The key is held
in memory only, never printed, and its text forms are registered with the redactor.

**The issuer** defaults to what module phr sets (``TOKEN_ISSUER =
"pulsebridge-fhir-proxy:<project_id>:<name_prefix>"``, infra/modules/phr/fhir_proxy.tf), from the
project and prefix the command already has; ``--token-issuer`` overrides it.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import secrets
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime

from pulsebridge_smoke.children import child_environment, gcloud_said

MIN_KEY_BYTES = 32  # fhir_proxy.signing_key.MIN_KEY_BYTES
MAX_KEY_BYTES = 1024
TOKEN_LIFETIME_SECONDS = 900
ISSUER_PREFIX = "pulsebridge-fhir-proxy"  # module phr: "pulsebridge-fhir-proxy:<project>:<prefix>"


class SigningKeyError(Exception):
    """The signing key could not be read or is not a usable key. The message never quotes it."""


def secret_name(prefix: str) -> str:
    return f"{prefix}-session-signing-key"


def default_issuer(project: str, prefix: str) -> str:
    return f"{ISSUER_PREFIX}:{project}:{prefix}"


def key_from_secret_bytes(data: bytes) -> bytes:
    """The proxy's rule (``fhir_proxy.signing_key``): the bytes, one trailing newline dropped
    when the rest is still long enough, at least 32 bytes."""
    if len(data) > MAX_KEY_BYTES:
        raise SigningKeyError("the signing key's latest version is longer than 1 KiB")
    if data.endswith(b"\n") and len(data) > MIN_KEY_BYTES:
        data = data[:-1]
        if data.endswith(b"\r") and len(data) > MIN_KEY_BYTES:
            data = data[:-1]
    if len(data) < MIN_KEY_BYTES:
        raise SigningKeyError(
            f"the signing key's latest version is shorter than {MIN_KEY_BYTES} bytes, so it "
            "cannot be the proxy's key"
        )
    return data


def secret_manager_signing_key(
    project: str, prefix: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> bytes:
    """``gcloud secrets versions access latest`` for ``<prefix>-session-signing-key`` with the
    caller's own credentials. Returns the key's bytes; stdout is captured, never printed."""
    try:
        done = run(
            [
                "gcloud",
                "secrets",
                "versions",
                "access",
                "latest",
                f"--secret={secret_name(prefix)}",
                f"--project={project}",
                "--format=get(payload.data)",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=child_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SigningKeyError(f"gcloud did not run ({type(exc).__name__})") from None
    if done.returncode != 0:
        # stderr of a failed access holds no secret (there was no value to print); it is
        # redacted again when printed.
        raise SigningKeyError(
            f"could not read {secret_name(prefix)} with gcloud (exit {done.returncode}). "
            "Check that gcloud is logged in as you (gcloud auth list), that "
            "CLOUDSDK_CORE_PROJECT is the stack's project, that SECRET_NAME_PREFIX is its "
            "secret_name_prefix output, and that your account may access the secret "
            "(roles/secretmanager.secretAccessor). " + gcloud_said(done.stderr)
        )
    text = "".join((done.stdout or "").split())
    try:
        # URL-safe base64, as gcloud prints payload.data; padding may be left out.
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise SigningKeyError(f"gcloud's answer for {secret_name(prefix)} was not base64") from None
    if not data:
        raise SigningKeyError(f"{secret_name(prefix)} has an empty latest version")
    return key_from_secret_bytes(data)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def session_token(
    key: bytes,
    issuer: str,
    *,
    now: datetime | None = None,
    lifetime: int = TOKEN_LIFETIME_SECONDS,
    jti: str | None = None,
) -> str:
    """An HS256 session token with the proxy's claims (see the module docstring)."""
    issued = int((now or datetime.now(UTC)).timestamp())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": issuer,
        "aud": issuer,
        "iat": issued,
        "exp": issued + lifetime,
        "jti": jti or secrets.token_urlsafe(16),
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, claims)
    )
    signature = hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def key_text_forms(key: bytes) -> list[str]:
    """Every text form the key could be shown in, for the redactor: the text itself when it is
    UTF-8, and its base64 encodings (gcloud's URL-safe one and the standard one, with and
    without padding)."""
    forms = []
    with contextlib.suppress(UnicodeDecodeError):
        forms.append(key.decode("utf-8"))
    for encoded in (base64.urlsafe_b64encode(key), base64.b64encode(key)):
        text = encoded.decode("ascii")
        forms += [text, text.rstrip("=")]
    return [f for f in dict.fromkeys(forms) if len(f) >= 4]
