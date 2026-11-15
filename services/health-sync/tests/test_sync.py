"""A sync run end to end: Google (scripted) -> T06 mapping -> FHIR store (scripted).

The clock is fixed at 2026-01-15T15:07:30Z (10:07:30 in New York, UTC-05:00 in January). With the
defaults (look-back 180 min, final after 60 min) a run asks for [12:05, 15:05) UTC and writes
windows that ended at or before 14:05 UTC as final, later ones as preliminary.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from health_sync.sync import (
    HTTP_STATUS,
    UNNAMED_DEVICE,
    Outcome,
    TimeRange,
    batches,
    clean_windows,
    completion_cutoff,
    floor_to_window,
    run_sync,
    split_range,
    sync_range,
)
from health_sync.testing import (
    FHIR_PATH,
    PATIENT_ID,
    fake,
    fhir_ok,
    google_error,
    list_response,
    oauth_error,
    observation_keys,
    observations,
    rollup_page,
    token_response,
    utc,
    window,
)
from pulsebridge_gcp import RetryableError
from pulsebridge_gcp.testing import json_response


def run(world, config=None, ranges=None):
    config = config or world.config()
    if ranges is None:
        ranges = split_range(sync_range(world.clock.now, config.lookback))
    return run_sync(config, world.deps(), ranges=ranges, deadline_seconds=config.run_deadline)


def script_google(world, *pages, source=None):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", *pages)
    world.google.add("GET", "/dataPoints", source or list_response())


# ---- ranges and the cutoff -------------------------------------------------------------------


def test_floor_to_window_rounds_down_to_five_minutes_utc():
    assert floor_to_window(utc("2026-01-15T15:07:30Z")) == utc("2026-01-15T15:05:00Z")
    assert floor_to_window(utc("2026-01-15T15:05:00Z")) == utc("2026-01-15T15:05:00Z")
    assert floor_to_window(utc("2026-01-15T15:04:59.999Z")) == utc("2026-01-15T15:00:00Z")


def test_sync_range_is_the_lookback_ending_at_the_last_boundary():
    assert sync_range(utc("2026-01-15T15:07:30Z"), timedelta(minutes=180)) == TimeRange(
        utc("2026-01-15T12:05:00Z"), utc("2026-01-15T15:05:00Z")
    )


def test_completion_cutoff_is_final_after_before_now_on_a_boundary():
    cutoff = completion_cutoff(utc("2026-01-15T15:07:30Z"), timedelta(minutes=60))
    assert cutoff == utc("2026-01-15T14:05:00Z")


def test_consecutive_runs_overlap_and_every_preliminary_window_is_requested_again():
    """Runs 15 minutes apart: each range overlaps the previous one, and every window written
    preliminary by one run lies inside the next run's range (so it can become final)."""
    lookback, final_after = timedelta(minutes=180), timedelta(minutes=60)
    first_now = utc("2026-01-15T15:07:30Z")
    second_now = first_now + timedelta(minutes=15)
    first, second = sync_range(first_now, lookback), sync_range(second_now, lookback)
    assert second.start < first.end  # overlap
    assert first.end - second.start == timedelta(minutes=165)
    # Preliminary windows of the first run end in (cutoff, first.end]; all start >= second.start.
    assert completion_cutoff(first_now, final_after) - timedelta(minutes=5) >= second.start


# ---- one run end to end ----------------------------------------------------------------------


