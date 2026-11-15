"""Start-up validation and the mounted secret files."""

import logging
from datetime import timedelta
from pathlib import Path

import pytest
from health_sync.app import check_startup
from health_sync.config import ConfigError, ServiceConfig
from health_sync.credentials import MAX_SECRET_BYTES, SecretsMissing, load_credentials
from health_sync.testing import CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, formatted_logs, leaked


def env(world, **overrides):
    values = {
        "GCP_PROJECT_ID": "demo-project",
        "HEALTHCARE_LOCATION": "us-central1",
        "HEALTHCARE_DATASET_ID": "demo",
        "FHIR_STORE_ID": "fhir",
        "PATIENT_ID": "PB-SYN-0001",
        "TIME_ZONE": "America/New_York",
        "GOOGLE_HEALTH_CLIENT_ID_FILE": world.paths.client_id,
        "GOOGLE_HEALTH_CLIENT_SECRET_FILE": world.paths.client_secret,
        "GOOGLE_HEALTH_REFRESH_TOKEN_FILE": world.paths.refresh_token,
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def test_from_env_with_defaults(world):
    config = ServiceConfig.from_env(env(world))
    assert config.time_zone.key == "America/New_York"
    assert (config.lookback, config.final_after, config.sync_interval) == (
        timedelta(minutes=180),
        timedelta(minutes=60),
        timedelta(minutes=15),
    )
    assert (config.batch_windows, config.max_attempts, config.run_deadline) == (100, 4, 150.0)
    assert config.scope.endswith("googlehealth.health_metrics_and_measurements.readonly")


def test_optional_values_are_read(world):
    config = ServiceConfig.from_env(
        env(world, LOOKBACK_MINUTES="360", BATCH_WINDOWS="50", FINAL_AFTER_MINUTES="30")
    )
    assert (config.lookback, config.batch_windows, config.final_after) == (
        timedelta(minutes=360),
        50,
        timedelta(minutes=30),
    )


@pytest.mark.parametrize(
    "missing", ["PATIENT_ID", "TIME_ZONE", "GOOGLE_HEALTH_REFRESH_TOKEN_FILE", "FHIR_STORE_ID"]
)
def test_a_missing_variable_is_named(world, missing):
    with pytest.raises(ConfigError) as caught:
        ServiceConfig.from_env(env(world, **{missing: None}))
    assert missing in str(caught.value)


def test_a_relative_secret_path_is_refused(world):
    with pytest.raises(ConfigError, match="GOOGLE_HEALTH_CLIENT_SECRET_FILE must be an absolute"):
        relative = {"GOOGLE_HEALTH_CLIENT_SECRET_FILE": "secrets/x"}
        ServiceConfig.from_env(env(world, **relative))


@pytest.mark.parametrize("zone", ["UTC-5", "+05:30", "Etc/GMT+5", "GMT-04:00", "Mars/Olympus"])
def test_time_zone_must_be_a_real_iana_zone_not_an_offset(world, zone):
    with pytest.raises(ConfigError, match="TIME_ZONE"):
        ServiceConfig.from_env(env(world, TIME_ZONE=zone))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"LOOKBACK_MINUTES": "60", "FINAL_AFTER_MINUTES": "60"}, "at least FINAL_AFTER"),
        ({"LOOKBACK_MINUTES": "182"}, "multiple of 5"),
        ({"LOOKBACK_MINUTES": "20165"}, "LOOKBACK_MINUTES must be between"),
        ({"BATCH_WINDOWS": "0"}, "BATCH_WINDOWS"),
        ({"BATCH_WINDOWS": "1001"}, "BATCH_WINDOWS"),
        ({"MAX_ATTEMPTS": "x"}, "whole number"),
    ],
)
def test_unsafe_timing_and_bounds_are_refused(world, overrides, message):
    with pytest.raises(ConfigError, match=message):
        ServiceConfig.from_env(env(world, **overrides))


def test_a_lookback_over_one_request_is_refused(world):
    with pytest.raises(ConfigError, match="14 days"):
        world.config(lookback=timedelta(days=14, minutes=5))


def test_the_default_timing_tolerates_missed_runs(world):
    config = ServiceConfig.from_env(env(world))
    spare = config.lookback - config.final_after - config.sync_interval
    assert spare == timedelta(minutes=105)  # README: up to 7 missed runs


def test_check_startup_refuses_a_bad_store_or_patient(world):
    with pytest.raises(ConfigError, match="FHIR store"):
        check_startup(world.config(fhir_store_id="bad/store"))
    with pytest.raises(ConfigError, match="PATIENT_ID"):
        check_startup(world.config(patient_id=" "))


def test_check_startup_warns_about_missing_secret_files_by_name_only(world, caplog):
    world.write_secrets(client_secret=None)
    with caplog.at_level(logging.WARNING):
        check_startup(world.config())  # starts: /status must be able to say "not-configured"
    (record,) = [r for r in caplog.records if getattr(r, "fields", {}).get("event")]
    assert record.fields == {
        "event": "secrets-missing",
        "secrets": ["client-secret"],
        "reason": "secrets-missing:client-secret",
    }
    assert leaked(formatted_logs(caplog.records)) == []


# ---- the secret files ------------------------------------------------------------------------


def test_load_credentials_reads_and_trims_each_file(world):
    creds = load_credentials(world.paths)
    assert creds.secret_values() == (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)
    assert leaked(repr(creds)) == []


@pytest.mark.parametrize(
    "content",
    [b"", b"  \n", b"two words", b"\xff\xfe", b"tab\there", b"a" * (MAX_SECRET_BYTES + 1)],
)
def test_an_unusable_file_is_missing_by_name(world, content):
    Path(world.paths.refresh_token).write_bytes(content)
    with pytest.raises(SecretsMissing) as caught:
        load_credentials(world.paths)
    assert caught.value.names == ["refresh-token"]
    assert leaked(str(caught.value)) == []


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", "secrets-missing:refresh-token"),
        (b" \n", "secrets-missing:refresh-token"),
        (b"two words", "secret-unreadable:refresh-token"),
        (b"\xff\xfe", "secret-unreadable:refresh-token"),
        (b"a" * (MAX_SECRET_BYTES + 1), "secret-unreadable:refresh-token"),
    ],
)
def test_missing_and_unreadable_are_told_apart(world, content, reason):
    Path(world.paths.refresh_token).write_bytes(content)
    with pytest.raises(SecretsMissing) as caught:
        load_credentials(world.paths)
    assert caught.value.reason == reason


def test_a_path_that_cannot_be_read_is_unreadable(world):
    world.write_secrets(refresh_token=None)
    path = Path(world.paths.client_id)
    path.unlink()
    path.mkdir()  # reading a directory fails with an OSError other than "not found"
    with pytest.raises(SecretsMissing) as caught:
        load_credentials(world.paths)
    assert caught.value.reason == "secrets-missing:refresh-token;secret-unreadable:client-id"


def test_several_missing_files_are_all_named(world):
    world.write_secrets(client_id=None, refresh_token=None)
    with pytest.raises(SecretsMissing) as caught:
        load_credentials(world.paths)
    assert caught.value.names == ["client-id", "refresh-token"]
