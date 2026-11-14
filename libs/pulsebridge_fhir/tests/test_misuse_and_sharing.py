"""Re-review fixes: requests cannot be forged (m3), strict check gaps (m4), sharing (m5).

Expected request blocks and resources are written out literally.
"""

import copy
import json
from dataclasses import replace

import pytest
from pulsebridge_fhir import (
    BuiltResource,
    FhirInputError,
    PatientInput,
    Write,
    blood_pressure_transaction,
    build_device,
    build_ingest_bundle,
    build_patient,
    build_setup_bundle,
    check_complete_patient,
    patient_for_sharing,
    request_for,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_BP,
    SYNTHETIC_BP_SOURCE,
    SYNTHETIC_CUFF,
    SYNTHETIC_PATIENT,
    SYNTHETIC_PATIENT_REF,
    SYNTHETIC_RECORDED,
    SYNTHETIC_WATCH,
)

PATIENT_CREATE = {
    "method": "POST",
    "url": "Patient",
    "ifNoneExist": "identifier=urn:pulsebridge:patient|PB-SYN-0001",
}
WATCH_CREATE = {
    "method": "POST",
    "url": "Device",
    "ifNoneExist": "identifier=urn:pulsebridge:device|1ce5b303-b8ab-57de-837e-76d881dd2db3",
}
PT_CODING = {
    "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
    "code": "PT",
    "display": "Patient external identifier",
}


def built_pair():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    return patient, build_device(SYNTHETIC_WATCH, patient=patient)


def requests(bundle):
    return [e["request"] for e in bundle["entry"]]


# --- m3: the reviewer's five cases ---------------------------------------------------------------


def test_case1_requests_are_not_fields_that_can_be_swapped():
    """Before: replace(patient, create_if_absent=patient.update) made an ingest PUT."""
    patient, watch = built_pair()
    with pytest.raises(TypeError):
        replace(patient, create_if_absent=patient.update)
    with pytest.raises(AttributeError):
        patient.create_if_absent = patient.update
    bundle = build_ingest_bundle(patient=patient, devices=[watch], records=[])
    assert requests(bundle) == [PATIENT_CREATE, WATCH_CREATE]


def test_case2_mutating_a_returned_request_changes_nothing():
    patient, watch = built_pair()
    patient.create_if_absent["method"] = "PUT"
    patient.create_if_absent["url"] = "Patient?identifier=urn:pulsebridge:patient|PB-SYN-0001"
    patient.request(Write.CREATE_IF_ABSENT).pop("ifNoneExist")
    bundle = build_ingest_bundle(patient=patient, devices=[watch], records=[])
    assert requests(bundle) == [PATIENT_CREATE, WATCH_CREATE]


def test_case3_record_request_always_names_its_own_type_and_identifier():
    """A hand-built record gets the request its resource implies, whatever its key says."""
    patient, watch = built_pair()
    obs = {
        "resourceType": "Observation",
        "identifier": [{"system": "urn:pulsebridge:observation", "value": "obs-0001"}],
        "subject": patient.reference(),
    }
    record = BuiltResource(resource=obs, full_url="urn:uuid:0000-hand", key="Patient/PB-SYN-0001")
    bundle = build_ingest_bundle(patient=patient, devices=[watch], records=[record])
    assert bundle["entry"][2]["request"] == {
        "method": "PUT",
        "url": "Observation?identifier=urn:pulsebridge:observation|obs-0001",
    }


def test_case3_record_without_its_own_identifier_is_rejected():
    patient, watch = built_pair()
    obs = {
        "resourceType": "Observation",
        # Identifier in the Patient system: the request would have targeted a Patient search.
        "identifier": [{"system": "urn:pulsebridge:patient", "value": "PB-SYN-0001"}],
    }
    record = BuiltResource(resource=obs, full_url="urn:uuid:0000-hand", key="x")
    with pytest.raises(
        FhirInputError,
        match="Observation: expected exactly one identifier with system "
        "urn:pulsebridge:observation, found 0",
    ):
        build_ingest_bundle(patient=patient, devices=[watch], records=[record])


def test_case3_record_retyped_as_patient_is_rejected():
    patient, watch = built_pair()
    smuggled = BuiltResource(
        resource=copy.deepcopy(patient.resource), full_url="urn:uuid:0000-hand", key="x"
    )
    with pytest.raises(FhirInputError, match="written only by build_setup_bundle"):
        build_ingest_bundle(patient=patient, devices=[watch], records=[smuggled])


def test_case4_setup_url_follows_the_resource_identifier():
    """Before: setup accepted a Patient whose update URL named another identifier."""
    patient = build_patient(SYNTHETIC_PATIENT)
    patient.resource["identifier"][0]["value"] = "PB-SYN-0002"  # key still says PB-SYN-0001
    bundle = build_setup_bundle(patient=patient)
    assert requests(bundle) == [
        {"method": "PUT", "url": "Patient?identifier=urn:pulsebridge:patient|PB-SYN-0002"}
    ]


def test_case4_patient_with_two_pulsebridge_identifiers_is_rejected():
    patient = build_patient(SYNTHETIC_PATIENT)
    patient.resource["identifier"].append({"system": "urn:pulsebridge:patient", "value": "PB-X"})
    with pytest.raises(FhirInputError, match="expected exactly one identifier .* found 2"):
        build_setup_bundle(patient=patient)


@pytest.mark.parametrize("slot", ["records", "devices", "patient"])
def test_case5_plain_dict_is_a_clear_error_in_ingest(slot):
    patient, watch = built_pair()
    args = {"patient": patient, "devices": [watch], "records": []}
    if slot == "patient":
        args["patient"] = patient.resource
    else:
        args[slot] = [{"resourceType": "Observation"}]
    with pytest.raises(FhirInputError, match="must be a BuiltResource .* got dict"):
        build_ingest_bundle(**args)


