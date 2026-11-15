"""Fixtures for the share tests. The fakes and the environment builder are in
``fhir_proxy.share.testing`` (synthetic data only; keys and certificates generated at run time)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fhir_proxy.share.testing import ShareEnv, build_share_env, make_certificate, make_key
from fhir_proxy.testing import LogCapture


@pytest.fixture(scope="session")
def client_key():
    return make_key()


@pytest.fixture(scope="session")
def server_cert():
    """A certificate for 127.0.0.1 (also used, unverified, by the in-process transport)."""
    return make_certificate(["127.0.0.1"])


@pytest.fixture
def env(tmp_path: Path, client_key, server_cert) -> ShareEnv:
    return build_share_env(tmp_path, client_key, server_cert)


@pytest.fixture
def logs():
    handler = LogCapture()
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)
