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


@pytest.fixture
def seed_heart_rate(harness: Harness, patient: str):
    """Write synthetic five-minute windows through T06's real mapper; returns the Bundle ids."""
    from datetime import UTC, timedelta
    from zoneinfo import ZoneInfo

    from fhir_proxy.testing import apply_transaction
    from pulsebridge_fhir import DeviceInput, GoogleDataSource, PatientInput, heart_rate_transaction

    def seed(start, count: int, *, zone: str = "America/New_York", prefix: str = "hr"):  # noqa: ANN001, ANN202
        items = []
        for n in range(count):
            begin = start + timedelta(minutes=5 * n)
            items.append(
                {
                    "startTime": begin.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endTime": (begin + timedelta(minutes=5))
                    .astimezone(UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "heartRate": {
                        "beatsPerMinuteMin": 55 + n % 7,
                        "beatsPerMinuteAvg": 63.5 + n % 7,
                        "beatsPerMinuteMax": 71 + n % 7,
                    },
                }
            )
        bundle = heart_rate_transaction(
            items,
            patient=PatientInput(PATIENT_ID),
            device=DeviceInput("Synthetic Watch"),
            data_source=GoogleDataSource(platform=None, recording_method=None),
            tz=ZoneInfo(zone),
            recorded=harness.clock.now.replace(microsecond=0),
            complete_before=None,
        )
        return apply_transaction(harness.fake, bundle, prefix=prefix)

    return seed


@pytest.fixture
def map_into_store(harness: Harness, patient: str):
    """Run v2-to-fhir's real mapper (T12) over stored bytes, write its Bundle into the fake
    store and label the message as the mapper does; returns the labels."""
    from datetime import UTC, datetime

    from fhir_proxy.testing import apply_transaction
    from v2_to_fhir.mapping import MappingConfig, map_message

    def run(name: str, data: bytes, prefix: str = "bp") -> dict:
        result = map_message(
            data,
            message_name=name,
            config=MappingConfig(PATIENT_ID),
            recorded=datetime(2026, 1, 1, 14, 31, tzinfo=UTC),
        )
        labels = {"application_outcome": result.outcome.value, "reason": result.reason.value}
        if result.bundle is not None:
            apply_transaction(harness.fake, result.bundle, prefix=prefix)
        return labels

    return run