def test_case5_plain_dict_is_a_clear_error_in_setup():
    with pytest.raises(FhirInputError, match="patient must be a BuiltResource .* got dict"):
        build_setup_bundle(patient=build_patient(SYNTHETIC_PATIENT).resource)


def test_request_for_refuses_unknown_types_and_bad_provenance_ids():
    with pytest.raises(FhirInputError, match="no write rule for resourceType 'Encounter'"):
        request_for({"resourceType": "Encounter"}, Write.UPDATE)
    with pytest.raises(FhirInputError, match="Provenance: id 'a/b' is not a valid FHIR id"):
        request_for({"resourceType": "Provenance", "id": "a/b"}, Write.UPDATE)
    with pytest.raises(FhirInputError, match="Provenance has no conditional-create request"):
        request_for({"resourceType": "Provenance", "id": "p1"}, Write.CREATE_IF_ABSENT)


# --- m4: strict check gaps -----------------------------------------------------------------------


def full_patient():
    return copy.deepcopy(build_patient(SYNTHETIC_PATIENT).resource)


def test_family_and_given_on_different_official_names_is_incomplete():
    patient = full_patient()
    patient["name"] = [
        {"use": "official", "family": "Example"},
        {"use": "official", "given": ["Avery"]},
    ]
    with pytest.raises(FhirInputError, match="an official name with both a family and a given"):
        check_complete_patient(patient)


def test_blank_given_name_is_incomplete():
    patient = full_patient()
    patient["name"][0]["given"] = ["  "]
    with pytest.raises(FhirInputError, match="an official name with both a family and a given"):
        check_complete_patient(patient)


@pytest.mark.parametrize(
    "birth_date",
    ["1980-13-45", "1980-02-30", "١٩٨٠-٠٤-١٢", "1980-4-12"],
)
def test_birth_date_must_be_a_real_ascii_date(birth_date):
    patient = full_patient()
    patient["birthDate"] = birth_date
    with pytest.raises(FhirInputError, match=r"missing birthDate \(a real date, YYYY-MM-DD\)"):
        check_complete_patient(patient)


def test_identifier_typed_other_than_pt_is_not_enough():
    patient = full_patient()
    patient["identifier"][0]["type"]["coding"][0]["code"] = "MR"  # medical record number
    with pytest.raises(FhirInputError, match="missing a PT-typed identifier"):
        check_complete_patient(patient)


def test_complete_patient_still_passes():
    check_complete_patient(full_patient())


# --- m5: partial names on ingest, and the shared Patient ------------------------------------------


@pytest.mark.parametrize(
    ("patient", "expected_name"),
    [
        (PatientInput("PB-SYN-0001"), None),  # PID-5 empty
        (PatientInput("PB-SYN-0001", family="Example"), [{"use": "official", "family": "Example"}]),
        (PatientInput("PB-SYN-0001", given=("Avery",)), [{"use": "official", "given": ["Avery"]}]),
    ],
)
def test_ingest_accepts_identifier_with_partial_or_absent_name(patient, expected_name):
    bundle = blood_pressure_transaction(
        SYNTHETIC_BP,
        patient=patient,
        device=SYNTHETIC_CUFF,
        source=SYNTHETIC_BP_SOURCE,
        recorded=SYNTHETIC_RECORDED,
    )
    entry = bundle["entry"][0]
    assert entry["request"] == PATIENT_CREATE
    assert entry["resource"].get("name") == expected_name


def stored_patient():
    """A Patient as it might come back from the store: with id, meta and extra elements."""
    patient = full_patient()
    patient["id"] = "server-assigned-id"
    patient["meta"] = {"versionId": "3", "lastUpdated": "2026-01-02T00:00:00Z"}
    patient["telecom"] = [{"system": "phone", "value": "555-0100"}]
    patient["name"].append({"use": "nickname", "given": ["Ave"]})
    return patient


def test_patient_for_sharing_keeps_only_what_the_clinic_needs():
    assert patient_for_sharing(stored_patient()) == {
        "resourceType": "Patient",
        "identifier": [
            {
                "use": "official",
                "type": {"coding": [PT_CODING]},
                "system": "urn:pulsebridge:patient",
                "value": "PB-SYN-0001",
            }
        ],
        "name": [{"use": "official", "family": "Example", "given": ["Avery"]}],
        "gender": "female",
        "birthDate": "1980-04-12",
    }


def test_patient_for_sharing_drops_google_id_id_and_meta():
    shared = json.dumps(patient_for_sharing(stored_patient()))
    assert "google-health-user-id" not in shared
    assert "synthetic-health-user-0001" not in shared
    assert "server-assigned-id" not in shared
    assert "meta" not in json.loads(shared)
    assert "telecom" not in json.loads(shared)


def test_patient_for_sharing_does_not_alias_the_stored_record():
    stored = stored_patient()
    shared = patient_for_sharing(stored)
    shared["identifier"][0]["type"]["coding"][0]["code"] = "changed"
    assert stored["identifier"][0]["type"]["coding"][0]["code"] == "PT"


def test_patient_for_sharing_refuses_an_incomplete_patient():
    minimal = build_patient(SYNTHETIC_PATIENT_REF).resource
    with pytest.raises(FhirInputError, match="complete patient: missing"):
        patient_for_sharing(minimal)


def test_blood_pressure_example_asserts_no_recording_method():
    """The ORU^R01 message does not say how the reading was taken."""
    from pulsebridge_fhir.examples import build_examples

    prov = build_examples()["provenance-blood-pressure.json"]
    performer = prov["agent"][0]
    assert performer["type"]["coding"][0]["code"] == "performer"
    assert "role" not in performer
