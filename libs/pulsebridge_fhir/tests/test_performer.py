"""T33: heart-rate and blood-pressure Observations name the Patient as performer (self-measured).

R4 Observation.performer: Reference(Practitioner | PractitionerRole | Organization | CareTeam |
Patient | RelatedPerson), "who was responsible for asserting the observed value as 'true'".
"""

import pytest
from pulsebridge_fhir.examples import build_examples


def _observations(resource: dict) -> list[dict]:
    if resource["resourceType"] == "Observation":
        return [resource]
    if resource["resourceType"] == "Bundle":
        return [
            e["resource"]
            for e in resource["entry"]
            if e["resource"]["resourceType"] == "Observation"
        ]
    return []


EXAMPLES = build_examples()
WITH_OBSERVATIONS = [name for name, r in EXAMPLES.items() if _observations(r)]


def test_both_kinds_of_observation_are_covered():
    codes = {o["code"]["coding"][0]["code"] for r in EXAMPLES.values() for o in _observations(r)}
    assert codes == {"8867-4", "85354-9"}  # heart rate, blood-pressure panel


@pytest.mark.parametrize("name", WITH_OBSERVATIONS)
def test_performer_is_exactly_the_subject_patient(name):
    for obs in _observations(EXAMPLES[name]):
        assert obs["performer"] == [obs["subject"]]


@pytest.mark.parametrize("name", [n for n in WITH_OBSERVATIONS if n.startswith("bundle-")])
def test_performer_resolves_to_the_patient_entry_in_the_bundle(name):
    bundle = EXAMPLES[name]
    patients = [e["fullUrl"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"]
    for obs in _observations(bundle):
        assert [p["reference"] for p in obs["performer"]] == patients
