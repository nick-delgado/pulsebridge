"""Secret Manager, behind a three-method interface so tests use a fake.

The three secrets (decision D2) are created empty by the identity module (T04) as
``<prefix>-google-health-client-id``, ``…-client-secret`` and ``…-refresh-token``. This script
only adds **versions** to them: a secret is a named container, and each value written to it is
a new numbered version. ``health-sync`` mounts version ``latest`` (the most recently created) as
a file and reads it at the start of every run, so a new version is used without a redeploy.

Nothing is ever destroyed. Older refresh-token versions are left enabled unless the deployer
asks for ``--disable-previous`` (see the README for why that is opt-in).

Errors name the secret and the IAM role that is missing, never a value. The real adapter calls
``google-cloud-secret-manager``; its exceptions module is injected too, so the adapter's own
logic is tested without the library installed.
"""

from __future__ import annotations

import re
from types import ModuleType
from typing import Protocol

SECRET_KEYS = ("client-id", "client-secret", "refresh-token")
# Each Secret Manager call, retries included, ends within this many seconds.
TIMEOUT_SECONDS = 30.0
DEFAULT_PREFIX = "pb"  # the identity module's name_prefix default

# The identity module's name_prefix validation, exactly.
_PREFIX = re.compile(r"[a-z]([a-z0-9-]{0,10}[a-z0-9])?", re.ASCII)
_VERSION_NAME = re.compile(r"projects/[^/]+/secrets/[^/]+/versions/(\d+)", re.ASCII)


def secret_id(prefix: str, key: str) -> str:
    """``<prefix>-google-health-<key>``, as the identity module names them."""
    if key not in SECRET_KEYS:
        raise ValueError(f"unknown secret key {key!r}")
    return f"{prefix}-google-health-{key}"


def check_prefix(prefix: str) -> str:
    if not _PREFIX.fullmatch(prefix):
        raise ValueError(
            "the name prefix must be 1-12 characters of a-z, 0-9 and '-', start with a letter "
            "and not end with '-' (the identity module's name_prefix rule)"
        )
    return prefix


class SecretStoreError(Exception):
    """``str()`` is a sentence naming the secret and the fix; safe to print."""


class SecretStore(Protocol):
    def access_latest(self, secret: str) -> str | None:
        """The latest version's value, or ``None`` when there is no usable version (none yet,
        or the latest is disabled or destroyed)."""

    def add_version(self, secret: str, value: str) -> int:
        """Store ``value`` as a new version; return its number."""

    def disable_versions_before(self, secret: str, version: int) -> int:
        """Disable every enabled version numbered below ``version``; return how many."""


def _google_exceptions() -> ModuleType:
    from google.api_core import exceptions

    return exceptions


class SecretManagerStore:
    """The real store: ``google.cloud.secretmanager.SecretManagerServiceClient`` with the
    deployer's Application Default Credentials (``gcloud auth application-default login``)."""

    def __init__(self, project: str, client: object, errors: ModuleType | None = None) -> None:
        self._project = project
        self._client = client
        self._errors = errors if errors is not None else _google_exceptions()

    @classmethod
    def from_environment(cls, project: str) -> SecretManagerStore:
        from google.cloud import secretmanager

        return cls(project, secretmanager.SecretManagerServiceClient())

    def _secret_path(self, secret: str) -> str:
        return f"projects/{self._project}/secrets/{secret}"

    def _denied(self, secret: str, role: str) -> SecretStoreError:
        return SecretStoreError(
            f"Permission denied on secret {secret}: your account needs {role} on it "
            "(or a project role that includes it). Check `gcloud auth application-default "
            "login` was run with the right account."
        )

    def _missing(self, secret: str) -> SecretStoreError:
        return SecretStoreError(
            f"Secret {secret} does not exist in project {self._project}. Apply the Terraform "
            "env root first (the identity module creates it), or pass the --name-prefix it used."
        )

    def access_latest(self, secret: str) -> str | None:
        errors = self._errors
        try:
            response = self._client.access_secret_version(  # type: ignore[attr-defined]
                request={"name": f"{self._secret_path(secret)}/versions/latest"},
                timeout=TIMEOUT_SECONDS,
            )
        except errors.NotFound:
            # No version yet (the state right after `terraform apply`), or no such secret; a
            # missing secret is reported by add_version, which fails the same way.
            return None
        except errors.FailedPrecondition:
            # The latest version is disabled or destroyed: health-sync could not read it
            # either, so it counts as absent.
            return None
        except errors.PermissionDenied:
            raise self._denied(secret, "roles/secretmanager.secretAccessor") from None
        except errors.GoogleAPIError as exc:  # API errors and RetryError (timeout)
            raise SecretStoreError(
                f"Reading secret {secret} failed ({type(exc).__name__})."
            ) from None
        try:
            return response.payload.data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def add_version(self, secret: str, value: str) -> int:
        errors = self._errors
        try:
            version = self._client.add_secret_version(  # type: ignore[attr-defined]
                request={
                    "parent": self._secret_path(secret),
                    "payload": {"data": value.encode("utf-8")},
                },
                timeout=TIMEOUT_SECONDS,
            )
        except errors.NotFound:
            raise self._missing(secret) from None
        except errors.PermissionDenied:
            raise self._denied(secret, "roles/secretmanager.secretVersionAdder") from None
        except errors.GoogleAPIError as exc:  # API errors and RetryError (timeout)
            raise SecretStoreError(
                f"Storing a version of secret {secret} failed ({type(exc).__name__})."
            ) from None
        match = _VERSION_NAME.fullmatch(getattr(version, "name", ""))
        if not match:
            raise SecretStoreError(f"Secret Manager answered with no version for {secret}.")
        return int(match.group(1))

    def disable_versions_before(self, secret: str, version: int) -> int:
        errors = self._errors
        disabled = 0
        try:
            listed = self._client.list_secret_versions(  # type: ignore[attr-defined]
                request={"parent": self._secret_path(secret), "filter": "state:ENABLED"},
                timeout=TIMEOUT_SECONDS,
            )
            for item in listed:
                match = _VERSION_NAME.fullmatch(getattr(item, "name", ""))
                if match and int(match.group(1)) < version:
                    self._client.disable_secret_version(  # type: ignore[attr-defined]
                        request={"name": item.name}, timeout=TIMEOUT_SECONDS
                    )
                    disabled += 1
        except errors.PermissionDenied:
            raise self._denied(secret, "roles/secretmanager.secretVersionManager") from None
        except errors.GoogleAPIError as exc:  # API errors and RetryError (timeout)
            raise SecretStoreError(
                f"Disabling older versions of {secret} failed ({type(exc).__name__}) after "
                f"{disabled} were disabled."
            ) from None
        return disabled
