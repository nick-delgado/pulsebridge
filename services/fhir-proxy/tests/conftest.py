"""pytest fixtures. The fakes themselves are in ``fhir_proxy.testing`` (synthetic data only)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fhir_proxy.testing import FHIR_STORE, PATIENT_ID, Harness, LogCapture, build_harness


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return build_harness(tmp_path)


@pytest.fixture
def patient(harness: Harness) -> str:
    """The setup step's Patient in the fake store; returns its reference."""
    harness.fake.put(
        FHIR_STORE,
        {
            "resourceType": "Patient",
            "id": "pat-1",
            "identifier": [{"system": "urn:pulsebridge:patient", "value": PATIENT_ID}],
        },
    )
    return "Patient/pat-1"


@pytest.fixture
def logs():
    handler = LogCapture()
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)