def test_one_run_end_to_end(world):
    items = [  # newest first, as Google sends them; 13:00 to 14:00 has no data (absent)
        window("2026-01-15T15:00:00Z", 81, 84.25, 90),
        window("2026-01-15T14:05:00Z", 70, 72.4, 75),
        window("2026-01-15T14:00:00Z", 66, 67, 69),
        window("2026-01-15T12:05:00Z", 55, 57.125, 60),
    ]
    script_google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    report = run(world)

    assert report.outcome == Outcome.SYNCED and report.http_status == 200
    assert (report.windows_fetched, report.windows_written, report.bundles_written) == (4, 4, 1)
    assert (report.windows_final, report.windows_preliminary) == (2, 2)
    assert world.rollup_bodies() == [
        {
            "range": {"startTime": "2026-01-15T12:05:00Z", "endTime": "2026-01-15T15:05:00Z"},
            "windowSize": "300s",
            "pageSize": 4032,
        }
    ]
    (bundle,) = world.bundles()
    assert bundle["type"] == "transaction"
    requests = [e["request"] for e in bundle["entry"]]
    kinds = [(r["method"], r["url"].split("?")[0].split("/")[0]) for r in requests]
    assert (
        kinds
        == [("POST", "Patient"), ("POST", "Device")]
        + [
            ("PUT", "Observation"),
            ("PUT", "Provenance"),
        ]
        * 4
    )
    # D11: Patient and Device are conditional creates, never overwritten.
    assert requests[0] == {
        "method": "POST",
        "url": "Patient",
        "ifNoneExist": f"identifier=urn:pulsebridge:patient|{PATIENT_ID}",
    }
    assert requests[1]["ifNoneExist"].startswith("identifier=urn:pulsebridge:device|")
    # Observations: conditional update on their own identifier, oldest first.
    for entry in bundle["entry"][2::2]:
        key = entry["resource"]["identifier"][0]["value"]
        assert entry["request"] == {
            "method": "PUT",
            "url": f"Observation?identifier=urn:pulsebridge:observation|{key}",
        }
    obs = observations(bundle)
    assert [(o["effectivePeriod"]["start"], o["status"]) for o in obs] == [
        ("2026-01-15T07:05:00-05:00", "final"),
        ("2026-01-15T09:00:00-05:00", "final"),  # ends 14:05Z, exactly the cutoff: final
        ("2026-01-15T09:05:00-05:00", "preliminary"),  # ends 14:10Z, after the cutoff
        ("2026-01-15T10:00:00-05:00", "preliminary"),
    ]
    device = bundle["entry"][1]["resource"]
    assert device["deviceName"][0]["name"] == "Synthetic Watch 1"


