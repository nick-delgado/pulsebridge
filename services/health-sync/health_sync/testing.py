"""Test helpers for health-sync, like ``pulsebridge_gcp.testing``. All synthetic, no network.

- Google Health and the OAuth token endpoint are scripted with ``pulsebridge_gcp.testing``'s
  ``FakeTransport`` (routes by path suffix: ``/token``, ``:rollUp``, ``/dataPoints``).
- The FHIR store is the real ``HealthcareClient`` over :class:`FhirFake` (a ``FakeTransport``
  that also answers the Patient and Observation searches from an in-memory store), so the
  library's error classification is exercised end to end.
- Credentials are built at run time from obviously fake pieces (so no literal in this file looks
  like a credential to a secret scanner) and written to files in ``tmp_path``, the way Cloud Run
  mounts them.
- Roll-up windows use the shape spike S3 recorded, with invented values.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pulsebridge_gcp import HealthcareClient, TimedOutError
from pulsebridge_gcp.testing import FakeTransport, RecordedRequest, json_response
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
class FhirFake(FakeTransport):
    """The FHIR store. ``POST`` of a Bundle goes to the scripted routes, as with
    ``FakeTransport``; the two searches health-sync makes are answered from this state:

    - ``patient_server_id``: the Patient the setup step wrote (``None``: no Patient yet);
    - ``stored``: Observation resources the store holds, returned by every Observation search;
    - ``stranded``: the ``total`` of the ``_summary=count`` search;
    - ``read_failure``: if set, every search answers with it;
    - ``auto_store``: copy each successfully written Bundle's Observations into ``stored``
      (replacing by identifier) and give the Patient an id, like a real store would.
    """

    patient_server_id: str | None = None
    stored: list[dict] = field(default_factory=list)
    stranded: int = 0
    read_failure: HttpResponse | None = None
    auto_store: bool = False
    page_size: int | None = None  # page the Observation search, with a ``next`` link
    observation_failure: HttpResponse | None = None  # only the stored-windows search fails
    count_failure: HttpResponse | None = None  # only the stranded count fails
    duplicate_patient: bool = False  # the identifier matches two Patients

    def request(self, method, url, *, params, body, headers, timeout):  # noqa: ANN001
        path = urlsplit(url).path
        if method == "GET" and path.endswith(("/fhir/Patient", "/fhir/Observation")):
            pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
            self.requests.append(
                RecordedRequest(method, url, dict(pairs), pairs, body, dict(headers), timeout)
            )
            if self.read_failure is not None:
                return self.read_failure
            if path.endswith("/Patient"):
                ids = [self.patient_server_id] if self.patient_server_id else []
                if self.duplicate_patient:
                    ids = ids + ["p-duplicate"]
                entries = [{"resource": {"resourceType": "Patient", "id": i}} for i in ids]
                return json_response(200, _searchset(entries))
            if self.observation_failure is not None and "_summary" not in dict(pairs):
                return self.observation_failure
            if self.count_failure is not None and dict(pairs).get("_summary") == "count":
                return self.count_failure
            if dict(pairs).get("_summary") == "count":
                return json_response(
                    200, {"resourceType": "Bundle", "type": "searchset", "total": self.stranded}
                )
            entries = [{"resource": r} for r in self.stored]
            if self.page_size is None:
                return json_response(200, _searchset(entries))
            page = int(urlsplit(url).query.removeprefix("page=") or 1)
            chunk = entries[(page - 1) * self.page_size : page * self.page_size]
            body = _searchset(chunk)
            if page * self.page_size < len(entries):
                base = url.split("?", 1)[0]
                body["link"] = [{"relation": "next", "url": f"{base}?page={page + 1}"}]
            return json_response(200, body)
        response = super().request(
            method, url, params=params, body=body, headers=headers, timeout=timeout
        )
        if self.auto_store and method == "POST" and 200 <= response.status < 300 and body:
            self._absorb(json.loads(body.decode("utf-8")))
        return response

    def _absorb(self, bundle: dict) -> None:
        if self.patient_server_id is None:
            self.patient_server_id = "p-1"
        for entry in bundle.get("entry", []):
            resource = entry["resource"]
            if resource["resourceType"] != "Observation":
                continue
            key = resource["identifier"][0]["value"]
            self.stored = [r for r in self.stored if r["identifier"][0]["value"] != key]
            self.stored.append(resource)


def _searchset(entries: Sequence[dict]) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(entries),
        "entry": list(entries),
    }


@dataclass
class SlowTransport:
    """Wraps a transport; every request takes ``latency`` seconds of the test clock. A request
    whose timeout is shorter waits the timeout and then fails as a timeout would (no response:
    ``TimedOutError``, as pulsebridge_gcp's RequestsTransport raises for a timeout), so tests can
    check that a run's wall time stays inside its deadline."""

    inner: object
    clock: Clock
    latency: float
    timeouts: list[float] = field(default_factory=list)

    def request(self, method, url, *, params, body, headers, timeout):  # noqa: ANN001
        self.timeouts.append(timeout)
        if self.latency > timeout:
            self.clock.mono += timeout
            raise TimedOutError(f"{method} {url}", None, server_message="ReadTimeout")
        self.clock.mono += self.latency
        return self.inner.request(  # type: ignore[attr-defined]
            method, url, params=params, body=body, headers=headers, timeout=timeout
        )


@dataclass
class World:
    """One test's fakes: Google, the FHIR store, the clock, the secret files, the config."""

    tmp: Path
    google: FakeTransport = field(default_factory=FakeTransport)
    fhir_transport: FhirFake = field(default_factory=FhirFake)
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

    def deps(
        self, *, fhir_transport: object | None = None, google_transport: object | None = None
    ) -> SyncDependencies:
        transport = fhir_transport or self.fhir_transport

        def fhir(timeout: float) -> HealthcareClient:
            return HealthcareClient(
                project="demo-project",
                location="us-central1",
                dataset="demo",
                transport=transport,
                timeout=timeout,  # type: ignore[arg-type]
            )

        return SyncDependencies(
            google_transport=google_transport or self.google,  # type: ignore[arg-type]
            fhir=fhir,
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
