"""Fixtures for the smoke test's own tests. They never reach a deployment: the stack is
``e2e_fake_stack`` (real fhir-proxy, cuff-sim and v2-to-fhir code over fakes), and every value
is synthetic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The smoke test is run as `python -m pulsebridge_smoke` with this directory on the path (the
# Makefile's `smoke` target); pytest's importlib mode does not add it, so add it here.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from e2e_fake_stack import PASSWORD, Stack, build_stack  # noqa: E402
from fhir_proxy.share.testing import make_certificate, make_key  # noqa: E402


@pytest.fixture(scope="session")
def key_and_cert():
    return make_key(), make_certificate(["127.0.0.1"])


@pytest.fixture
def stack(tmp_path: Path, monkeypatch, key_and_cert) -> Stack:
    key, cert = key_and_cert
    return build_stack(tmp_path, monkeypatch, key, cert)


@pytest.fixture
def real_stack(tmp_path: Path, monkeypatch, key_and_cert) -> Stack:
    key, cert = key_and_cert
    return build_stack(tmp_path, monkeypatch, key, cert, real_data=True)


@pytest.fixture
def password() -> str:
    return PASSWORD
