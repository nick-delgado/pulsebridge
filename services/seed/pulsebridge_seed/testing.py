"""Test helpers, like ``pulsebridge_gcp.testing``. No network, no credentials, no GCP.

The FHIR store is the real :class:`pulsebridge_gcp.HealthcareClient` over :class:`StoreFake`, a
``FakeTransport`` that keeps a tiny in-memory store:

- ``executeBundle`` applies each entry (Patient and Device ``PUT`` or conditional create,
  Observation conditional update by identifier, Provenance ``PUT`` by id) and answers ``201`` or
  ``200`` per entry;
- ``GET Patient`` and ``GET Observation`` searches answer from what was stored, with ``total``,
  ``_count`` paging and ``next`` links inside the store.

A route scripted with ``add`` takes precedence, so a test can make a search fail or answer
anything. ``bundle_errors`` scripts the next ``executeBundle`` answers (``None``: apply it).
Every value is synthetic.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit
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
OBSERVATION_SYSTEM = "urn:pulsebridge:observation"


def _overlaps(resource: dict, start: datetime | None, end: datetime | None) -> bool:
    period = resource.get("effectivePeriod") or {}
    lo = datetime.fromisoformat(period["start"])
    hi = datetime.fromisoformat(period["end"])
    return (start is None or hi >= start) and (end is None or lo < end)


class StoreFake(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.bundle_errors: list[HttpResponse | BaseException | None] = []
        self.patients: list[dict] = []
        self.observations: dict[str, dict] = {}  # by identifier value
        self.provenances: dict[str, dict] = {}
        self.page_size_cap: int | None = None  # force small pages to exercise next links

    # -- routing --------------------------------------------------------------------------

    def _scripted(self, method: str, url: str) -> bool:
        path = urlsplit(url).path
        return any(r.method == method and path.endswith(r.suffix) for r in self._routes)

    def request(self, method, url, *, params, body, headers, timeout):
        if self._scripted(method, url):
            return super().request(
                method, url, params=params, body=body, headers=headers, timeout=timeout
            )
        pairs = list(params.items()) if isinstance(params, dict) else list(params or [])
        query = parse_qsl(urlsplit(url).query)
        recorded = RecordedRequest(
            method, url, dict(pairs or query), pairs or query, body, dict(headers), timeout
        )
        self.requests.append(recorded)
        path = urlsplit(url).path
        if method == "POST" and path.endswith("/fhir"):
            return self._execute(json.loads(body))
        if method == "GET" and path.endswith("/fhir/Patient"):
            return self._searchset([copy.deepcopy(p) for p in self.patients])
        if method == "GET" and path.endswith("/fhir/Observation"):
            return self._observation_search(url, pairs or query)
        raise AssertionError(f"StoreFake: no route for {method} {path}")

    # -- executeBundle --------------------------------------------------------------------

    def _execute(self, bundle: dict) -> HttpResponse:
        error = self.bundle_errors.pop(0) if self.bundle_errors else None
        if isinstance(error, BaseException):
            raise error
        if error is not None:
            return error
        answers = []
        for entry in bundle["entry"]:
            resource = copy.deepcopy(entry["resource"])
            request = entry["request"]
            kind = resource["resourceType"]
            created = False
            if kind == "Patient":
                if request["method"] == "PUT" and self.patients:
                    resource["id"] = self.patients[0]["id"]
                    self.patients[0] = resource
                elif not self.patients:
                    resource["id"] = "pat-1"
                    self.patients.append(resource)
                    created = True
            elif kind == "Observation":
                key = next(
                    i["value"] for i in resource["identifier"] if i["system"] == OBSERVATION_SYSTEM
                )
                created = key not in self.observations
                self.observations[key] = resource
            elif kind == "Provenance":
                created = resource["id"] not in self.provenances
                self.provenances[resource["id"]] = resource
            answers.append({"response": {"status": "201 Created" if created else "200 OK"}})
        return json_response(
            200, {"resourceType": "Bundle", "type": "transaction-response", "entry": answers}
        )

    # -- searches -------------------------------------------------------------------------

    def _searchset(self, resources: list[dict], *, total: int | None = None, link=None):
        body = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(resources) if total is None else total,
            "entry": [{"resource": r, "search": {"mode": "match"}} for r in resources],
        }
        if link:
            body["link"] = [{"relation": "next", "url": link}]
        return json_response(200, body)

    def _observation_search(self, url: str, pairs: list[tuple[str, str]]) -> HttpResponse:
        start = end = None
        for name, value in pairs:
            if name == "date" and value.startswith("ge"):
                start = datetime.fromisoformat(value[2:].replace("Z", "+00:00"))
            if name == "date" and value.startswith("lt"):
                end = datetime.fromisoformat(value[2:].replace("Z", "+00:00"))
        matches = [copy.deepcopy(r) for r in self.observations.values() if _overlaps(r, start, end)]
        params = dict(pairs)
        count = int(params.get("_count", "1000"))
        if self.page_size_cap:
            count = min(count, self.page_size_cap)
        offset = int(params.get("_offset", "0"))
        page = matches[offset : offset + count]
        link = None
        if offset + count < len(matches):
            base = url.split("?", 1)[0]
            following = [(k, v) for k, v in pairs if k != "_offset"]
            link = f"{base}?{urlencode(following + [('_offset', str(offset + count))])}"
        return self._searchset(page, total=len(matches), link=link)

    def bundles(self) -> list[dict]:
        return [r.json() for r in self.requests if r.method == "POST" and r.url.endswith("/fhir")]


def patient_search(*patients: dict, total: int | None = None) -> HttpResponse:
    body = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": patient} for patient in patients],
    }
    if total is not None:
        body["total"] = total
    return json_response(200, body)


def fresh_store() -> StoreFake:
    return StoreFake()


def store_holding(*patients: dict) -> StoreFake:
    store = StoreFake()
    store.patients = [copy.deepcopy(p) for p in patients]
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
