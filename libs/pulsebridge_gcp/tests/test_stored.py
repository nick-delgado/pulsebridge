"""pulsebridge_gcp.stored: the stored-window read shared by health-sync and the seeder (T35).

The search goes through the real HealthcareClient over the library's FakeTransport, so the
request path, the query and the `next` links are the client's own. All data is synthetic.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from pulsebridge_fhir.stored import StoredReadError, StoredWindow
from pulsebridge_gcp import HealthcareClient, RetryableError
from pulsebridge_gcp.stored import MAX_SEARCH_PAGES, max_pages_for_range, stored_windows
from pulsebridge_gcp.testing import FakeTransport, json_response

START = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
END = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
SEARCH = "/fhirStores/hs/fhir/Observation"


def make_client(transport: FakeTransport) -> HealthcareClient:
    return HealthcareClient(project="p", location="l", dataset="d", transport=transport)


def observation(key: str, status: str = "final") -> dict:
    return {
        "resourceType": "Observation",
        "identifier": [{"system": "urn:pulsebridge:observation", "value": key}],
        "status": status,
        "effectivePeriod": {"end": "2026-01-15T12:05:00Z"},
        "valueQuantity": {"value": 64.5},
    }


def page(*resources: dict, next_page: int | None = None) -> dict:
    bundle = {"resourceType": "Bundle", "type": "searchset"}
    bundle["entry"] = [{"resource": r} for r in resources]
    if next_page is not None:
        base = make_client(FakeTransport()).fhir_base_url("hs")
        bundle["link"] = [{"relation": "next", "url": f"{base}/Observation?_page={next_page}"}]
    return bundle


def read(transport: FakeTransport, **kwargs) -> dict[str, StoredWindow]:
    client = make_client(transport)
    return stored_windows(lambda: client, "hs", "Patient/p-1", START, END, **kwargs)


def searches(transport: FakeTransport) -> list:
    return transport.calls("GET", SEARCH)


def test_reads_every_page_and_sends_the_r4_query():
    transport = FakeTransport()
    transport.add(
        "GET",
        SEARCH,
        json_response(200, page(observation("k1"), next_page=2)),
        json_response(200, page(observation("k2", "preliminary"))),
    )
    found = read(transport)
    assert {k: w.status for k, w in found.items()} == {"k1": "final", "k2": "preliminary"}
    assert found["k1"] == StoredWindow(
        status="final", end=datetime(2026, 1, 15, 12, 5, tzinfo=UTC), values=(None, 64.5, None)
    )
    first = searches(transport)[0].params
    assert first["subject"] == "Patient/p-1"
    assert first["code"] == "http://loinc.org|8867-4"
    assert first["_count"] == "1000"
    assert len(searches(transport)) == 2


def test_client_for_is_called_for_every_request_including_the_last():
    # health-sync relies on this: a fresh client per page, so each page's timeout is cut to the
    # time its run has left.
    transport = FakeTransport()
    transport.add(
        "GET",
        SEARCH,
        json_response(200, page(observation("k1"), next_page=2)),
        json_response(200, page(observation("k2"), next_page=3)),
        json_response(200, page(observation("k3"))),
    )
    client = make_client(transport)
    made = []

    def client_for():
        made.append(1)
        return client

    found = stored_windows(client_for, "hs", "Patient/p-1", START, END)
    assert sorted(found) == ["k1", "k2", "k3"]
    assert len(made) == 4  # three searches, and the next_page call that finds no next link


def test_a_key_found_twice_is_left_out_and_counted_on_the_callers_logger(caplog):
    transport = FakeTransport()
    transport.add(
        "GET",
        SEARCH,
        json_response(200, page(observation("k1"), observation("k2"), next_page=2)),
        json_response(200, page(observation("k1", "preliminary"))),
    )
    with caplog.at_level(logging.WARNING):
        found = read(transport, logger=logging.getLogger("some_service"))
    assert sorted(found) == ["k2"]
    records = [r for r in caplog.records if getattr(r, "fields", {}).get("event")]
    assert [(r.name, r.fields) for r in records] == [
        ("some_service", {"event": "stored-duplicate-key", "duplicate_keys": 1})
    ]
    assert "k1" not in caplog.text


def test_an_unusable_page_is_a_read_error():
    transport = FakeTransport()
    transport.add("GET", SEARCH, json_response(200, {"resourceType": "OperationOutcome"}))
    with pytest.raises(StoredReadError):
        read(transport)


def test_an_http_error_is_the_clients_and_propagates():
    transport = FakeTransport()
    transport.add("GET", SEARCH, json_response(503, {}))
    with pytest.raises(RetryableError):
        read(transport)


def test_the_page_bound_counts_pages_read_including_the_last_next_page():
    # With max_pages=N the read gives up once N pages have been read, before it looks at what
    # the Nth page's next link returned: N - 1 pages succeed, N pages fail. (This is the
    # behaviour both services had before T35; it is kept exactly.)
    def chain(pages: int) -> FakeTransport:
        transport = FakeTransport()
        responses = [
            json_response(200, page(observation(f"k{n}"), next_page=n + 1 if n < pages else None))
            for n in range(1, pages + 1)
        ]
        transport.add("GET", SEARCH, *responses)
        return transport

    assert len(read(chain(2), max_pages=3)) == 2
    with pytest.raises(StoredReadError, match="too many search pages"):
        read(chain(3), max_pages=3)


def test_the_default_bound_is_health_syncs_twenty_pages():
    assert MAX_SEARCH_PAGES == 20
    transport = FakeTransport()
    transport.add("GET", SEARCH, json_response(200, page(next_page=2)))  # repeated forever
    with pytest.raises(StoredReadError):
        read(transport)
    assert len(searches(transport)) == 21  # the first search and 20 next_page calls


def test_the_seeders_bound_grows_with_the_range():
    # One five-minute window per page at worst, plus one: three hours is 36 windows.
    assert max_pages_for_range(START, END) == 38
    assert max_pages_for_range(START, START + timedelta(days=366)) == 366 * 288 + 2
    assert max_pages_for_range(START, START + timedelta(minutes=4)) == 2
