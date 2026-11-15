"""The command line and its configuration: errors, exit codes, dry run, what is logged."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest
from pulsebridge_gcp.testing import json_response
from pulsebridge_seed.cli import EXIT_FAILED, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from pulsebridge_seed.config import ALWAYS, FHIR, ConfigError, SeedConfig
from pulsebridge_seed.generator import generate
from pulsebridge_seed.logs import JsonFormatter
from pulsebridge_seed.testing import (
    ENV,
    NOW,
    PATIENT_ID,
    StoreFake,
    deps_for,
    fresh_store,
    patient_search,
)


@pytest.fixture
def env(monkeypatch):
    for name in list(ALWAYS) + list(FHIR) + ["SYNTHETIC_SEED", "BATCH_WINDOWS"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


# ---- configuration ------------------------------------------------------------------------------


def test_missing_variables_are_named_without_values():
    with pytest.raises(ConfigError) as caught:
        SeedConfig.from_env({"PATIENT_ID": PATIENT_ID})
    message = str(caught.value)
    for name in ("TIME_ZONE", "GCP_PROJECT_ID", "HEALTHCARE_LOCATION", "FHIR_STORE_ID"):
        assert name in message
    assert PATIENT_ID not in message


def test_a_dry_run_needs_no_fhir_configuration():
    config = SeedConfig.from_env(
        {"PATIENT_ID": PATIENT_ID, "TIME_ZONE": "Europe/Berlin"}, need_fhir=False
    )
    assert config.fhir is None


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"PATIENT_ID": "PB-0001"}, "PATIENT_ID must start with SYN-"),
        ({"PATIENT_ID": "SYN-"}, "PATIENT_ID must start with SYN-"),
        ({"PATIENT_ID": "SYN-a|b"}, "PATIENT_ID may hold only"),
        ({"TIME_ZONE": "UTC-5"}, "TIME_ZONE must be an IANA zone"),
        ({"TIME_ZONE": "Etc/GMT+5"}, "TIME_ZONE must be an IANA zone"),
        ({"TIME_ZONE": "Mars/Olympus"}, "TIME_ZONE is not a known"),
        ({"SYNTHETIC_SEED": "-1"}, "SYNTHETIC_SEED must be a whole number"),
        ({"SYNTHETIC_SEED": str(2**63)}, "SYNTHETIC_SEED must be between"),
        ({"BATCH_WINDOWS": "0"}, "BATCH_WINDOWS must be between"),
        ({"FINAL_AFTER_MINUTES": "7"}, "FINAL_AFTER_MINUTES must be a multiple of 5"),
    ],
)
def test_invalid_values_are_refused_with_the_variable_named(changes, named):
    values = dict(ENV) | changes
    with pytest.raises(ConfigError, match=named) as caught:
        SeedConfig.from_env(values)
    for value in changes.values():
        if len(value) > 4:  # longer than the "SYN-" the message itself names
            assert value not in str(caught.value)


def test_seed_flag_overrides_the_variable():
    config = SeedConfig.from_env(dict(ENV) | {"SYNTHETIC_SEED": "5"}, seed_override="99")
    assert config.seed == 99


# ---- command line -------------------------------------------------------------------------------


def test_config_error_exits_2(env, capsys):
    env.setenv("PATIENT_ID", "REAL-0001")
    assert main(["--days", "1"], deps=deps_for(StoreFake())) == EXIT_USAGE
    assert "PATIENT_ID must start with SYN-" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--days", "0"],
        ["--days", "367"],
        ["--days", "1", "--hours", "2"],
        ["--days", "1", "--until", "2026-03-10T10:00:00"],
        ["--hours", "x"],
    ],
)
def test_bad_arguments_exit_2(env, argv):
    with pytest.raises(SystemExit) as caught:
        main(argv, deps=deps_for(StoreFake()))
    assert caught.value.code == EXIT_USAGE


def test_until_later_than_now_is_refused(env, capsys):
    store = StoreFake()
    later = (NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert main(["--days", "1", "--until", later], deps=deps_for(store)) == EXIT_USAGE
    assert store.requests == []


def test_dry_run_prints_counts_and_makes_no_requests(env, capsys):
    for name in FHIR:
        env.delenv(name)
    store = StoreFake()
    argv = ["--days", "2", "--dry-run", "--until", "2026-03-10T05:00:00Z"]
    assert main(argv, deps=deps_for(store)) == EXIT_OK
    assert store.requests == []
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "dry-run"
    assert report["windows_in_range"] == 576
    assert report["windows_generated"] + report["windows_absent"] == 576
    assert report["windows_written"] == 0


def test_hours_and_until_set_the_range(env, capsys):
    store = fresh_store()
    code = main(["--hours", "3", "--until", "2026-03-10T06:07:00-05:00"], deps=deps_for(store))
    assert code == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["range"] == {"start": "2026-03-10T08:05:00Z", "end": "2026-03-10T11:05:00Z"}
    assert report["windows_in_range"] == 36


def test_refusal_exits_4(env):
    store = StoreFake()
    store.add("GET", "/fhir/Patient", patient_search({"resourceType": "Patient", "id": "p"}))
    assert main(["--hours", "1"], deps=deps_for(store)) == EXIT_REFUSED


def test_fhir_failure_exits_1(env):
    store = fresh_store()
    store.bundle_errors = [json_response(403, {})]
    assert main(["--hours", "1"], deps=deps_for(store)) == EXIT_FAILED


def test_logs_and_report_carry_no_values_or_patient_identifier(env, capsys, caplog):
    """A run with retries, a successful seeding and a refusal: no log line or report contains
    the patient identifier, the seed or any generated heart-rate value."""
    env.setenv("SYNTHETIC_SEED", "424242")
    caplog.set_level(logging.INFO)
    store = fresh_store()
    store.bundle_errors = [json_response(503, {}), None, json_response(429, {})]
    until = "2026-03-10T05:00:00Z"
    assert main(["--hours", "6", "--until", until], deps=deps_for(store)) == EXIT_OK
    refused = StoreFake()
    refused.add("GET", "/fhir/Patient", patient_search({"resourceType": "Patient", "id": "p"}))
    assert main(["--hours", "1"], deps=deps_for(refused)) == EXIT_REFUSED

    formatter = JsonFormatter()
    lines = [formatter.format(r) for r in caplog.records]
    captured = capsys.readouterr()
    text = "\n".join(lines) + captured.out + captured.err
    assert any('"event": "fhir-retry"' in line for line in lines)
    assert any('"outcome": "refused"' in line for line in lines)
    assert PATIENT_ID not in text
    assert "424242" not in text
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    end = datetime(2026, 3, 10, 5, tzinfo=UTC)
    items = generate(424242, ZoneInfo("America/New_York"), end - timedelta(hours=6), end).items
    averages = {str(i["heartRate"]["beatsPerMinuteAvg"]) for i in items}
    # Decimal averages such as "63.27" are distinctive; none may appear anywhere.
    distinctive = {a for a in averages if "." in a and len(a) >= 5}
    assert distinctive
    assert not [a for a in distinctive if a in text]


# ---- review follow-ups --------------------------------------------------------------------------


def test_seed_config_itself_refuses_a_non_synthetic_patient():
    from zoneinfo import ZoneInfo

    with pytest.raises(ConfigError, match="must start with SYN-"):
        SeedConfig(patient_id="PB-0001", time_zone=ZoneInfo("UTC"), fhir=None)


@pytest.mark.parametrize("value", ["0", "1001", "99999999999999999999"])
def test_batch_windows_out_of_range_says_between(value):
    with pytest.raises(ConfigError, match="BATCH_WINDOWS must be between 1 and 1000"):
        SeedConfig.from_env(dict(ENV) | {"BATCH_WINDOWS": value})


def test_an_unexpected_error_is_logged_as_a_seed_run_with_counts(env, capsys, caplog):
    caplog.set_level(logging.INFO)
    store = fresh_store()
    store.bundle_errors = [None, KeyError("a value 63.27 inside")]
    assert main(["--hours", "1", "--until", "2026-03-10T05:00:00Z"], deps=deps_for(store)) == (
        EXIT_FAILED
    )
    runs = [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "seed-run"]
    assert len(runs) == 1 and runs[0].levelno == logging.ERROR
    assert runs[0].fields["outcome"] == "bug" and runs[0].fields["reason"] == "KeyError"
    assert runs[0].fields["windows_generated"] > 0
    out = capsys.readouterr()
    assert "63.27" not in out.out + out.err
    assert json.loads(out.out)["outcome"] == "bug"


def test_a_failure_before_the_run_is_still_reported(env, capsys, monkeypatch):
    import pulsebridge_seed.cli as cli

    def no_credentials(_config):
        raise RuntimeError("no default credentials")

    monkeypatch.setattr(cli, "default_dependencies", no_credentials)
    assert main(["--hours", "1"]) == EXIT_FAILED
    report = json.loads(capsys.readouterr().out)
    assert (report["outcome"], report["reason"]) == ("bug", "RuntimeError")
