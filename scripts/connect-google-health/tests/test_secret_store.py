"""The Secret Manager adapter, against a fake client and a fake exceptions module, and the
secret names against the identity module (T04)."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from connect_google_health.secret_store import (
    SECRET_KEYS,
    SecretManagerStore,
    SecretStoreError,
    check_prefix,
    secret_id,
)
from connect_google_health.testing import CLIENT_SECRET, REFRESH_TOKEN

REPO = Path(__file__).resolve().parents[3]


# A stand-in for google.api_core.exceptions with the same class hierarchy.
class GoogleAPIError(Exception):
    pass


class GoogleAPICallError(GoogleAPIError):
    pass


class RetryError(GoogleAPIError):
    pass


class NotFound(GoogleAPICallError):
    pass


class PermissionDenied(GoogleAPICallError):
    pass


class FailedPrecondition(GoogleAPICallError):
    pass


class ServiceUnavailable(GoogleAPICallError):
    pass


ERRORS = SimpleNamespace(
    GoogleAPIError=GoogleAPIError,
    GoogleAPICallError=GoogleAPICallError,
    NotFound=NotFound,
    PermissionDenied=PermissionDenied,
    FailedPrecondition=FailedPrecondition,
)
SECRET = "pb-google-health-refresh-token"  # noqa: S105 - a secret name, not a value
PARENT = f"projects/demo-project-1/secrets/{SECRET}"


class FakeClient:
    def __init__(self, *, raises=None, data=b"", version_name=None, versions=()):
        self.raises = raises
        self.data = data
        self.version_name = version_name
        self.versions = versions
        self.requests = []
        self.timeouts = []

    def _maybe_raise(self):
        if self.raises is not None:
            raise self.raises

    def access_secret_version(self, request, timeout=None):
        self.timeouts.append(timeout)
        self.requests.append(("access", request))
        self._maybe_raise()
        return SimpleNamespace(payload=SimpleNamespace(data=self.data))

    def add_secret_version(self, request, timeout=None):
        self.timeouts.append(timeout)
        self.requests.append(("add", request))
        self._maybe_raise()
        return SimpleNamespace(name=self.version_name)

    def list_secret_versions(self, request, timeout=None):
        self.timeouts.append(timeout)
        self.requests.append(("list", request))
        self._maybe_raise()
        return [SimpleNamespace(name=f"{PARENT}/versions/{n}") for n in self.versions]

    def disable_secret_version(self, request, timeout=None):
        self.timeouts.append(timeout)
        self.requests.append(("disable", request))


def store(client):
    return SecretManagerStore("demo-project-1", client, errors=ERRORS)


def test_secret_ids_are_the_identity_modules_three_google_health_secrets():
    main_tf = (REPO / "infra/modules/identity/main.tf").read_text()
    secrets_tf = (REPO / "infra/modules/identity/secrets.tf").read_text()
    declared = set(re.findall(r'"(google-health-[a-z-]+)"\s*=', main_tf))
    assert declared == {f"google-health-{key}" for key in SECRET_KEYS}
    assert 'secret_id = "${var.name_prefix}-${each.key}"' in secrets_tf
    assert [secret_id("pb", key) for key in SECRET_KEYS] == [
        "pb-google-health-client-id",
        "pb-google-health-client-secret",
        "pb-google-health-refresh-token",
    ]


@pytest.mark.parametrize("good", ["pb", "a", "dev-1", "abcdefghijkl"])
def test_prefix_rule_accepts(good):
    assert check_prefix(good) == good


@pytest.mark.parametrize("bad", ["", "1pb", "pb-", "PB", "abcdefghijklm", "p_b", "pb/x"])
def test_prefix_rule_refuses(bad):
    with pytest.raises(ValueError):
        check_prefix(bad)


def test_access_latest_asks_for_the_latest_version():
    client = FakeClient(data=CLIENT_SECRET.encode())
    assert store(client).access_latest(SECRET) == CLIENT_SECRET
    assert client.requests == [("access", {"name": f"{PARENT}/versions/latest"})]


@pytest.mark.parametrize("error", [NotFound("x"), FailedPrecondition("x")])
def test_no_usable_version_is_none(error):
    assert store(FakeClient(raises=error)).access_latest(SECRET) is None


def test_access_permission_denied_names_the_role():
    with pytest.raises(SecretStoreError) as caught:
        store(FakeClient(raises=PermissionDenied(CLIENT_SECRET))).access_latest(SECRET)
    assert "roles/secretmanager.secretAccessor" in str(caught.value)
    assert SECRET in str(caught.value)
    assert CLIENT_SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_other_api_errors_show_only_the_type():
    with pytest.raises(SecretStoreError) as caught:
        store(FakeClient(raises=ServiceUnavailable(CLIENT_SECRET))).access_latest(SECRET)
    assert "ServiceUnavailable" in str(caught.value)
    assert CLIENT_SECRET not in str(caught.value)


def test_add_version_sends_the_bytes_and_returns_the_number():
    client = FakeClient(version_name=f"{PARENT}/versions/7")
    assert store(client).add_version(SECRET, REFRESH_TOKEN) == 7
    assert client.requests == [
        ("add", {"parent": PARENT, "payload": {"data": REFRESH_TOKEN.encode()}})
    ]


def test_add_version_to_a_missing_secret_says_apply_terraform():
    with pytest.raises(SecretStoreError) as caught:
        store(FakeClient(raises=NotFound("x"))).add_version(SECRET, REFRESH_TOKEN)
    assert "Apply the Terraform" in str(caught.value)
    assert REFRESH_TOKEN not in str(caught.value)


def test_add_version_permission_denied_names_the_role():
    with pytest.raises(SecretStoreError) as caught:
        store(FakeClient(raises=PermissionDenied("x"))).add_version(SECRET, REFRESH_TOKEN)
    assert "roles/secretmanager.secretVersionAdder" in str(caught.value)


def test_add_version_without_a_version_name_fails():
    with pytest.raises(SecretStoreError):
        store(FakeClient(version_name="")).add_version(SECRET, REFRESH_TOKEN)


def test_disable_versions_before_only_disables_older_enabled_versions():
    client = FakeClient(versions=(1, 3, 4, 5))
    assert store(client).disable_versions_before(SECRET, 4) == 2
    assert client.requests == [
        ("list", {"parent": PARENT, "filter": "state:ENABLED"}),
        ("disable", {"name": f"{PARENT}/versions/1"}),
        ("disable", {"name": f"{PARENT}/versions/3"}),
    ]


def test_disable_permission_denied_names_the_role():
    with pytest.raises(SecretStoreError) as caught:
        store(FakeClient(raises=PermissionDenied("x"))).disable_versions_before(SECRET, 2)
    assert "roles/secretmanager.secretVersionManager" in str(caught.value)


def test_every_call_has_a_timeout():
    client = FakeClient(data=b"x", version_name=f"{PARENT}/versions/2", versions=(1,))
    s = store(client)
    s.access_latest(SECRET)
    s.add_version(SECRET, REFRESH_TOKEN)
    s.disable_versions_before(SECRET, 2)
    assert client.timeouts == [30.0, 30.0, 30.0, 30.0]


def test_a_retry_timeout_is_reported_by_type():
    with pytest.raises(SecretStoreError) as caught:
        store(FakeClient(raises=RetryError(CLIENT_SECRET))).access_latest(SECRET)
    assert "RetryError" in str(caught.value)
    assert CLIENT_SECRET not in str(caught.value)
