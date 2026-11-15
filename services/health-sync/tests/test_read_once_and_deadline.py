"""Review round 1: read once and write what changed; the run deadline; the device fallback.

The clock starts at 2026-01-15T15:07:30Z, so a run asks for [12:05, 15:05) UTC with cutoff
14:05 UTC (windows ending later are preliminary).
"""

import logging
from collections import Counter
from datetime import timedelta

from health_sync.sync import Outcome, run_sync, split_range, sync_range
from health_sync.testing import (
    FHIR_PATH,
    SlowTransport,
    fhir_ok,
    formatted_logs,
    list_response,
    observation_keys,
    observations,
    rollup_page,
    token_response,
    utc,
    window,
)
from pulsebridge_gcp.testing import json_response


def run(world, config=None, *, ranges=None, deadline=None, **deps):
    config = config or world.config()
    if ranges is None:
        ranges = split_range(sync_range(world.clock.now, config.lookback))
    return run_sync(
        config,
        world.deps(**deps),
        ranges=ranges,
        deadline_seconds=config.run_deadline if deadline is None else deadline,
    )


def google(world, *pages, sources=None):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", *pages)
    world.google.add("GET", "/dataPoints", *(sources or [list_response()]))


def fhir_searches(world):
    return [r for r in world.fhir_transport.requests if r.method == "GET"]


W1300 = window("2026-01-15T13:00:00Z", 60, 61, 62)  # final in both runs below
W1405 = window("2026-01-15T14:05:00Z", 70, 72, 75)  # preliminary, then final at 15:22:30
W1430 = window("2026-01-15T14:30:00Z", 80, 81, 82)  # preliminary in both runs
W1505 = window("2026-01-15T15:05:00Z", 66, 67, 68)  # only in the second run's range


# ---- read once, write what changed -----------------------------------------------------------


