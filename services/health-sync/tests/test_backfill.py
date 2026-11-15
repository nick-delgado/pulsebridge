"""``backfill --days N``: the 14-day split, batching, re-running, exit codes."""

import json
from datetime import timedelta

import pytest
from health_sync import backfill
from health_sync.sync import TimeRange, backfill_range, split_range
from health_sync.testing import (
    FHIR_PATH,
    fhir_ok,
    list_response,
    oauth_error,
    observation_keys,
    rollup_page,
    token_response,
    utc,
    window,
)


def test_a_range_that_is_not_a_multiple_of_14_days_ends_with_a_shorter_request():
    now = utc("2026-02-20T12:03:10Z")
    pieces = split_range(backfill_range(now, 30))
    assert pieces == [
        TimeRange(utc("2026-01-21T12:00:00Z"), utc("2026-02-04T12:00:00Z")),
        TimeRange(utc("2026-02-04T12:00:00Z"), utc("2026-02-18T12:00:00Z")),
        TimeRange(utc("2026-02-18T12:00:00Z"), utc("2026-02-20T12:00:00Z")),
    ]


def test_an_exact_multiple_of_14_days_gives_whole_requests():
    pieces = split_range(backfill_range(utc("2026-02-20T12:00:00Z"), 28))
    assert pieces == [
        TimeRange(utc("2026-01-23T12:00:00Z"), utc("2026-02-06T12:00:00Z")),
        TimeRange(utc("2026-02-06T12:00:00Z"), utc("2026-02-20T12:00:00Z")),
    ]


def test_pieces_stay_336_hours_across_a_daylight_saving_change():
    """US daylight saving time ended 2026-11-01 (EDT -04:00 to EST -05:00). The split is in
    physical time, so the piece crossing the change is exactly 14 x 24 hours: a local-calendar
    split would make it 337 hours and the API would refuse it."""
    pieces = split_range(backfill_range(utc("2026-11-10T17:02:00Z"), 20))
    assert pieces == [
        TimeRange(utc("2026-10-21T17:00:00Z"), utc("2026-11-04T17:00:00Z")),
        TimeRange(utc("2026-11-04T17:00:00Z"), utc("2026-11-10T17:00:00Z")),
    ]
    assert all(p.end - p.start <= timedelta(days=14) for p in pieces)
    assert pieces[0].end - pieces[0].start == timedelta(hours=336)


def test_backfill_requests_each_piece_and_writes_every_window(world, capsys):
    world.clock.now = utc("2026-02-20T12:03:10Z")
    world.google.add("POST", "/token", token_response())
    world.google.add(
        "POST",
        ":rollUp",
        rollup_page([window("2026-01-22T10:00:00Z", 60, 61, 62)]),
        rollup_page([]),
        rollup_page(
            [window("2026-02-19T10:05:00Z", 60, 61, 62), window("2026-02-19T10:00:00Z", 60, 61, 62)]
        ),
    )
    world.google.add("GET", "/dataPoints", list_response())
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    code = backfill.run(30, world.config(), world.deps())
    assert code == backfill.EXIT_OK
    ranges = [b["range"] for b in world.rollup_bodies()]
    assert ranges == [
        {"startTime": "2026-01-21T12:00:00Z", "endTime": "2026-02-04T12:00:00Z"},
        {"startTime": "2026-02-04T12:00:00Z", "endTime": "2026-02-18T12:00:00Z"},
        {"startTime": "2026-02-18T12:00:00Z", "endTime": "2026-02-20T12:00:00Z"},
    ]
    assert len(world.bundles()) == 2  # the empty middle piece writes nothing
    report = json.loads(capsys.readouterr().out)
    assert (report["outcome"], report["windows_written"], report["ranges_completed"]) == (
        "synced",
        3,
        3,
    )
    # One token for the whole backfill, one data-source call per piece with data.
    assert world.token_calls() == 1
    assert len(world.google.calls("GET", "/dataPoints")) == 2


def test_a_day_of_data_is_batched_and_rerunning_writes_the_same_keys(world, capsys):
    """288 windows a day: about 580 entries, written as three Bundles of at most 100 windows."""
    world.clock.now = utc("2026-01-16T00:00:00Z")
    day = [
        window(
            (utc("2026-01-15T00:00:00Z") + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            60,
            61,
            62,
        )
        for i in range(288)
    ][::-1]
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", rollup_page(day))
    world.google.add("GET", "/dataPoints", list_response())
    world.fhir_transport.add("POST", FHIR_PATH, fhir_ok())
    assert backfill.run(1, world.config(), world.deps()) == backfill.EXIT_OK
    first = world.bundles()
    assert [len(b["entry"]) for b in first] == [202, 202, 178]
    assert sum(len(b["entry"]) for b in first) == 582
    assert backfill.run(1, world.config(), world.deps()) == backfill.EXIT_OK
    second = world.bundles()[3:]
    assert observation_keys(second) == observation_keys(first)
    assert len(set(observation_keys(first))) == 288


def test_reconnect_needed_exits_3_without_writing(world, capsys):
    world.google.add("POST", "/token", oauth_error("invalid_grant"))
    assert backfill.run(5, world.config(), world.deps()) == backfill.EXIT_RECONNECT
    assert world.fhir_transport.requests == []
    assert json.loads(capsys.readouterr().out)["outcome"] == "reconnect-needed"


def test_a_failure_exits_1_and_reports_the_ranges_already_done(world, capsys):
    world.clock.now = utc("2026-02-20T12:03:10Z")
    world.google.add("POST", "/token", token_response())
    world.google.add("POST", ":rollUp", rollup_page([]), rollup_page([]), oauth_error("x", 500))
    assert backfill.run(30, world.config(), world.deps()) == backfill.EXIT_FAILED
    report = json.loads(capsys.readouterr().out)
    assert (report["outcome"], report["ranges_completed"]) == ("google-unavailable", 2)


@pytest.mark.parametrize(
    "argv", [[], ["--days", "0"], ["--days", "-3"], ["--days", "367"], ["--days", "two"]]
)
def test_bad_arguments_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        backfill.parse_args(argv)
    assert caught.value.code == 2


def test_days_is_parsed():
    assert backfill.parse_args(["--days", "14"]).days == 14


def test_main_reports_configuration_errors_by_name_only(monkeypatch, capsys):
    for name in ("GCP_PROJECT_ID", "PATIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert backfill.main(["--days", "1"]) == backfill.EXIT_USAGE
    assert "GCP_PROJECT_ID" in capsys.readouterr().err


def test_main_contains_a_bug_and_logs_its_type_only(world, monkeypatch, capsys, caplog):
    monkeypatch.setattr(backfill.ServiceConfig, "from_env", classmethod(lambda cls: world.config()))
    # Never Application Default Credentials in a test: they are absent in CI.
    monkeypatch.setattr(backfill, "default_dependencies", lambda config: world.deps())

    def broken(*_a, **_k):
        raise KeyError("PB-SYN-0001")

    monkeypatch.setattr(backfill, "run", broken)
    assert backfill.main(["--days", "1"]) == backfill.EXIT_FAILED
    output = capsys.readouterr()
    assert [r.getMessage() for r in caplog.records] == ["backfill failed: KeyError"]
    for text in (output.out, output.err, caplog.text):
        assert "PB-SYN-0001" not in text
        assert "Traceback" not in text
