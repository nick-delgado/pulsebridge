"""Flags and environment variables: required values, defaults, validation before anything runs."""

from datetime import date

import pytest
from connect_google_health import cli
from connect_google_health.oauth import DEFAULT_SCOPE

ENV = {
    "GCP_PROJECT_ID": "demo-project-1",
    "HEALTHCARE_LOCATION": "us-central1",
    "HEALTHCARE_DATASET_ID": "pulsebridge",
    "FHIR_STORE_ID": "fhir",
    "PATIENT_ID": "PB-SYN-0001",
    "PATIENT_FAMILY_NAME": "Syntheticfamily",
    "PATIENT_GIVEN_NAME": "Syntheticgiven",
    "PATIENT_GENDER": "female",
    "PATIENT_BIRTH_DATE": "1980-04-12",
}


def test_everything_from_the_environment_with_documented_defaults():
    config = cli.load_config([], ENV)
    assert config.project == "demo-project-1"
    assert config.prefix == "pb"
    assert config.secret("refresh-token") == "pb-google-health-refresh-token"
    assert config.scope == DEFAULT_SCOPE
    assert (config.port, config.paste, config.open_browser) == (0, False, True)
    assert config.timeout_seconds == 300
    assert not config.reset_client and not config.disable_previous
    assert config.fhir == cli.FhirTarget("us-central1", "pulsebridge", "fhir")
    assert config.demographics.birth_date == date(1980, 4, 12)
    assert config.demographics.given == "Syntheticgiven"


def test_flags_override_the_environment():
    config = cli.load_config(
        ["--project", "other-project", "--name-prefix", "dev", "--scope", "s1 s2", "--port", "8085",
         "--paste", "--no-browser", "--reset-client", "--disable-previous", "--timeout", "60",
         "--gender", "male"],
        ENV,
    )  # fmt: skip
    assert config.project == "other-project"
    assert config.secret("client-id") == "dev-google-health-client-id"
    assert config.scope == "s1 s2"
    assert (config.port, config.paste, config.open_browser) == (8085, True, False)
    assert config.reset_client and config.disable_previous
    assert config.timeout_seconds == 60
    assert config.demographics.gender == "male"


def test_skip_patient_needs_no_fhir_or_demographics():
    config = cli.load_config(["--skip-patient"], {"GCP_PROJECT_ID": "demo-project-1"})
    assert config.fhir is None and config.demographics is None


@pytest.mark.parametrize(
    "missing",
    ["GCP_PROJECT_ID", "FHIR_STORE_ID", "PATIENT_ID", "PATIENT_FAMILY_NAME", "PATIENT_BIRTH_DATE"],
)
def test_required_values(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(cli.UsageError) as caught:
        cli.load_config([], env)
    assert missing in str(caught.value)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("GCP_PROJECT_ID", "Not_A_Project", "not a valid GCP project ID"),
        ("PATIENT_GENDER", "Syntheticwrong", "must be one of"),
        ("PATIENT_BIRTH_DATE", "12/04/1980", "YYYY-MM-DD"),
        ("PATIENT_BIRTH_DATE", "2999-01-01", "past"),
        ("FHIR_STORE_ID", "fhir/../other", "not valid"),
        ("HEALTHCARE_LOCATION", "US Central", "not valid"),
        ("PULSEBRIDGE_NAME_PREFIX", "PB", "name_prefix"),
        ("GOOGLE_HEALTH_SCOPE", "two  spaces", "scope"),
        ("PATIENT_ID", " PB-SYN-0001", "Patient configuration"),  # T06 refuses padding
    ],
)
def test_invalid_values_are_refused_without_echoing_them(name, value, message):
    with pytest.raises(cli.UsageError) as caught:
        cli.load_config([], {**ENV, name: value})
    assert message in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize("args", [["--port", "70000"], ["--timeout", "5"]])
def test_numeric_limits(args):
    with pytest.raises(cli.UsageError):
        cli.load_config(args, ENV)


def test_main_reports_usage_errors_with_exit_2(capsys):
    assert cli.main([], {}) == cli.EXIT_USAGE
    assert "GCP_PROJECT_ID is required" in capsys.readouterr().err


def test_main_help_exits_0(capsys):
    assert cli.main(["--help"], {}) == 0
    out = capsys.readouterr().out
    for flag in ("--paste", "--skip-patient", "--disable-previous", "[PATIENT_BIRTH_DATE]"):
        assert flag in out
