"""The three Google Health secrets (decision D2), read from files that Cloud Run mounts.

Why files, not environment variables, and not the Secret Manager API:

- Cloud Run mounts a Secret Manager secret as a volume, and "when reading a volume, Cloud Run
  always fetches the secret value from the Secret Manager to use the value with the latest
  version" (Cloud Run docs, "Configure secrets"). This module reads the files **at the start of
  every run**, so a new refresh-token version written by the connect script (T11) is used by the
  next run without a redeploy. That is how "read from Secret Manager at run time" is met.
- Environment variables are "resolved at instance startup time" (same page): a new version would
  only be seen by a new instance, which can be hours later on a warm service.
- Calling the Secret Manager API from code would need another client library and would put
  secret names and access in this code; the mount keeps both in Terraform (T20).

A missing or unreadable file is not a start-up failure (Cloud Run does not check volume secrets
at start-up either). It is the ``not-configured`` state, which the status route reports so the
dashboard can say so; the reason code says which case it is (``secrets-missing`` or
``secret-unreadable``). Values are never logged, never put in an exception message, and hidden
from ``repr``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from health_sync.config import SecretPaths

# Google documents refresh tokens up to 512 bytes and access tokens up to 2048 bytes
# (Google Identity, "Token size"); client ids and secrets are shorter. 4 KiB is a generous cap
# that stops a wrongly mounted file (a whole JSON key, a binary) being sent anywhere.
MAX_SECRET_BYTES = 4096


class SecretsMissing(Exception):
    """One or more secret files cannot be used. Names only (``client-id``, ``client-secret``,
    ``refresh-token``), never a value or the file content:

    - ``missing``: the file does not exist or is empty: nothing has been stored yet (the
      connect script has not run, or the mount is not configured).
    - ``unreadable``: the file exists but cannot be used: a read error (permissions, a secret
      version Cloud Run cannot access), more than 4 KiB, not UTF-8, or characters a
      credential cannot contain (spaces, control characters). Something is mounted, but wrong.
    """

    def __init__(self, missing: list[str], unreadable: list[str] | None = None) -> None:
        self.missing = missing
        self.unreadable = unreadable or []
        super().__init__(f"Google Health secrets not usable: {self.reason}")

    @property
    def names(self) -> list[str]:
        return self.missing + self.unreadable

    @property
    def reason(self) -> str:
        """The reason code reported by runs and ``/status``, e.g.
        ``secrets-missing:refresh-token`` or ``secret-unreadable:client-id``."""
        parts = []
        if self.missing:
            parts.append("secrets-missing:" + ",".join(self.missing))
        if self.unreadable:
            parts.append("secret-unreadable:" + ",".join(self.unreadable))
        return ";".join(parts)


@dataclass(frozen=True)
class OAuthCredentials:
    """The OAuth client and the user's refresh token. ``repr`` shows nothing."""

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)

    def secret_values(self) -> tuple[str, ...]:
        """For tests and log scrubbing only: the values that must never appear anywhere."""
        return (self.client_id, self.client_secret, self.refresh_token)


_MISSING = object()
_UNREADABLE = object()


def _read(path: str) -> str | object:
    """The secret's text, or ``_MISSING`` / ``_UNREADABLE``."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SECRET_BYTES + 1)
    except FileNotFoundError:
        return _MISSING
    except OSError:
        return _UNREADABLE
    if len(raw) > MAX_SECRET_BYTES:
        return _UNREADABLE
    try:
        text = raw.decode("utf-8").strip()  # a trailing newline from `echo` is common
    except UnicodeDecodeError:
        return _UNREADABLE
    if not text:
        return _MISSING
    # One token, printable ASCII, no spaces: anything else is not a credential we can send.
    if any(not ("!" <= ch <= "~") for ch in text):
        return _UNREADABLE
    return text


def load_credentials(paths: SecretPaths) -> OAuthCredentials:
    """Read the three files now. Raises :class:`SecretsMissing` naming the unusable ones."""
    values = {
        "client-id": _read(paths.client_id),
        "client-secret": _read(paths.client_secret),
        "refresh-token": _read(paths.refresh_token),
    }
    missing = [name for name, value in values.items() if value is _MISSING]
    unreadable = [name for name, value in values.items() if value is _UNREADABLE]
    if missing or unreadable:
        raise SecretsMissing(missing, unreadable)
    return OAuthCredentials(
        client_id=values["client-id"],  # type: ignore[arg-type]
        client_secret=values["client-secret"],  # type: ignore[arg-type]
        refresh_token=values["refresh-token"],  # type: ignore[arg-type]
    )
