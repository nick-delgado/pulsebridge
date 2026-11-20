"""``GET /fhir/Observation``: strict validation, the patient pin, the date rule, paging."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

import pytest
from fhir_proxy.testing import FHIR_STORE

HR = "code=http://loinc.org|8867-4"
BP = "code=http://loinc.org|85354-9"
DAY_START = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)  # midnight in New York


def _reason(response) -> str:  # noqa: ANN001
    return response.get_json()["issue"][0]["details"]["coding"][0]["code"]


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("", "missing-parameter"),
        ("date=gt2026-01-01T00:00:00Z", "missing-parameter"),
        (f"{HR}&patient=Patient/other", "unsupported-parameter"),
        (f"{HR}&subject=Patient/pat-1", "unsupported-parameter"),
        (f"{HR}&_include=Observation:device", "unsupported-parameter"),
        (f"{HR}&_revinclude=Provenance:target", "unsupported-parameter"),
        (f"{HR}&_elements=id", "unsupported-parameter"),
        (f"{HR}&_format=xml", "unsupported-parameter"),
        (f"{HR}&status=final", "unsupported-parameter"),
        (f"{HR}&subject.name=x", "unsupported-parameter"),
        (f"{HR}&subject:Patient.identifier=x", "unsupported-parameter"),
        ("code:text=heart", "unsupported-parameter"),
        ("code:not=http://loinc.org|8867-4", "unsupported-parameter"),
        ("code=8867-4", "unsupported-value"),
        ("code=http://snomed.info/sct|364075005", "unsupported-value"),
        ("code=http://loinc.org|8480-6", "unsupported-value"),
        ("code=http://loinc.org|8867-4,http://loinc.org|85354-9", "unsupported-value"),
        (f"{HR}&{HR}", "repeated-parameter"),
        (f"{HR}&date=2026-01-01T00:00:00Z", "unsupported-value"),
        (f"{HR}&date=eq2026-01-01T00:00:00Z", "unsupported-value"),
        (f"{HR}&date=sa2026-01-01T00:00:00Z", "unsupported-value"),
        (f"{HR}&date=gt2026-01-01", "unsupported-value"),
        (f"{HR}&date=gt2026-01-01T00:00:00", "unsupported-value"),
        (f"{HR}&date=gt2026-01-01T00:00:00+05:00", "unsupported-value"),  # + decoded as space
        (f"{HR}&date=gt2026-13-01T00:00:00Z", "unsupported-value"),
        (f"{HR}&date=gt2026-01-01T00:00:00Z&date=ge2026-01-01T00:00:00Z", "unsupported-value"),
        (f"{HR}&date=lt2026-01-01T00:00:00Z&date=le2026-01-02T00:00:00Z", "unsupported-value"),
        (f"{HR}&date=gt2026-01-02T00:00:00Z&date=lt2026-01-01T00:00:00Z", "unsupported-value"),
        (f"{HR}&_sort=-_lastUpdated", "unsupported-value"),
        (f"{HR}&_count=0", "unsupported-value"),
        (f"{HR}&_count=1001", "unsupported-value"),
        (f"{HR}&_count=1e3", "unsupported-value"),
        (f"{HR}&_page_token=abc%20def", "unsupported-value"),
        ("&".join([HR] + [f"date=gt2026-01-0{d}T00:00:00Z" for d in range(1, 9)] * 2), None),
    ],
)
def test_unsupported_searches_are_refused_without_reaching_the_store(
    harness, patient, query: str, reason: str | None
) -> None:
    response = harness.get(f"/fhir/Observation?{query}")
    assert response.status_code == 400
    body = response.get_json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] in ("not-supported", "invalid")
    if reason is not None:
        assert _reason(response) == reason
    assert harness.fake.calls("GET", "/Observation") == []


def test_a_hostile_parameter_name_is_not_echoed(harness, patient) -> None:
    response = harness.get(f"/fhir/Observation?{HR}&%3Cscript%3Ealert(1)%3C%2Fscript%3E=1")
    assert response.status_code == 400
    assert "script" not in response.get_data(as_text=True)


def test_search_is_pinned_to_the_configured_patient(harness, patient, seed_heart_rate) -> None:
    seed_heart_rate(DAY_START, 3)
    # Another patient's Observation in the same store is never returned.
    harness.fake.put(
        FHIR_STORE,
        {
            "resourceType": "Observation",
            "id": "someone-else",
            "subject": {"reference": "Patient/other"},
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
            "effectivePeriod": {"start": "2026-01-01T05:00:00Z", "end": "2026-01-01T05:05:00Z"},
        },
    )
    response = harness.get(f"/fhir/Observation?{HR}")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/fhir+json"
    bundle = response.get_json()
    assert bundle["type"] == "searchset"
    ids = [e["resource"]["id"] for e in bundle["entry"]]
    assert len(ids) == 3 and "someone-else" not in ids
    (store_call,) = harness.fake.calls("GET", "/fhirStores/main/fhir/Observation$")
    assert ("subject", patient) in store_call[2]
    # Nothing in the answer names the store.
    assert "healthcare.googleapis.com" not in response.get_data(as_text=True)
    assert "example-project" not in response.get_data(as_text=True)


def test_no_patient_yet_means_no_results_and_no_observation_search(harness) -> None:
    response = harness.get(f"/fhir/Observation?{HR}")
    assert response.status_code == 200
    assert response.get_json()["entry"] == []
    assert harness.fake.calls("GET", "/Observation") == []


def test_aligned_24_hours_returns_288_windows(harness, seed_heart_rate) -> None:
    # 290 windows from 00:00 local: the 24 hours (start, end) overlaps exactly 288 of them.
    seed_heart_rate(DAY_START - timedelta(minutes=5), 290)
    start = DAY_START.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (DAY_START + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = harness.get(f"/fhir/Observation?{HR}&date=gt{start}&date=lt{end}&_count=1000")
    windows = [e["resource"]["effectivePeriod"] for e in response.get_json()["entry"]]
    assert len(windows) == 288
    starts = sorted(datetime.fromisoformat(w["start"]) for w in windows)
    assert starts[0] == DAY_START  # the window ending exactly at start is not returned
    assert starts[-1] == DAY_START + timedelta(days=1) - timedelta(minutes=5)


def test_ge_and_le_include_the_boundary_window(harness, seed_heart_rate) -> None:
    seed_heart_rate(DAY_START - timedelta(minutes=5), 290)
    start = DAY_START.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (DAY_START + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = harness.get(f"/fhir/Observation?{HR}&date=ge{start}&date=le{end}&_count=1000")
    assert len(response.get_json()["entry"]) == 290


def test_paging_links_point_at_the_proxy(harness, seed_heart_rate) -> None:
    seed_heart_rate(DAY_START, 30)
    harness.fake.page_size_override = 10
    first = harness.get(f"/fhir/Observation?{HR}&_count=10").get_json()
    links = {link["relation"]: link["url"] for link in first["link"]}
    assert links["self"].startswith("/fhir/Observation?")
    nxt = links["next"]
    assert nxt.startswith("/fhir/Observation?") and "healthcare.googleapis.com" not in nxt
    query = dict(parse_qsl(urlsplit(nxt).query))
    assert query["code"] == "http://loinc.org|8867-4" and query["_page_token"] == "10"  # noqa: S105
    seen = [e["resource"]["id"] for e in first["entry"]]
    while "next" in links:
        page = harness.get(links["next"]).get_json()
        seen += [e["resource"]["id"] for e in page["entry"]]
        links = {link["relation"]: link["url"] for link in page["link"]}
    assert len(seen) == len(set(seen)) == 30
    # The store saw our pinned query plus its own page token, never anything else; the date is
    # the shown window's boundary (T47), on every page, the same one the links carry.
    boundary = "ge" + "2025-12-26T12:00:00.000Z"  # the harness clock minus HISTORY_DAYS (7)
    assert query["date"] == boundary
    calls = harness.fake.calls("GET", "/Observation$")
    assert len(calls) == 3
    for call in calls:
        assert {name for name, _ in call[2]} <= {
            "subject",
            "code",
            "date",
            "_sort",
            "_count",
            "_page_token",
        }
        assert [v for name, v in call[2] if name == "date"] == [boundary]


def test_paging_link_keeps_the_prefix(tmp_path) -> None:
    from fhir_proxy.testing import build_harness

    h = build_harness(tmp_path, path_prefix="/api")
    h.fake.put(
        FHIR_STORE,
        {
            "resourceType": "Patient",
            "id": "pat-1",
            "identifier": [{"system": "urn:pulsebridge:patient", "value": "PB-SYN-0001"}],
        },
    )
    for n in range(3):
        h.fake.put(
            FHIR_STORE,
            {
                "resourceType": "Observation",
                "id": f"o{n}",
                "subject": {"reference": "Patient/pat-1"},
                "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
                "effectiveDateTime": f"2026-01-01T1{n}:00:00Z",
            },
        )
    h.fake.page_size_override = 1
    auth = h.auth()
    page = h.client.get(f"/api/fhir/Observation?{BP}&_count=1", headers=auth).get_json()
    nxt = next(link["url"] for link in page["link"] if link["relation"] == "next")
    assert nxt.startswith("/api/fhir/Observation?")


def test_store_failures_are_bounded_reasons(harness, patient) -> None:
    from pulsebridge_gcp import HttpResponse, TimedOutError

    harness.fake.fail("GET", "/Observation$", HttpResponse(403, b'{"error": {"message": "x"}}'))
    response = harness.get(f"/fhir/Observation?{HR}")
    assert response.status_code == 502 and _reason(response) == "upstream-permission-denied"
    harness.fake.failures.clear()
    harness.fake.fail("GET", "/Observation$", TimedOutError("GET x", None))
    response = harness.get(f"/fhir/Observation?{HR}")
    assert response.status_code == 504 and _reason(response) == "upstream-timeout"
    harness.fake.failures.clear()
    harness.fake.fail(
        "GET",
        "/Observation$",
        HttpResponse(503, b'{"error": {"message": "projects/example-project secret detail"}}'),
    )
    response = harness.get(f"/fhir/Observation?{HR}")
    assert response.status_code == 503 and _reason(response) == "upstream-unavailable"
    assert "example-project" not in response.get_data(as_text=True)
    harness.fake.failures.clear()
    harness.fake.fail(
        "GET",
        "/Observation$",
        HttpResponse(400, b'{"error": {"message": "projects/example-project/datasets/x refused"}}'),
    )
    response = harness.get(f"/fhir/Observation?{HR}")
    assert response.status_code == 502 and _reason(response) == "upstream-refused"
    assert "example-project" not in response.get_data(as_text=True)


def test_search_results_have_a_full_url_at_the_proxy(harness, seed_heart_rate) -> None:
    seed_heart_rate(DAY_START, 2)
    bundle = harness.get(f"/fhir/Observation?{HR}").get_json()
    for entry in bundle["entry"]:
        assert entry["fullUrl"] == (
            f"https://proxy.example.test/fhir/Observation/{entry['resource']['id']}"
        )
        # The fullUrl resolves: the read route answers it.
        path = entry["fullUrl"].removeprefix("https://proxy.example.test")
        assert harness.get(path).get_json()["id"] == entry["resource"]["id"]


def test_read_route_is_pinned_to_the_patient(harness, patient) -> None:  # noqa: ARG001
    harness.fake.put(
        FHIR_STORE,
        {
            "resourceType": "Observation",
            "id": "other",
            "subject": {"reference": "Patient/someone-else"},
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
        },
    )
    assert harness.get("/fhir/Observation/other").status_code == 404
    assert harness.get("/fhir/Observation/missing").status_code == 404


def test_the_proxy_filters_even_if_the_store_ignores_the_pin(
    harness, patient, seed_heart_rate
) -> None:  # noqa: ARG001
    seed_heart_rate(DAY_START, 2)
    harness.fake.put(
        FHIR_STORE,
        {
            "resourceType": "Observation",
            "id": "leak",
            "subject": {"reference": "Patient/someone-else"},
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
            "effectivePeriod": {"start": "2026-01-01T05:00:00Z", "end": "2026-01-01T05:05:00Z"},
        },
    )
    harness.fake.ignore_subject = True
    ids = [e["resource"]["id"] for e in harness.get(f"/fhir/Observation?{HR}").get_json()["entry"]]
    assert "leak" not in ids and len(ids) == 2


@pytest.mark.parametrize("query", ["_format=xml", "_elements=id", "_summary=true", "anything=1"])
def test_read_route_refuses_parameters(harness, seed_heart_rate, query: str) -> None:
    seed_heart_rate(DAY_START, 1)
    obs_id = next(rid for (t, rid) in harness.fake.fhir[FHIR_STORE] if t == "Observation")
    response = harness.get(f"/fhir/Observation/{obs_id}?{query}")
    assert response.status_code == 400
    assert response.get_json()["resourceType"] == "OperationOutcome"


def test_read_route_sends_etag_and_last_modified(harness, seed_heart_rate) -> None:
    seed_heart_rate(DAY_START, 1)
    obs_id = next(rid for (t, rid) in harness.fake.fhir[FHIR_STORE] if t == "Observation")
    harness.fake.fhir[FHIR_STORE][("Observation", obs_id)]["meta"]["versionId"] = "MTY4"
    stored = harness.fake.fhir[FHIR_STORE][("Observation", obs_id)]["meta"]["lastUpdated"]
    response = harness.get(f"/fhir/Observation/{obs_id}")
    assert response.headers["ETag"] == 'W/"MTY4"'
    from email.utils import parsedate_to_datetime

    assert parsedate_to_datetime(response.headers["Last-Modified"]) == datetime.fromisoformat(
        stored
    ).replace(microsecond=0)