def test_the_second_run_writes_only_new_and_newly_final_windows(world):
    world.fhir_transport.auto_store = True
    google(world, rollup_page([W1430, W1405, W1300]), rollup_page([W1505, W1430, W1405, W1300]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    first = run(world)
    world.clock.now += timedelta(minutes=15)
    second = run(world)
    assert (first.windows_new, first.windows_written) == (3, 3)
    assert first.stored_read == "patient-not-found"  # the first write created the Patient
    assert second.stored_read == "ok"
    assert (second.windows_unchanged, second.windows_finalised, second.windows_new) == (2, 1, 1)
    assert second.windows_written == 2
    written = [
        (o["effectivePeriod"]["start"], o["status"]) for o in observations(world.bundles()[1])
    ]
    assert written == [
        ("2026-01-15T09:05:00-05:00", "final"),
        ("2026-01-15T10:05:00-05:00", "preliminary"),
    ]


def test_nothing_changed_means_no_write_at_all(world):
    world.fhir_transport.auto_store = True
    items = [W1300]
    google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    world.clock.now += timedelta(minutes=15)
    report = run(world)
    assert (report.windows_unchanged, report.windows_written, report.bundles_written) == (1, 0, 0)
    assert len(world.bundles()) == 1
    assert len(world.google.calls("GET", "/dataPoints")) == 1  # no lookup for nothing to write


def test_changed_values_are_written_and_a_changed_final_window_is_counted_as_amended(world):
    """T06's heart_rate_transaction cannot write ``amended``, so the window is written ``final``
    with the new values (a silent amendment, README "Amendments") and counted."""
    world.fhir_transport.auto_store = True
    later_1300 = window("2026-01-15T13:00:00Z", 59, 61.5, 64)
    later_1430 = window("2026-01-15T14:30:00Z", 79, 80.5, 85)
    google(world, rollup_page([W1430, W1300]), rollup_page([later_1430, later_1300]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    report = run(world)
    assert (report.windows_amended, report.windows_changed, report.windows_unchanged) == (1, 1, 0)
    statuses = {
        o["effectivePeriod"]["start"]: (o["status"], o["valueQuantity"]["value"])
        for o in observations(world.bundles()[1])
    }
    assert statuses == {
        "2026-01-15T08:00:00-05:00": ("final", 61.5),
        "2026-01-15T09:30:00-05:00": ("preliminary", 80.5),
    }


def test_the_stored_window_search_uses_standard_parameters(world):
    world.fhir_transport.patient_server_id = "p-7"
    google(world, rollup_page([W1300]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    patient, count, windows = fhir_searches(world)
    assert patient.path.endswith("/fhir/Patient")
    assert patient.param_pairs == [
        ("identifier", "urn:pulsebridge:patient|PB-SYN-0001"),
        ("_elements", "id"),
    ]
    assert count.param_pairs == [
        ("subject", "Patient/p-7"),
        ("code", "http://loinc.org|8867-4"),
        ("status", "preliminary"),
        ("date", "lt2026-01-15T12:05:00Z"),
        ("_summary", "count"),
    ]
    assert windows.param_pairs == [
        ("subject", "Patient/p-7"),
        ("code", "http://loinc.org|8867-4"),
        ("date", "ge2026-01-15T12:05:00Z"),
        ("date", "lt2026-01-15T15:05:00Z"),
        ("_elements", "identifier,status,effectivePeriod,valueQuantity,component"),
        ("_count", "1000"),
    ]


def test_stored_windows_are_read_across_search_pages(world):
    world.fhir_transport.auto_store = True
    items = [window(f"2026-01-15T13:{m:02d}:00Z", 60, 61, 62) for m in (0, 5, 10)]
    google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    world.fhir_transport.page_size = 2
    report = run(world)
    assert report.windows_unchanged == 3
    assert report.windows_written == 0
    pages = [
        r
        for r in fhir_searches(world)
        if r.path.endswith("/Observation") and "_summary" not in r.params
    ]
    assert len(pages) == 2


def test_if_the_store_cannot_be_read_every_window_is_written(world, caplog):
    world.fhir_transport.auto_store = True
    google(world, rollup_page([W1300]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    world.fhir_transport.read_failure = json_response(503, {})
    with caplog.at_level(logging.WARNING):
        report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert (report.stored_read, report.windows_new, report.windows_written) == ("failed:503", 1, 1)
    assert "stored-read-failed" in formatted_logs(caplog.records)


def test_if_only_the_window_search_fails_every_window_is_written(world, caplog):
    world.fhir_transport.patient_server_id = "p-7"
    world.fhir_transport.observation_failure = json_response(500, {})
    google(world, rollup_page([W1300, W1405]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    with caplog.at_level(logging.WARNING):
        report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert report.stranded_preliminary == 0  # the count search still worked
    assert (report.stored_read, report.windows_new, report.windows_written) == ("failed:500", 2, 2)
    assert "stored-read-failed" in formatted_logs(caplog.records)


def test_each_window_is_written_at_most_twice_over_a_day_of_runs(world):
    """Twelve runs, 15 minutes apart, over windows that all arrived on time: each window is
    written once preliminary (if it was recent when first seen) and once final, never more."""
    world.fhir_transport.auto_store = True
    start = utc("2026-01-15T09:00:00Z")
    every = [
        window((start + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), 60, 61, 62)
        for i in range(12 * 12)
    ]
    world.google.add("POST", "/token", token_response())
    world.google.add("GET", "/dataPoints", list_response())
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    world.clock.now = utc("2026-01-15T12:00:00Z")
    for _ in range(12):
        end = world.clock.now.replace(second=0)
        visible = [w for w in every if utc(w["endTime"]) <= end]
        world.google._routes = [r for r in world.google._routes if r.suffix != ":rollUp"]
        world.google.add("POST", ":rollUp", rollup_page(visible[::-1]))
        run(world)
        world.clock.now += timedelta(minutes=15)
    writes = Counter(observation_keys(world.bundles()))
    assert max(writes.values()) <= 2
    assert sum(writes.values()) < 2 * len(writes) + 1


# ---- stranded preliminary windows ------------------------------------------------------------


def test_stranded_preliminary_windows_are_reported_with_a_warning(world, caplog):
    world.fhir_transport.patient_server_id = "p-7"
    world.fhir_transport.stranded = 5
    google(world, rollup_page([W1300]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    with caplog.at_level(logging.WARNING):
        report = run(world)
    assert report.stranded_preliminary == 5
    assert report.as_dict()["stranded_preliminary"] == 5
    assert [
        r.fields.get("stranded_preliminary")
        for r in caplog.records
        if r.fields.get("event") == "stranded-preliminary"
    ] == [5]


def test_no_stranded_windows_no_warning(world, caplog):
    world.fhir_transport.patient_server_id = "p-7"
    google(world, rollup_page([W1300]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    with caplog.at_level(logging.WARNING):
        report = run(world)
    assert report.stranded_preliminary == 0
    assert "stranded-preliminary" not in formatted_logs(caplog.records)


# ---- the deadline ----------------------------------------------------------------------------


def test_fhir_requests_get_a_timeout_cut_to_the_time_left(world):
    """FHIR answers take 45 s. Three searches use 135 of the 150 s; the Bundle gets a 15 s
    timeout instead of 60, times out, and the run ends exactly at its deadline."""
    world.fhir_transport.patient_server_id = "p-7"
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    slow = SlowTransport(world.fhir_transport, world.clock, latency=45)
    google(world, rollup_page([W1300]))
    begin = world.clock.mono
    report = run(world, fhir_transport=slow)
    assert slow.timeouts == [60, 60, 60, 15]
    assert world.clock.mono - begin <= 150
    assert report.outcome == Outcome.FHIR_UNAVAILABLE
    assert report.http_status == 503


def test_no_bundle_is_started_with_too_little_time_left(world):
    world.fhir_transport.patient_server_id = "p-7"
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    slow = SlowTransport(world.fhir_transport, world.clock, latency=48)
    google(world, rollup_page([W1300]))
    begin = world.clock.mono
    report = run(world, fhir_transport=slow)
    assert (report.outcome, report.reason) == (Outcome.DEADLINE_REACHED, "before-bundle")
    assert report.http_status == 503
    assert world.bundles() == []
    assert world.clock.mono - begin <= 150


def test_a_range_started_late_gets_a_cut_timeout(world):
    google(world, rollup_page([]))
    slow = SlowTransport(world.google, world.clock, latency=22)
    ranges = split_range(sync_range(world.clock.now, timedelta(hours=2)), timedelta(hours=1))
    begin = world.clock.mono
    report = run(world, ranges=ranges, deadline=55, google_transport=slow)
    # token 22 s + first range 22 s = 44 s; 11 s left is enough to start the second range (the
    # step minimum is 10 s), whose request gets an 11 s timeout instead of 30 and times out.
    assert slow.timeouts == [30, 30, 11]
    assert (report.outcome, report.ranges_completed) == (Outcome.GOOGLE_UNAVAILABLE, 1)
    assert world.clock.mono - begin == 55


def test_a_slow_google_never_pushes_a_run_past_its_deadline(world):
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    google(world, rollup_page([W1300]))
    slow = SlowTransport(world.google, world.clock, latency=40)  # over the 30 s timeout
    begin = world.clock.mono
    report = run(world, world.config(max_attempts=10), google_transport=slow)
    # Timeouts at 30, 61, 93, 127 after waits of 1, 2, 4, 8 s; at 135 only 15 s are left.
    assert slow.timeouts == [30, 30, 30, 30, 15]
    assert world.clock.sleeps == [1, 2, 4, 8]
    assert report.outcome == Outcome.GOOGLE_UNAVAILABLE
    assert world.clock.mono - begin == 150


def test_a_range_start_check_reports_deadline_reached(world):
    google(world, rollup_page([]))
    slow = SlowTransport(world.google, world.clock, latency=20)
    ranges = split_range(sync_range(world.clock.now, timedelta(hours=2)), timedelta(hours=1))
    report = run(world, ranges=ranges, deadline=48, google_transport=slow)
    # 20 s token + 20 s first range = 40 s; 8 s left is under the 10 s step minimum.
    assert (report.outcome, report.reason) == (Outcome.DEADLINE_REACHED, "before-range")
    assert report.ranges_completed == 1


# ---- device name -----------------------------------------------------------------------------


def test_an_unnamed_range_takes_the_name_of_the_newest_earlier_point(world):
    google(
        world,
        rollup_page([W1300]),
        sources=[
            list_response(None, "FITBIT", "PASSIVELY_MEASURED"),
            list_response("Synthetic Watch 1", None, None),
        ],
    )
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    first, second = world.google.calls("GET", "/dataPoints")
    assert first.params["filter"] == (
        'heart_rate.sample_time.physical_time >= "2026-01-15T13:00:00Z" AND '
        'heart_rate.sample_time.physical_time < "2026-01-15T13:05:00Z"'
    )
    assert second.params == {
        "filter": 'heart_rate.sample_time.physical_time < "2026-01-15T13:00:00Z"',
        "pageSize": "1",
    }
    bundle = world.bundles()[0]
    assert bundle["entry"][1]["resource"]["deviceName"][0]["name"] == "Synthetic Watch 1"
    provenance = bundle["entry"][3]["resource"]
    agents = {a["type"]["coding"][0]["code"]: a for a in provenance["agent"]}
    assert agents["custodian"]["who"]["identifier"]["value"] == "FITBIT"  # from the range


def test_a_named_range_makes_one_lookup_only(world):
    google(world, rollup_page([W1300]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    assert len(world.google.calls("GET", "/dataPoints")) == 1


def test_the_device_lookup_is_per_batch(world):
    items = [window(f"2026-01-15T13:{5 * i:02d}:00Z", 60, 61, 62) for i in range(5)]
    google(world, rollup_page(items[::-1]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world, world.config(batch_windows=2))
    filters = [c.params["filter"] for c in world.google.calls("GET", "/dataPoints")]
    spans = [("13:00", "13:10"), ("13:10", "13:20"), ("13:20", "13:25")]
    assert filters == [
        f'heart_rate.sample_time.physical_time >= "2026-01-15T{a}:00Z" AND '
        f'heart_rate.sample_time.physical_time < "2026-01-15T{b}:00Z"'
        for a, b in spans
    ]
