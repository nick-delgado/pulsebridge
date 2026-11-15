"""Test helpers for health-sync, like ``pulsebridge_gcp.testing``. All synthetic, no network.

- Google Health and the OAuth token endpoint are scripted with ``pulsebridge_gcp.testing``'s
  ``FakeTransport`` (routes by path suffix: ``/token``, ``:rollUp``, ``/dataPoints``).
- The FHIR store is the real ``HealthcareClient`` over a second ``FakeTransport``, so the
  library's error classification is exercised end to end.
- Credentials are built at run time from obviously fake pieces (so no literal in this file looks
  like a credential to a secret scanner) and written to files in ``tmp_path``, the way Cloud Run
  mounts them.
- Roll-up windows use the shape spike S3 recorded, with invented values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pulsebridge_gcp import HealthcareClient
from pulsebridge_gcp.testing import FakeTransport, json_response
from pulsebridge_gcp.transport import HttpResponse

from health_sync.config import DEFAULT_SCOPE, SecretPaths, ServiceConfig
from health_sync.logs import JsonFormatter
from health_sync.sync import SyncDependencies

FHIR_PATH = "/fhirStores/fhir/fhir"
PATIENT_ID = "PB-SYN-0001"


def fake(*parts: str) -> str:
    """A synthetic credential-shaped string, assembled at run time."""
    return "-".join(("synthetic",) + parts)


CLIENT_ID = fake("client", "id")
CLIENT_SECRET = fake("client", "secret", "value")
REFRESH_TOKEN = fake("refresh", "grant", "value")
ACCESS_TOKEN = fake("access", "one")
ACCESS_TOKEN_2 = fake("access", "two")


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def window(start: str, bpm_min: float, bpm_avg: float, bpm_max: float) -> dict:
    """One roll-up item exactly as spike S3 recorded the shape (values invented)."""
    begin = utc(start)
    return {
        "startTime": start,
        "endTime": (begin + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "heartRate": {
            "beatsPerMinuteMin": bpm_min,
            "beatsPerMinuteAvg": bpm_avg,
            "beatsPerMinuteMax": bpm_max,
        },
    }


def rollup_page(items: list[dict], next_token: str | None = None) -> HttpResponse:
    body: dict = {"rollupDataPoints": items}
    if next_token is not None:
        body["nextPageToken"] = next_token
    return json_response(200, body)


def token_response(
    value: str = ACCESS_TOKEN, *, expires_in: int = 3599, scope: str = DEFAULT_SCOPE
) -> HttpResponse:
    return json_response(
        200,
        {"access_token": value, "expires_in": expires_in, "scope": scope, "token_type": "Bearer"},
    )


def oauth_error(code: str, status: int = 400) -> HttpResponse:
    return json_response(status, {"error": code, "error_description": "synthetic description"})


def google_error(status: int, reason: str | None = None) -> HttpResponse:
    error: dict = {"code": status, "message": "synthetic", "status": "SYNTHETIC_STATUS"}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return json_response(status, {"error": error})


def list_response(
    display_name: str | None = "Synthetic Watch 1",
    platform: str | None = "FITBIT",
    method: str | None = "PASSIVELY_MEASURED",
) -> HttpResponse:
    source: dict = {}
    if platform is not None:
        source["platform"] = platform
    if method is not None:
        source["recordingMethod"] = method
    if display_name is not None:
        source["device"] = {"displayName": display_name}
    point = {
        "dataSource": source,
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-01-15T14:59:58Z"},
            "beatsPerMinute": "77",
        },
    }
    return json_response(200, {"dataPoints": [point]})


def fhir_ok(bundle_entries: int = 0) -> HttpResponse:
    return json_response(
        200,
        {
            "resourceType": "Bundle",
            "type": "transaction-response",
            "entry": [{"response": {"status": "200 OK"}}] * bundle_entries,
        },
    )


@dataclass
class Clock:
    """Wall time stays put (``now``); monotonic time moves only when the code sleeps or a test
    advances it. ``sleeps`` records every backoff delay."""

    now: datetime = field(default_factory=lambda: utc("2026-01-15T15:07:30Z"))
    mono: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += seconds


@dataclass
class World:
    """One test's fakes: Google, the FHIR store, the clock, the secret files, the config."""

    tmp: Path
    google: FakeTransport = field(default_factory=FakeTransport)
    fhir_transport: FakeTransport = field(default_factory=FakeTransport)
    clock: Clock = field(default_factory=Clock)

    def __post_init__(self) -> None:
        self.secret_dir = self.tmp / "secrets"
        self.secret_dir.mkdir()
        self.paths = SecretPaths(
            client_id=str(self.secret_dir / "client-id"),
            client_secret=str(self.secret_dir / "client-secret"),
            refresh_token=str(self.secret_dir / "refresh-token"),
        )
        self.write_secrets()

    def write_secrets(self, **overrides: str | None) -> None:
        values = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            **overrides,
        }
        for name, value in values.items():
            path = Path(getattr(self.paths, name))
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(value + "\n")

    def config(self, **overrides: object) -> ServiceConfig:
        values: dict = {
            "project_id": "demo-project",
            "location": "us-central1",
            "dataset_id": "demo",
            "fhir_store_id": "fhir",
            "patient_id": PATIENT_ID,
            "time_zone": ZoneInfo("America/New_York"),
            "secrets": self.paths,
            "status_cache_seconds": 0.0,
        }
        values.update(overrides)
        return ServiceConfig(**values)

    def deps(self) -> SyncDependencies:
        client = HealthcareClient(
            project="demo-project",
            location="us-central1",
            dataset="demo",
            transport=self.fhir_transport,
        )
        return SyncDependencies(
            google_transport=self.google,
            fhir=client,
            now=lambda: self.clock.now,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    # ---- what was sent ----

    def bundles(self) -> list[dict]:
        return [r.json() for r in self.fhir_transport.calls("POST", FHIR_PATH)]

    def rollup_bodies(self) -> list[dict]:
        return [r.json() for r in self.google.calls("POST", ":rollUp")]

    def token_calls(self) -> int:
        return len(self.google.calls("POST", "/token"))


def observations(bundle: dict) -> list[dict]:
    return [
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"
    ]


def observation_keys(bundles: list[dict]) -> list[str]:
    return [o["identifier"][0]["value"] for b in bundles for o in observations(b)]


def formatted_logs(records: list[logging.LogRecord]) -> str:
    """Every captured record as the JSON line production would print."""
    formatter = JsonFormatter()
    return "\n".join(formatter.format(r) for r in records)


def leaked(text: str, *extra: str) -> list[str]:
    """Which synthetic secrets (and ``extra`` values) appear in ``text``; tests assert ``[]``."""
    values = (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, ACCESS_TOKEN, ACCESS_TOKEN_2, *extra)
    return [value for value in values if value in text]