def test_the_trace_panel_can_rebuild_the_google_window_from_what_is_stored(world):
    """No raw response is stored; start, end, min, avg and max come back exactly as received."""
    item = window("2026-01-15T14:10:00Z", 61, 73.41666666666667, 92)
    script_google(world, rollup_page([item]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    bundle = world.bundles()[0]
    (obs,) = observations(bundle)
    provenance = bundle["entry"][3]["resource"]
    rebuilt = {
        "startTime": utc(obs["effectivePeriod"]["start"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": utc(obs["effectivePeriod"]["end"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "heartRate": {
            "beatsPerMinuteMin": obs["component"][0]["valueQuantity"]["value"],
            "beatsPerMinuteAvg": obs["valueQuantity"]["value"],
            "beatsPerMinuteMax": obs["component"][1]["valueQuantity"]["value"],
        },
    }
    assert rebuilt == item
    assert provenance["entity"][0]["what"]["identifier"]["value"] == (
        "2026-01-15T14:10:00Z/2026-01-15T14:15:00Z"
    )


def test_the_data_source_reported_by_google_reaches_the_provenance(world):
    script_google(
        world,
        rollup_page([window("2026-01-15T14:10:00Z", 60, 61, 62)]),
        source=list_response("Synthetic Watch 1", "FITBIT", "PASSIVELY_MEASURED"),
    )
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    provenance = world.bundles()[0]["entry"][3]["resource"]
    agents = {a["type"]["coding"][0]["code"]: a for a in provenance["agent"]}
    assert agents["performer"]["role"][0]["coding"][0]["code"] == "PASSIVELY_MEASURED"
    assert agents["custodian"]["who"]["identifier"]["value"] == "FITBIT"


def test_without_a_reported_source_nothing_is_asserted(world):
    script_google(
        world,
        rollup_page([window("2026-01-15T14:10:00Z", 60, 61, 62)]),
        source=list_response(None, None, None),
    )
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    bundle = world.bundles()[0]
    assert bundle["entry"][1]["resource"]["deviceName"][0]["name"] == UNNAMED_DEVICE
    provenance = bundle["entry"][3]["resource"]
    types = [a["type"]["coding"][0]["code"] for a in provenance["agent"]]
    assert types == ["performer", "assembler"]  # no custodian
    assert "role" not in provenance["agent"][0]


def test_times_carry_the_offset_in_force_on_each_side_of_a_daylight_saving_change(world):
    # US daylight saving time began 2026-03-08 at 07:00 UTC (02:00 EST became 03:00 EDT).
    world.clock.now = utc("2026-03-08T08:07:30Z")
    items = [window("2026-03-08T07:00:00Z", 60, 61, 62), window("2026-03-08T06:55:00Z", 60, 61, 62)]
    script_google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    starts = [o["effectivePeriod"]["start"] for o in observations(world.bundles()[0])]
    assert starts == ["2026-03-08T01:55:00-05:00", "2026-03-08T03:00:00-04:00"]


# ---- the windows themselves ------------------------------------------------------------------


def test_newest_first_input_is_written_oldest_first(world):
    items = [window(f"2026-01-15T14:{m:02d}:00Z", 60, 61, 62) for m in (50, 40, 30, 20)]
    script_google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    starts = [o["effectivePeriod"]["start"] for o in observations(world.bundles()[0])]
    assert starts == sorted(starts)
    assert starts[0] == "2026-01-15T09:20:00-05:00"


def test_paged_windows_are_all_written(world):
    script_google(
        world,
        rollup_page([window("2026-01-15T15:00:00Z", 60, 61, 62)], "p2"),
        rollup_page([window("2026-01-15T14:55:00Z", 60, 61, 62)], "p3"),
        rollup_page([window("2026-01-15T14:50:00Z", 60, 61, 62)]),
    )
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    report = run(world)
    assert (report.pages, report.windows_written) == (3, 3)
    assert len(observations(world.bundles()[0])) == 3


def test_clean_windows_drops_empty_invalid_and_duplicate_items():
    good = window("2026-01-15T14:00:00Z", 60, 61, 62)
    clean = clean_windows(
        [
            {"startTime": "2026-01-15T14:10:00Z", "endTime": "2026-01-15T14:15:00Z"},  # no data
            window("2026-01-15T14:05:00Z", 70, 65, 80),  # avg below min: refused by T06
            window("2026-01-15T13:55:00Z", 0, 0, 0),  # out of range
            good,
            window("2026-01-15T14:00:00Z", 90, 91, 92),  # second item for the same start
            "not an object",
        ]
    )
    assert clean.items == [good]
    assert (clean.empty, clean.invalid) == (2, 3)


def test_an_invalid_window_is_skipped_and_the_rest_written(world):
    items = [window("2026-01-15T14:10:00Z", 70, 65, 80), window("2026-01-15T14:05:00Z", 60, 61, 62)]
    script_google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert (report.windows_invalid, report.windows_written) == (1, 1)


def test_no_windows_means_no_data_source_call_and_no_write(world):
    script_google(world, rollup_page([]))
    report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert report.windows_written == 0
    assert world.google.calls("GET", "/dataPoints") == []
    assert world.fhir_transport.requests == []


# ---- idempotency and overlap -----------------------------------------------------------------


def test_overlapping_runs_rewrite_the_same_observations(world):
    """Two runs 15 minutes apart: the windows both see land on the same keys, and a window that
    was only partly synced (new values, now complete) updates the same Observation as final."""
    early = [window("2026-01-15T14:05:00Z", 70, 72, 75), window("2026-01-15T14:00:00Z", 66, 67, 69)]
    late = [
        window("2026-01-15T14:20:00Z", 60, 61, 62),
        window("2026-01-15T14:05:00Z", 68, 73.5, 79),  # more samples arrived for this window
        window("2026-01-15T14:00:00Z", 66, 67, 69),
    ]
    script_google(world, rollup_page(early), rollup_page(late))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    world.clock.now += timedelta(minutes=15)
    run(world)
    first, second = world.bundles()
    keys1, keys2 = observation_keys([first]), observation_keys([second])
    assert keys2[:2] == keys1  # same windows, same keys
    assert len(set(keys2)) == 3
    obs1, obs2 = observations(first), observations(second)
    assert (obs1[1]["status"], obs1[1]["valueQuantity"]["value"]) == ("preliminary", 72)
    assert (obs2[1]["status"], obs2[1]["valueQuantity"]["value"]) == ("final", 73.5)

    def prov(b):
        return [e["request"]["url"] for e in b["entry"] if "Provenance" in e["request"]["url"]]

    assert prov(second)[:2] == prov(first)
    # Patient and Device: the same conditional creates in both Bundles.
    assert first["entry"][:2] == second["entry"][:2]


def test_rerunning_the_same_range_sends_the_same_writes(world):
    items = [window("2026-01-15T14:05:00Z", 70, 72, 75)]
    script_google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world)
    run(world)
    first, second = world.bundles()

    def strip(b):  # Provenance.recorded is the write time and may differ
        return [
            {k: v for k, v in e["resource"].items() if k != "recorded"} | {"_req": e["request"]}
            for e in b["entry"]
        ]

    assert strip(first) == strip(second)


# ---- batching --------------------------------------------------------------------------------


def test_batches_split_at_the_configured_size():
    assert [len(b) for b in batches(list(range(5)), 2)] == [2, 2, 1]
    assert [len(b) for b in batches(list(range(4)), 4)] == [4]
    assert list(batches([], 3)) == []


@pytest.mark.parametrize(
    ("count", "size", "expected"), [(5, 2, [2, 2, 1]), (4, 4, [4]), (4, 3, [3, 1]), (1, 100, [1])]
)
def test_each_batch_is_its_own_bundle_with_patient_and_device(world, count, size, expected):
    items = [window(f"2026-01-15T14:{5 * i:02d}:00Z", 60, 61, 62) for i in range(count)]
    script_google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    report = run(world, world.config(batch_windows=size))
    bundles = world.bundles()
    assert [len(observations(b)) for b in bundles] == expected
    assert all(len(b["entry"]) == 2 + 2 * n for b, n in zip(bundles, expected, strict=True))
    assert all(
        [e["resource"]["resourceType"] for e in b["entry"][:2]] == ["Patient", "Device"]
        for b in bundles
    )
    assert report.bundles_written == len(expected)
    # Together the batches hold every window exactly once.
    assert len(set(observation_keys(bundles))) == count


# ---- reconnect needed and other Google failures ----------------------------------------------


def test_invalid_grant_ends_the_run_cleanly_as_reconnect_needed(world):
    world.google.add("POST", "/token", oauth_error("invalid_grant"))
    report = run(world)
    assert report.outcome == Outcome.RECONNECT_NEEDED
    assert report.reason == "refresh-token-invalid"
    assert report.http_status == 200  # Scheduler must not retry: only a reconnect can help
    assert world.token_calls() == 1  # no retry loop
    assert world.clock.sleeps == []
    assert world.google.calls("POST", ":rollUp") == []
    assert world.fhir_transport.requests == []


def test_a_missing_secret_file_is_not_configured(world):
    world.write_secrets(refresh_token=None)
    report = run(world)
    assert report.outcome == Outcome.NOT_CONFIGURED
    assert report.reason == "secrets-missing:refresh-token"
    assert world.google.requests == []


def test_a_new_refresh_token_version_is_used_by_the_next_run(world):
    """The secret file is read at the start of every run, as Cloud Run serves the latest
    version on each read of a mounted secret."""
    world.google.add("POST", "/token", oauth_error("invalid_grant"), token_response())
    world.google.add("POST", ":rollUp", rollup_page([]))
    assert run(world).outcome == Outcome.RECONNECT_NEEDED
    new_token = fake("refresh", "grant", "new")
    world.write_secrets(refresh_token=new_token)
    assert run(world).outcome == Outcome.SYNCED
    bodies = [c.body.decode() for c in world.google.calls("POST", "/token")]
    assert new_token in bodies[1]
    assert new_token not in bodies[0]


def test_google_unavailable_after_retries_fails_the_run_with_503(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", google_error(503))
    report = run(world)
    assert report.outcome == Outcome.GOOGLE_UNAVAILABLE
    assert report.http_status == 503
    assert report.reason == "heart-rate dataPoints:rollUp:retries-exhausted:503"
    assert world.clock.sleeps == [1.0, 2.0, 4.0]


def test_a_google_refusal_fails_the_run_with_500(world):
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", google_error(400, "INVALID_TIME_RANGE"))
    report = run(world)
    assert (report.outcome, report.http_status) == (Outcome.GOOGLE_ERROR, 500)


# ---- FHIR store failures ---------------------------------------------------------------------


def one_window(world):
    script_google(world, rollup_page([window("2026-01-15T14:05:00Z", 70, 72, 75)]))


def test_a_transient_fhir_failure_is_retried_then_written(world):
    one_window(world)
    world.fhir_transport.add(
        "POST", FHIR_PATH, json_response(503, {}), json_response(409, {}), fhir_ok()
    )
    report = run(world)
    assert report.outcome == Outcome.SYNCED
    assert len(world.bundles()) == 3
    assert world.clock.sleeps == [1.0, 2.0]


def test_a_fhir_store_that_stays_unavailable_fails_with_503(world):
    one_window(world)
    world.fhir_transport.add("POST", FHIR_PATH, RetryableError("POST fhir", None))
    report = run(world)
    assert (report.outcome, report.http_status) == (Outcome.FHIR_UNAVAILABLE, 503)
    assert report.reason == "fhir-write-failed"
    assert len(world.bundles()) == 4


def test_a_refused_bundle_is_not_resent_and_fails_with_500(world):
    one_window(world)
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "invalid",
                "diagnostics": "value 72 bad",
                "expression": ["Observation.valueQuantity"],
            }
        ],
    }
    world.fhir_transport.add("POST", FHIR_PATH, json_response(400, outcome))
    report = run(world)
    assert (report.outcome, report.http_status) == (Outcome.FHIR_REJECTED, 500)
    assert (report.fhir_status, report.reason) == (400, "fhir-refused-bundle")
    assert report.fhir_issues == [
        {"severity": "error", "code": "invalid", "expression": ["Observation.valueQuantity"]}
    ]
    assert len(world.bundles()) == 1
    assert world.clock.sleeps == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_iam_or_a_missing_store_is_unavailable_without_retrying_now(world, status):
    one_window(world)
    world.fhir_transport.add("POST", FHIR_PATH, json_response(status, {}))
    report = run(world)
    assert (report.outcome, report.fhir_status) == (Outcome.FHIR_UNAVAILABLE, status)
    assert len(world.bundles()) == 1


