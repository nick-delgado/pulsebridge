"""Test helpers, like ``pulsebridge_gcp.testing``. No network, no credentials, no GCP.

The FHIR store is the real :class:`pulsebridge_gcp.HealthcareClient` over :class:`StoreFake`, a
``FakeTransport`` that answers ``executeBundle`` with one ``201 Created`` per entry of the
Bundle actually sent. Every value is synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pulsebridge_gcp import HealthcareClient, HttpResponse
from pulsebridge_gcp.testing import FakeTransport, RecordedRequest, json_response

from pulsebridge_seed.config import FhirTarget, SeedConfig
from pulsebridge_seed.seeder import Dependencies

PATIENT_ID = "SYN-TEST-0042"  # distinctive, so a leak into a log line would be found
STORE = "health"
NOW = datetime(2026, 3, 10, 12, 2, 30, tzinfo=UTC)
ENV = {
    "PATIENT_ID": PATIENT_ID,
    "TIME_ZONE": "America/New_York",
    "GCP_PROJECT_ID": "demo-project",
    "HEALTHCARE_LOCATION": "us-central1",
    "HEALTHCARE_DATASET_ID": "phr",
    "FHIR_STORE_ID": STORE,
}


class StoreFake(FakeTransport):
    """``POST …/fhir`` (executeBundle) answers from the Bundle sent, unless ``bundle_errors``
    holds a response or exception for the next call (``None``: answer normally). Other routes
    are scripted with ``add``."""

    def __init__(self) -> None:
        super().__init__()
        self.bundle_errors: list[HttpResponse | BaseException | None] = []

    def request(self, method, url, *, params, body, headers, timeout):
        if not (method == "POST" and url.endswith("/fhir")):
            return super().request(
                method, url, params=params, body=body, headers=headers, timeout=timeout
            )
        self.requests.append(RecordedRequest(method, url, {}, [], body, dict(headers), timeout))
        error = self.bundle_errors.pop(0) if self.bundle_errors else None
        if isinstance(error, BaseException):
            raise error
        if error is not None:
            return error
        sent = json.loads(body)
        return json_response(
            200,
            {
                "resourceType": "Bundle",
                "type": "transaction-response",
                "entry": [{"response": {"status": "201 Created"}} for _ in sent["entry"]],
            },
        )

    def bundles(self) -> list[dict]:
        return [r.json() for r in self.calls("POST", "/fhir")]


def patient_search(*patients: dict) -> HttpResponse:
    return json_response(
        200,
        {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": patient} for patient in patients],
        },
    )


def fresh_store() -> StoreFake:
    store = StoreFake()
    store.add("GET", "/fhir/Patient", patient_search())
    return store


def make_config(**overrides) -> SeedConfig:
    values: dict = {
        "patient_id": PATIENT_ID,
        "time_zone": ZoneInfo("America/New_York"),
        "fhir": FhirTarget("demo-project", "us-central1", "phr", STORE),
        "seed": 7,
    }
    values.update(overrides)
    return SeedConfig(**values)


def deps_for(
    transport: FakeTransport, now: datetime = NOW, sleeps: list | None = None
) -> Dependencies:
    client = HealthcareClient(
        project="demo-project", location="us-central1", dataset="phr", transport=transport
    )
    return Dependencies(
        fhir=client,
        now=lambda: now,
        sleep=sleeps.append if sleeps is not None else (lambda _seconds: None),
    )
