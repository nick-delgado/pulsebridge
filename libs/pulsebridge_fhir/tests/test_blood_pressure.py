"""Blood-pressure panel Observation (T06 description, AC 1)."""

from dataclasses import replace

import pytest
from pulsebridge_fhir import build_blood_pressure_observation, build_device, build_patient
from pulsebridge_fhir.examples import SYNTHETIC_BP, SYNTHETIC_CUFF, SYNTHETIC_PATIENT


@pytest.fixture
def built():
    patient = build_patient(SYNTHETIC_PATIENT)
    cuff = build_device(SYNTHETIC_CUFF, patient=patient)
    return patient, cuff


def bp(built, reading=SYNTHETIC_BP):
    patient, cuff = built
    return build_blood_pressure_observation(reading, patient=patient, device=cuff).resource


def test_meets_base_vital_signs_profile(built, vital_signs_profile):
    vital_signs_profile(bp(built))


def test_bp_profile_panel_shape(built):
    obs = bp(built)
    # bp profile: code 85354-9, no top-level valueQuantity, one systolic and one diastolic
    # component, each in mm[Hg].
    assert obs["code"]["coding"][0]["system"] == "http://loinc.org"
    assert obs["code"]["coding"][0]["code"] == "85354-9"
    assert "valueQuantity" not in obs
    codes = [c["code"]["coding"][0]["code"] for c in obs["component"]]
    assert sorted(codes) == ["8462-4", "8480-6"]
    for comp in obs["component"]:
        assert comp["code"]["coding"][0]["system"] == "http://loinc.org"
        assert comp["valueQuantity"]["code"] == "mm[Hg]"
        assert comp["valueQuantity"]["system"] == "http://unitsofmeasure.org"
    assert obs["meta"]["profile"] == ["http://hl7.org/fhir/StructureDefinition/bp"]


def test_values_go_to_the_right_component(built):
    obs = bp(built, replace(SYNTHETIC_BP, systolic=133, diastolic=88))
    by_code = {
        c["code"]["coding"][0]["code"]: c["valueQuantity"]["value"] for c in obs["component"]
    }
    assert by_code == {"8480-6": 133, "8462-4": 88}


def test_required_elements_and_time(built):
    patient, cuff = built
    obs = bp(built)
    assert obs["status"] == "final"
    assert obs["category"][0]["coding"][0]["code"] == "vital-signs"
    assert obs["subject"] == {"reference": patient.full_url}
    assert obs["device"] == {"reference": cuff.full_url}
    # A single reading has one moment; the offset the reading carried is kept.
    assert obs["effectiveDateTime"] == "2026-01-01T09:30:00-05:00"
    assert "effectivePeriod" not in obs
