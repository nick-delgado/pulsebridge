"""What ``real_dependencies()`` sets up: the Google libraries' payload logging switched off, TLS
verification on, and Google's fixed endpoints that no flag or environment variable can change.

The last test drives the real ``google-cloud-secret-manager`` client (a dead channel on
127.0.0.1, anonymous credentials) with ``GOOGLE_SDK_PYTHON_LOGGING_SCOPE`` set, and checks the
payload never reaches stderr. The library is not in the repository's dev environment, so that
test is skipped there and runs where the library is installed (``connect.sh``'s environment).
"""

import base64
import http.client
import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from connect_google_health import cli, oauth, patient
from connect_google_health.sdk_logging import QUIET_LOGGERS
from connect_google_health.testing import REFRESH_TOKEN
from pulsebridge_gcp import RequestsTransport

PACKAGE_DIR = Path(__file__).resolve().parents[1]
EVIL = "https://evil.example/collect"


@pytest.fixture
def restore_logging():
    """real_dependencies() changes process-wide logging; put it back for the other tests."""
    saved = {
        name: (list(lg.handlers), lg.propagate, lg.level)
        for name in QUIET_LOGGERS
        for lg in [logging.getLogger(name)]
    }
    saved_debuglevel = http.client.HTTPConnection.debuglevel
    yield
    logging.disable(logging.NOTSET)
    for name, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)
    http.client.HTTPConnection.debuglevel = saved_debuglevel


class HostileEnvironment(dict):
    """Every variable the code could read says "send it to evil.example"."""

    def get(self, key, default=None):
        return EVIL

    def __getitem__(self, key):
        return EVIL

    def __contains__(self, key):
        return True


def test_real_dependencies_switches_off_sdk_payload_logging(monkeypatch, restore_logging):
    monkeypatch.setenv("GOOGLE_SDK_PYTHON_LOGGING_SCOPE", "google.cloud.secretmanager_v1")
    monkeypatch.setenv("GRPC_TRACE", "all")
    monkeypatch.setenv("GRPC_VERBOSITY", "DEBUG")
    logging.getLogger("google").addHandler(logging.StreamHandler())
    http.client.HTTPConnection.debuglevel = 1

    cli.real_dependencies()

    assert "GOOGLE_SDK_PYTHON_LOGGING_SCOPE" not in os.environ
    assert "GRPC_TRACE" not in os.environ
    assert os.environ["GRPC_VERBOSITY"] == "NONE"
    for name in ("google", "grpc", "urllib3", "requests"):
        logger = logging.getLogger(name)
        assert [type(h) for h in logger.handlers] == [logging.NullHandler], name
        assert logger.propagate is False
        assert logger.level == logging.CRITICAL
    assert http.client.HTTPConnection.debuglevel == 0
    # Even a logger configured later below "google" (as api-core does for a scope) is silent.
    child = logging.getLogger("google.cloud.secretmanager_v1.test")
    child.addHandler(logging.StreamHandler())
    child.setLevel(logging.DEBUG)
    assert not child.isEnabledFor(logging.CRITICAL)


def test_real_dependencies_use_googles_fixed_endpoints_and_verify_tls(monkeypatch, restore_logging):
    monkeypatch.setattr(os, "environ", HostileEnvironment())
    deps = cli.real_dependencies()
    transport = deps.google_transport()
    assert isinstance(transport, RequestsTransport)
    assert transport._session.verify is True
    assert deps.token_url == "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
    assert oauth.TOKEN_URL == "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
    assert oauth.AUTH_URL == "https://accounts.google.com/o/oauth2/v2/auth"
    assert patient.IDENTITY_URL == "https://health.googleapis.com/v4/users/me/identity"


def test_no_flag_or_variable_reaches_an_endpoint():
    env = {
        "GCP_PROJECT_ID": "demo-project-1",
        "HEALTHCARE_LOCATION": "us-central1",
        "HEALTHCARE_DATASET_ID": "pulsebridge",
        "FHIR_STORE_ID": "fhir",
        "PATIENT_ID": "PB-SYN-0001",
        "PATIENT_FAMILY_NAME": "Syntheticfamily",
        "PATIENT_GIVEN_NAME": "Syntheticgiven",
        "PATIENT_GENDER": "female",
        "PATIENT_BIRTH_DATE": "1980-04-12",
        **{
            name: EVIL
            for name in (
                "TOKEN_URL", "GOOGLE_TOKEN_URL", "OAUTH_TOKEN_URL", "GOOGLE_HEALTH_TOKEN_URL",
                "AUTH_URL", "GOOGLE_AUTH_URL", "IDENTITY_URL", "GOOGLE_HEALTH_API_URL",
                "GOOGLE_HEALTH_BASE_URL", "HEALTHCARE_API_URL",
            )
        },
    }  # fmt: skip
    config = cli.load_config([], env)
    assert set(cli.Config.__dataclass_fields__) == {
        "project", "prefix", "scope", "port", "paste", "open_browser", "timeout_seconds",
        "reset_client", "disable_previous", "accept_unlisted_scope", "fhir", "demographics",
    }  # fmt: skip
    assert EVIL not in repr(config) and EVIL not in repr(config.demographics)
    flags = {a.dest for a in cli.build_parser()._actions}
    assert not any("url" in flag or "endpoint" in flag for flag in flags)


_REAL_CLIENT_SCRIPT = r"""
import sys
from connect_google_health import cli
from connect_google_health.secret_store import SecretManagerStore, SecretStoreError

if sys.argv[1] == "protected":
    cli.real_dependencies()
from google.api_core.client_options import ClientOptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import secretmanager

client = secretmanager.SecretManagerServiceClient(
    credentials=AnonymousCredentials(),
    client_options=ClientOptions(api_endpoint="127.0.0.1:9"),
)
store = SecretManagerStore("demo-project-1", client)
try:
    store.add_version("pb-google-health-refresh-token", sys.argv[2])
except SecretStoreError:
    pass
"""


def _has_secret_manager() -> bool:
    try:
        return importlib.util.find_spec("google.cloud.secretmanager") is not None
    except ModuleNotFoundError:
        return False


@pytest.mark.skipif(
    not _has_secret_manager(),
    reason="google-cloud-secret-manager is not installed in this environment",
)
@pytest.mark.parametrize("scope", ["google", "google.cloud.secretmanager_v1"])
@pytest.mark.parametrize("mode", ["unprotected", "protected"])
def test_the_real_client_does_not_log_the_payload_once_protected(mode, scope):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GCP_SA_KEY_B64"}
    }
    env.update(
        GOOGLE_SDK_PYTHON_LOGGING_SCOPE=scope,
        PYTHONPATH=str(PACKAGE_DIR),
        GCE_METADATA_HOST="127.0.0.1:9",
        NO_PROXY="*",
    )
    secret = REFRESH_TOKEN + "-sdk-logging-probe"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _REAL_CLIENT_SCRIPT, mode, secret],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    encoded = base64.b64encode(secret.encode()).decode()
    found = encoded in output or secret in output
    if mode == "unprotected":
        # The control: without the protection the library really does print the payload, so
        # the check below would see a leak.
        assert found, "control failed: the SDK did not log the payload; the test proves nothing"
        assert "AddSecretVersion" in output
    else:
        assert not found
        assert "AddSecretVersion" not in output
