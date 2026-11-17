"""Keep credential material out of everything the smoke test prints or writes.

The smoke test holds two secrets: the reviewer password (read at run time, never from a file in
the repository) and the session token the proxy issues for it. Both are registered here as soon
as they are known, and every line of output, every failure message and the optional JSON report
pass through :meth:`Redactor.__call__` first. That is the backstop; the plan is that no code path
formats a header, a request body or a token in the first place (CLAUDE.md, "Credentials and
secrets").

Two patterns are also scrubbed whatever their value, because a server could echo a credential
the smoke test never saw: ``Bearer <anything>`` and anything shaped like a JWT
(``eyJ...``.``...``.``...``, the shape of the proxy's session token).
"""

from __future__ import annotations

import re

REDACTED = "[redacted]"
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")


class Redactor:
    """Replaces every registered secret (and any bearer token or JWT) with ``[redacted]``."""

    def __init__(self) -> None:
        self._secrets: list[str] = []

    def add(self, secret: str | None) -> None:
        """Register a value that must never be shown. Very short values are refused as a
        mistake rather than silently redacting every occurrence of, say, ``a``."""
        if not secret:
            return
        if len(secret) < 4:
            raise ValueError("refusing to register a secret shorter than 4 characters")
        if secret not in self._secrets:
            self._secrets.append(secret)
            # Longest first, so a secret that contains another is replaced whole.
            self._secrets.sort(key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _BEARER.sub(f"Bearer {REDACTED}", text)
        return _JWT.sub(REDACTED, text)
