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
dashboard can say so. Values are never logged, never put in an exception message, and hidden
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
    """One or more secret files are missing, empty or unreadable. ``names`` lists which
    (``client-id``, ``client-secret``, ``refresh-token``), never a value or the file content."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(f"Google Health secrets not available: {', '.join(names)}")
        self.names = names


@dataclass(frozen=True)
class OAuthCredentials:
    """The OAuth client and the user's refresh token. ``repr`` shows nothing."""

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)

    def secret_values(self) -> tuple[str, ...]:
        """For tests and log scrubbing only: the values that must never appear anywhere."""
        return (self.client_id, self.client_secret, self.refresh_token)


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SECRET_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_SECRET_BYTES:
        return None
    try:
        text = raw.decode("utf-8").strip()  # a trailing newline from `echo` is common
    except UnicodeDecodeError:
        return None
    # One token, printable ASCII, no spaces: anything else is not a credential we can send.
    if not text or any(not ("!" <= ch <= "~") for ch in text):
        return None
    return text


def load_credentials(paths: SecretPaths) -> OAuthCredentials:
    """Read the three files now. Raises :class:`SecretsMissing` naming the unusable ones."""
    values = {
        "client-id": _read(paths.client_id),
        "client-secret": _read(paths.client_secret),
        "refresh-token": _read(paths.refresh_token),
    }
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise SecretsMissing(missing)
    return OAuthCredentials(
        client_id=values["client-id"],  # type: ignore[arg-type]
        client_secret=values["client-secret"],  # type: ignore[arg-type]
        refresh_token=values["refresh-token"],  # type: ignore[arg-type]
    )
