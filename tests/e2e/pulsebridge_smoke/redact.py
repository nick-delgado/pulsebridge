"""Keep credential material out of everything the smoke test prints or writes.

The smoke test holds two secrets: the stack's session-signing key (read at run time from Secret
Manager, never from a file; registered in its text forms, ``session.key_text_forms``) and the
session token signed with it. Both are registered here as soon as they are known, and every
line of output, every failure message and the optional JSON report pass through
:meth:`Redactor.__call__` first. That is the backstop; the plan is that no code path
formats a header, a request body or a token in the first place (CLAUDE.md, "Credentials and
secrets").

Two patterns are also scrubbed whatever their value, because a server could echo a credential
the smoke test never saw: ``Bearer <anything>`` and anything shaped like a JWT
(``eyJ...``.``...``.``...``, the shape of the proxy's session token).

**Escaped forms.** A check detail often shows a server value with ``{value!r}`` or inside a
``dict``, and the JSON report is ``json.dumps``. Both escape a backslash, a newline, a quote or
(for JSON) a non-ASCII character, so the secret as written there is not the secret's own text.
:meth:`Redactor.add` therefore also registers each such form: Python's repr escaping with
either quote character (``repr`` picks the quote from the whole string, which may hold more than
the secret) and ``json.dumps`` with and without ``ensure_ascii``.
"""

from __future__ import annotations

import json
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
        for form in escaped_forms(secret):
            if form not in self._secrets:
                self._secrets.append(form)
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets.sort(key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _BEARER.sub(f"Bearer {REDACTED}", text)
        return _JWT.sub(REDACTED, text)


def _repr_inner(text: str, quote: str) -> str:
    """The body of Python's ``repr`` of ``text`` when ``repr`` delimits it with ``quote``."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == quote:
            out.append("\\" + quote)
        elif ch in "'\"":
            out.append(ch)
        else:
            out.append(repr(ch)[1:-1])  # \n, \t, \x07, \u2028 ...; printable as is
    return "".join(out)


def escaped_forms(secret: str) -> list[str]:
    """The secret and every escaped form in which it can appear in the smoke test's output."""
    forms = [
        secret,
        _repr_inner(secret, "'"),
        _repr_inner(secret, '"'),
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    ]
    return list(dict.fromkeys(forms))
