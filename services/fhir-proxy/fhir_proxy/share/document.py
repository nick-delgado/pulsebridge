"""The OpenEMR client document: the private key, client ID and OpenEMR's TLS certificate.

Module openemr (infra/modules/openemr/README.md, "What fhir-proxy gets") stores ONE JSON
document per client registration as a Secret Manager version, and module phr mounts version
``latest`` as a file (``OPENEMR_CLIENT_FILE``)::

    {"format": "pulsebridge.openemr-client.v1", "client_id": "...",
     "token_endpoint_auth_method": "private_key_jwt", "kid": "<RFC 7638 thumbprint>",
     "alg": "RS384", "private_key": "<PEM, PKCS#8>", "site_addr_oath": "https://<ip>",
     "token_url": "https://<ip>/oauth2/default/token", "fhir_base_url": "https://<ip>/apis/...",
     "scope": "<the registered scopes>", "server_certificate_pem": "<PEM>", "created": "..."}

Why it is read on every share and never cached: the file may not exist yet (the VM registers the
client on its first boot), and a re-registration (database recreated, certificate replaced)
replaces the key, the client ID and the certificate together. A cached copy would pair an old
key with a new registration.

A document that cannot be used is refused before OpenEMR is called (503 ``share-not-configured``)
with one of a fixed set of sentences. Nothing from the document is ever echoed: not in an
answer, not in a log line, not in a trace. Only ``private_key`` is secret; the rest is plain
configuration, but it is still never quoted (a log or an answer names the field, not its value).
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from fhir_proxy.config import OpenEmrSettings

FORMAT = "pulsebridge.openemr-client.v1"
MAX_DOCUMENT_BYTES = 64 * 1024
MIN_RSA_BITS = 2048
# Module openemr refuses any client_id outside this before it reaches SQL or a file.
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{16,128}", re.ASCII)
_KID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)

# Problem code -> the fixed sentence the 503 answer carries (after "...not configured: ").
PROBLEMS = {
    "missing": "the OpenEMR client document is not there yet (the VM has not registered the "
    "client)",
    "unreadable": "the OpenEMR client document cannot be read",
    "not-json": "the OpenEMR client document is not a JSON object",
    "wrong-format": "the OpenEMR client document has an unknown format",
    "bad-field": "the OpenEMR client document lacks a field or has one in the wrong form",
    "bad-key": "the OpenEMR client document's private key is not a usable RSA key",
    "bad-certificate": "the OpenEMR client document's server certificate is not usable",
    "address-mismatch": "the OpenEMR client document names another OpenEMR address than "
    "OPENEMR_SITE_ADDRESS",
    "scope-mismatch": "the OpenEMR client document's scopes differ from OPENEMR_SCOPES (the "
    "VM may not have registered the new client yet)",
}


class DocumentProblem(Exception):
    """The document cannot be used; ``code`` is a key of :data:`PROBLEMS`, ``field`` the field
    name when one is at fault (a name from this module, never a value)."""

    def __init__(self, code: str, field_name: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field_name


@dataclass(frozen=True)
class ClientDocument:
    client_id: str
    kid: str
    scope: str
    token_url: str
    certificate_pem: str
    # Never printed: repr=False keeps the key out of any repr, traceback or debugger output.
    private_key: RSAPrivateKey = field(repr=False)
    # A fingerprint of the whole file, to tell whether a re-read found a new registration.
    fingerprint: str = field(repr=False, default="")

    def ssl_context(self) -> ssl.SSLContext:
        return pinned_context(self.certificate_pem)


def pinned_context(certificate_pem: str) -> ssl.SSLContext:
    """A TLS context that trusts ONLY OpenEMR's own certificate (module openemr, "TLS").

    ``create_default_context(cadata=...)`` loads that one certificate and, because a trust
    source is given, none of the system's public authorities. Hostname checking stays on: the
    certificate names the VM's IP (subject alternative name ``IP:<internal_ip>``), and Python
    matches an IP host against IP SANs. ``verify_mode`` stays ``CERT_REQUIRED``. Python 3.13's
    default context also sets ``VERIFY_X509_STRICT``; module openemr's certificate is shaped
    for it. Verification is never switched off anywhere in this package.
    """
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cadata=certificate_pem)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not (context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED):
        raise AssertionError("TLS verification must stay on")  # pragma: no cover - a guard
    return context


def _text(doc: dict, name: str, pattern: re.Pattern[str] | None = None, limit: int = 4096) -> str:
    value = doc.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise DocumentProblem("bad-field", name)
    if pattern is not None and not pattern.fullmatch(value):
        raise DocumentProblem("bad-field", name)
    return value


def parse_document(data: bytes, settings: OpenEmrSettings) -> ClientDocument:
    """Check every field the share relies on; raise :class:`DocumentProblem` otherwise."""
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentProblem("unreadable")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DocumentProblem("not-json") from None
    if not isinstance(doc, dict):
        raise DocumentProblem("not-json")
    if doc.get("format") != FORMAT:
        raise DocumentProblem("wrong-format")
    client_id = _text(doc, "client_id", _CLIENT_ID)
    kid = _text(doc, "kid", _KID)
    if doc.get("alg") != "RS384":
        raise DocumentProblem("bad-field", "alg")
    if doc.get("token_endpoint_auth_method") != "private_key_jwt":
        raise DocumentProblem("bad-field", "token_endpoint_auth_method")
    # The assertion's aud is token_url (spike S1: any other audience is refused), and every
    # call goes to the configured address, so a document from another OpenEMR is refused here,
    # where the reason is clear, instead of as an invalid_client later.
    for name, expected in (
        ("site_addr_oath", settings.site_address),
        ("token_url", settings.token_url),
        ("fhir_base_url", settings.fhir_base_url),
    ):
        if _text(doc, name).rstrip("/") != expected:
            raise DocumentProblem("address-mismatch", name)
    scope = _text(doc, "scope", limit=512)
    if sorted(scope.split()) != sorted(settings.scopes) or len(set(scope.split())) != len(
        scope.split()
    ):
        raise DocumentProblem("scope-mismatch", "scope")
    try:
        key = serialization.load_pem_private_key(
            _text(doc, "private_key", limit=16384).encode("ascii"), password=None
        )
    except (ValueError, TypeError, UnicodeEncodeError):
        raise DocumentProblem("bad-key", "private_key") from None
    if not isinstance(key, RSAPrivateKey) or key.key_size < MIN_RSA_BITS:
        raise DocumentProblem("bad-key", "private_key")
    certificate = _text(doc, "server_certificate_pem", limit=16384)
    try:
        pinned_context(certificate)
    except (ssl.SSLError, ValueError):
        raise DocumentProblem("bad-certificate", "server_certificate_pem") from None
    return ClientDocument(
        client_id=client_id,
        kid=kid,
        scope=" ".join(scope.split()),
        token_url=settings.token_url,
        certificate_pem=certificate,
        private_key=key,
        fingerprint=hashlib.sha256(data).hexdigest(),
    )


def load_document(settings: OpenEmrSettings) -> ClientDocument:
    """Read and check the mounted file now (no cache: see the module docstring)."""
    try:
        with open(settings.client_file, "rb") as handle:
            data = handle.read(MAX_DOCUMENT_BYTES + 1)
    except FileNotFoundError:
        raise DocumentProblem("missing") from None
    except OSError:
        raise DocumentProblem("unreadable") from None
    if not data.strip():
        # A secret with no version mounts as... nothing usable; an empty file reads the same.
        raise DocumentProblem("missing")
    return parse_document(data, settings)
