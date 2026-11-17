"""The shared stored-window read (pulsebridge_gcp.stored, T35) as health-sync calls it.

These run whole syncs through the fakes, so they check health-sync's own call site: a fresh
search client for every page (each page's timeout cut to the time the run has left), the
20-page bound, and the T35 handling of search pages that are not valid FHIR JSON (a read
error: every window is written, the run still succeeds). Synthetic data only.

The clock starts at 2026-01-15T15:07:30Z (health_sync.testing), so W1300 is final.
"""

import pytest
from health_sync.sync import Outcome, run_sync, split_range, sync_range
from health_sync.testing import (
    FHIR_PATH,
    SlowTransport,
    fhir_ok,
    list_response,
    rollup_page,
    token_response,
    window,
)
from pulsebridge_gcp.testing import json_response

W1300 = window("2026-01-15T13:00:00Z", 60, 61, 62)


def run(world, **deps):
    config = world.config()
    return run_sync(
        config,
        world.deps(**deps),
        ranges=split_range(sync_range(world.clock.now, config.lookback)),
        deadline_seconds=config.run_deadline,
    )


def ready(world, *, stored: int = 0, page_size: int | None = None) -> None:
    """One window from Google; a store with a Patient and ``stored`` other windows of ours."""
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", rollup_page([W1300]))
    world.google.add("GET", "/dataPoints", list_response())
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    world.fhir_transport.patient_server_id = "p-7"
    world.fhir_transport.page_size = page_size
    world.fhir_transport.stored = [
        {
            "resourceType": "Observation",
            "identifier": [{"system": "urn:pulsebridge:observation", "value": f"k{i}"}],
            "status": "final",
            # Every window T06 writes has all four (end, min, avg, max); a read where none has
            # them is StoredElementsMissing (T39).
            "effectivePeriod": {"start": "2026-01-15T10:00:00Z", "end": "2026-01-15T10:05:00Z"},
            "valueQuantity": {"value": 60},
            "component": [
                {
                    "code": {"coding": [{"system": "http://loinc.org", "code": "96935-2"}]},
                    "valueQuantity": {"value": 58},
                },
                {
                    "code": {"coding": [{"system": "http://loinc.org", "code": "55426-1"}]},
                    "valueQuantity": {"value": 63},
                },
            ],
        }
        for i in range(stored)
    ]


def observation_pages(transport) -> list:
    return [
        r
        for r in transport.requests
        if r.method == "GET"
        and r.path.endswith("/Observation")
        and r.params.get("_count") != "0"  # not the stranded count
    ]


# ---- a fresh client for every page -------------------------------------------------------------


def test_each_page_gets_a_client_whose_timeout_is_the_time_left(world):
    """Every FHIR request takes 9 s of the 150 s budget. The Patient search and the stranded
    count leave 132 s, enough to read (3 x 10 s + one Bundle at 30 s + 60 s = 120 s). Each page
    then gets min(10 s, time left): 10 s for 14 pages, then the 6 s that is left. The 15th page
    times out, so the read fails (everything would be written) and the run stops before the
    Bundle. With one client for every page, the 15th page would also get 10 s."""
    ready(world, stored=30, page_size=1)
    slow = SlowTransport(world.fhir_transport, world.clock, latency=9)
    report = run(world, fhir_transport=slow)
    patient_and_count, pages = slow.timeouts[:2], slow.timeouts[2:]
    assert patient_and_count == [10, 10]
    assert pages == [10] * 14 + [6]  # 132 - 14 x 9 = 6 s left for the 15th page
    assert len(observation_pages(world.fhir_transport)) == 14  # the 15th never answered
    assert report.stored_read == "failed:TimedOutError"
    assert (report.outcome, report.reason) == (Outcome.DEADLINE_REACHED, "before-bundle")


# ---- the 20-page bound -------------------------------------------------------------------------


def test_nineteen_pages_are_read(world):
    ready(world, stored=19, page_size=1)
    report = run(world)
    assert report.stored_read == "ok"
    assert len(observation_pages(world.fhir_transport)) == 19
    assert (report.windows_new, report.windows_written) == (1, 1)


def test_the_read_gives_up_after_twenty_pages_and_writes_everything(world):
    ready(world, stored=25, page_size=1)
    report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert report.stored_read == "failed:StoredReadError"
    assert len(observation_pages(world.fhir_transport)) == 21  # first page + 20 next pages
    assert (report.windows_new, report.windows_written) == (1, 1)


# ---- search pages that are not valid FHIR JSON (T35) -------------------------------------------


def test_a_null_entry_is_a_read_error_and_every_window_is_written(world):
    ready(world)
    world.fhir_transport.observation_failure = json_response(
        200, {"resourceType": "Bundle", "type": "searchset", "entry": None}
    )
    report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert report.stored_read == "failed:StoredReadError"
    assert (report.windows_new, report.windows_written) == (1, 1)


def test_a_number_as_entry_is_a_read_error_not_a_crash(world):
    ready(world)
    world.fhir_transport.observation_failure = json_response(
        200, {"resourceType": "Bundle", "type": "searchset", "entry": 5}
    )
    report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert report.stored_read == "failed:StoredReadError"
    assert report.windows_written == 1


def test_a_component_that_is_not_an_object_is_skipped_and_the_window_rewritten(world):
    # W1300 is written once, then its stored copy gets components that are not objects. The
    # next run reads min and max as unknown, so the values differ from the roll-up's and the
    # window (stored final, complete now) is written amended. Before T35 this crashed the run.
    ready(world)
    world.fhir_transport.patient_server_id = None
    world.fhir_transport.auto_store = True
    first = run(world)
    assert first.windows_written == 1
    (resource,) = world.fhir_transport.stored
    resource["component"] = ["not an object", {"code": {"coding": ["nor this"]}}]
    report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert report.stored_read == "ok"
    assert (report.windows_amended, report.windows_written) == (1, 1)


@pytest.mark.parametrize(
    "shape",
    [
        {"component": [{"code": "8867-4"}]},  # code as a string
        {"component": 5},
        {"identifier": 5},
    ],
)
def test_an_observation_with_a_wrong_shape_fails_the_read_and_every_window_is_written(world, shape):
    ready(world)
    resource = {
        "resourceType": "Observation",
        "identifier": [{"system": "urn:pulsebridge:observation", "value": "k-other"}],
        "status": "final",
    }
    world.fhir_transport.stored = [resource | shape]
    report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert report.stored_read == "failed:StoredReadError"
    assert (report.windows_new, report.windows_written) == (1, 1)
