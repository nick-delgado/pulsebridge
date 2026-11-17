"""pulsebridge_gcp.testing.select_elements: ``_elements`` as Cloud Healthcare applies it.

Each case is a live search seen on dev on 17 November 2026 (T39), replayed on a synthetic
heart-rate Observation. The fake FHIR stores of health-sync and the seeder use this, so a
wrong ``_elements`` loses the same fields in tests as on the real store.
"""

import pytest
from pulsebridge_gcp.testing import select_elements

OBSERVATION = {
    "resourceType": "Observation",
    "id": "obs-1",
    "meta": {"versionId": "1", "lastUpdated": "2026-01-15T13:06:00Z"},
    "identifier": [{"system": "urn:pulsebridge:observation", "value": "k1"}],
    "status": "final",
    "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
    "subject": {"reference": "Patient/p-1"},
    "effectivePeriod": {"start": "2026-01-15T13:00:00Z", "end": "2026-01-15T13:05:00Z"},
    "valueQuantity": {"value": 61, "unit": "/min"},
    "component": [],
}


@pytest.mark.parametrize(
    ("elements", "returned"),
    [
        (
            "identifier,status,effectivePeriod,valueQuantity,component",
            {"component", "id", "identifier", "meta", "resourceType", "status"},
        ),
        (
            "identifier,status,effective,value,component",
            {
                "component",
                "effectivePeriod",
                "id",
                "identifier",
                "meta",
                "resourceType",
                "status",
                "valueQuantity",
            },
        ),
        ("effective[x],value[x]", {"id", "meta", "resourceType"}),
        ("Observation.effective,Observation.value", {"id", "meta", "resourceType"}),
        ("id", {"id", "meta", "resourceType"}),
    ],
    ids=["typed-names", "base-names", "bracketed", "qualified", "id-only"],
)
def test_elements_as_cloud_healthcare_returned_them(elements, returned):
    assert set(select_elements(OBSERVATION, elements)) == returned


def test_values_are_returned_unchanged_and_the_input_is_not_modified():
    selected = select_elements(OBSERVATION, "effective,value")
    assert selected["effectivePeriod"] == OBSERVATION["effectivePeriod"]
    assert selected["valueQuantity"] == {"value": 61, "unit": "/min"}
    assert "status" in OBSERVATION


def test_no_elements_parameter_returns_the_whole_resource():
    assert select_elements(OBSERVATION, None) == OBSERVATION