def test_a_persistent_conflict_is_unavailable_after_the_retries(world):
    one_window(world)
    world.fhir_transport.add("POST", FHIR_PATH, json_response(409, {}))
    report = run(world)
    assert (report.outcome, report.reason) == (Outcome.FHIR_UNAVAILABLE, "fhir-conflict")
    assert len(world.bundles()) == 4


def test_a_failure_in_a_later_batch_keeps_earlier_batches_and_reports_the_failure(world):
    items = [window(f"2026-01-15T14:{5 * i:02d}:00Z", 60, 61, 62) for i in range(3)]
    script_google(world, rollup_page(items))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok(), json_response(400, {}))
    report = run(world, world.config(batch_windows=2))
    assert report.outcome == Outcome.FHIR_REJECTED
    assert (report.bundles_written, report.windows_written, report.ranges_completed) == (1, 2, 0)


def test_response_statuses_are_counted_per_resource_type(world):
    one_window(world)
    statuses = ["200 OK", "201 Created", "200 OK", "201 Created"]
    response = {
        "resourceType": "Bundle",
        "type": "transaction-response",
        "entry": [{"response": {"status": s}} for s in statuses],
    }
    world.fhir_transport.add("POST", FHIR_PATH, json_response(200, response))
    report = run(world)
    assert report.entry_statuses == {
        "Patient 200": 1,
        "Device 201": 1,
        "Observation 200": 1,
        "Provenance 201": 1,
    }


def test_every_outcome_has_an_http_status():
    assert set(HTTP_STATUS) == set(Outcome)
    assert {o for o, s in HTTP_STATUS.items() if s == 200} == {
        Outcome.SYNCED,
        Outcome.RECONNECT_NEEDED,
        Outcome.NOT_CONFIGURED,
    }


def test_another_time_zone_is_applied(world):
    script_google(world, rollup_page([window("2026-01-15T14:05:00Z", 70, 72, 75)]))
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    run(world, world.config(time_zone=ZoneInfo("Asia/Kolkata")))
    (obs,) = observations(world.bundles()[0])
    assert obs["effectivePeriod"]["start"] == "2026-01-15T19:35:00+05:30"
