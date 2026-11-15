"""The reason list (label-safe, one sentence each) and configuration from the environment."""

import re

import pytest
from pulsebridge_gcp import check_labels
from v2_to_fhir.config import ConfigError, ServiceConfig
from v2_to_fhir.reasons import SENTENCES, Outcome, Reason, sentence

# The documented label value limit (spike S5), restated here rather than imported, so a change
# to the shared check cannot silently loosen this test.
LABEL_VALUE = re.compile(r"[a-z0-9_-]{1,63}", re.ASCII)


@pytest.mark.parametrize("reason", list(Reason))
def test_every_reason_is_a_valid_label_value_with_a_sentence(reason):
    assert LABEL_VALUE.fullmatch(reason.value)
    assert len(reason.value.encode("utf-8")) <= 63
    check_labels({"application_outcome": "error", "reason": reason.value})
    text = SENTENCES[reason]
    assert text.endswith(".") and text[0].isupper()
    assert sentence(reason.value) == text


def test_outcome_values_are_the_two_d7_states():
    assert [o.value for o in Outcome] == ["accepted", "error"]


def test_every_sentence_belongs_to_a_reason():
    assert set(SENTENCES) == set(Reason)


def test_reason_codes_are_unique():
    assert len({r.value for r in Reason}) == len(Reason)


def test_unknown_reason_has_no_sentence():
    with pytest.raises(ValueError):
        sentence("no-such-reason")


def test_out_of_range_sentence_states_the_real_bounds():
    assert sentence("out-of-range") == (
        "A value is outside the plausible range (systolic 50 to 300, diastolic 20 to 200 mm[Hg])."
    )


ENV = {
    "GCP_PROJECT_ID": "demo-project",
    "HEALTHCARE_LOCATION": "us-central1",
    "HEALTHCARE_DATASET_ID": "demo",
    "FHIR_STORE_ID": "fhir",
    "HL7V2_STORE_ID": "main",
    "PATIENT_ID": "SYN-000123",
}


def test_config_from_env():
    config = ServiceConfig.from_env(ENV)
    assert config.project_id == "demo-project"
    assert config.hl7v2_store_id == "main"
    assert config.fhir_store_id == "fhir"
    assert config.mapping.patient_id == "SYN-000123"
    assert config.mapping.patient_assigning_authority is None
    with_authority = ServiceConfig.from_env({**ENV, "PATIENT_ASSIGNING_AUTHORITY": "PULSEBRIDGE"})
    assert with_authority.mapping.patient_assigning_authority == "PULSEBRIDGE"


def test_missing_variables_are_named_and_values_are_not_shown():
    env = {**ENV, "PATIENT_ID": "  ", "FHIR_STORE_ID": ""}
    del env["GCP_PROJECT_ID"]
    with pytest.raises(ConfigError) as caught:
        ServiceConfig.from_env(env)
    assert str(caught.value) == (
        "missing environment variables: GCP_PROJECT_ID, FHIR_STORE_ID, PATIENT_ID"
    )
    assert "demo" not in str(caught.value)


def test_the_readme_documents_every_reason_and_variable():
    from pathlib import Path

    from v2_to_fhir.config import VARIABLES

    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    for reason in Reason:
        assert f"`{reason.value}`" in readme, reason
    for name in VARIABLES:
        assert f"`{name}`" in readme, name
