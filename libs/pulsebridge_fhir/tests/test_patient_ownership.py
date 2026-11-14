"""Who owns the Patient record: ingest creates if absent, setup upserts (review finding 1).

Expected request blocks are written out literally, from the R4 rules, not derived from the code.
"""

from dataclasses import replace
from datetime import date

import pytest
from pulsebridge_fhir import (
    FhirInputError,
    PatientInput,
    Write,
    blood_pressure_transaction,
    build_device,
    build_heart_rate_observation,
    build_ingest_bundle,
    build_patient,
    build_setup_bundle,
    check_complete_patient,
    heart_rate_transaction,
    parse_google_utc_offset,
    patient_setup_transaction,
    window_from_rollup,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_BP,
    SYNTHETIC_BP_SOURCE,
    SYNTHETIC_CUFF,
    SYNTHETIC_DATA_SOURCE,
    SYNTHETIC_PATIENT,
    SYNTHETIC_PATIENT_FROM_PID,
    SYNTHETIC_PATIENT_REF,
    SYNTHETIC_RECORDED,
    SYNTHETIC_ROLLUP,
    SYNTHETIC_WATCH,
)

PATIENT_CREATE = {
    "method": "POST",
    "url": "Patient",
    "ifNoneExist": "identifier=urn:pulsebridge:patient|PB-SYN-0001",
}
PATIENT_UPSERT = {"method": "PUT", "url": "Patient?identifier=urn:pulsebridge:patient|PB-SYN-0001"}
WATCH_CREATE = {
    "method": "POST",
    "url": "Device",
    "ifNoneExist": "identifier=urn:pulsebridge:device|1ce5b303-b8ab-57de-837e-76d881dd2db3",
}
WATCH_UPSERT = {
    "method": "PUT",
    "url": "Device?identifier=urn:pulsebridge:device|1ce5b303-b8ab-57de-837e-76d881dd2db3",
}
CUFF_CREATE = {
    "method": "POST",
    "url": "Device",
    "ifNoneExist": "identifier=urn:pulsebridge:device|95eebf63-cf9c-5007-9bca-7c92e84c69fb",
}


def requests_by_type(bundle):
    out: dict[str, list[dict]] = {}
    for entry in bundle["entry"]:
        out.setdefault(entry["resource"]["resourceType"], []).append(entry["request"])
    return out


def hr_ingest(patient=SYNTHETIC_PATIENT_REF):
    return heart_rate_transaction(
        SYNTHETIC_ROLLUP,
        patient=patient,
        device=SYNTHETIC_WATCH,
        data_source=SYNTHETIC_DATA_SOURCE,
        tz=parse_google_utc_offset("-18000s"),
        recorded=SYNTHETIC_RECORDED,
        complete_before=None,
    )


def bp_ingest(patient=SYNTHETIC_PATIENT_FROM_PID):
    return blood_pressure_transaction(
        SYNTHETIC_BP,
        patient=patient,
        device=SYNTHETIC_CUFF,
        source=SYNTHETIC_BP_SOURCE,
        recorded=SYNTHETIC_RECORDED,
    )


# --- Ingest: conditional create, never PUT ------------------------------------------------------


def test_heart_rate_ingest_creates_patient_and_device_if_absent():
    requests = requests_by_type(hr_ingest())
    assert requests["Patient"] == [PATIENT_CREATE]
    assert requests["Device"] == [WATCH_CREATE]


def test_blood_pressure_ingest_creates_patient_and_device_if_absent():
    requests = requests_by_type(bp_ingest())
    assert requests["Patient"] == [PATIENT_CREATE]
    assert requests["Device"] == [CUFF_CREATE]


@pytest.mark.parametrize("bundle", [hr_ingest, bp_ingest])
def test_ingest_bundle_has_no_patient_or_device_put(bundle):
    for entry in bundle()["entry"]:
        if entry["resource"]["resourceType"] in ("Patient", "Device"):
            assert entry["request"]["method"] == "POST"
            assert not entry["request"]["url"].startswith(("Patient?", "Device?"))


def test_ingest_even_with_full_demographics_does_not_upsert():
    """Even when the ingest caller happens to know everything, it must not replace the record."""
    requests = requests_by_type(hr_ingest(patient=SYNTHETIC_PATIENT))
    assert requests["Patient"] == [PATIENT_CREATE]


def test_ifnoneexist_has_no_leading_question_mark():
    # R4 Bundle.entry.request.ifNoneExist: "just the query portion of the URL - what follows the
    # '?' (not including the '?')".
    for entry in hr_ingest()["entry"]:
        if "ifNoneExist" in entry["request"]:
            assert not entry["request"]["ifNoneExist"].startswith("?")
            assert entry["request"]["ifNoneExist"].startswith("identifier=")


def test_observations_still_link_to_the_patient_entry():
    bundle = hr_ingest()
    patient_url = bundle["entry"][0]["fullUrl"]
    subjects = {
        e["resource"]["subject"]["reference"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Observation"
    }
    assert subjects == {patient_url}


@pytest.mark.parametrize("resource", ["patient", "device"])
def test_ingest_bundle_refuses_patient_or_device_as_a_record(resource):
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    smuggled = patient if resource == "patient" else watch
    with pytest.raises(FhirInputError, match="written only by build_setup_bundle"):
        build_ingest_bundle(patient=patient, devices=[watch], records=[smuggled])


def test_ingest_bundle_refuses_wrong_types_in_patient_and_device_slots():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    with pytest.raises(FhirInputError, match="patient must be a Patient"):
        build_ingest_bundle(patient=watch, devices=[], records=[])
    with pytest.raises(FhirInputError, match="device must be a Device"):
        build_ingest_bundle(patient=patient, devices=[patient], records=[])


def test_observation_has_no_create_request():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    obs = build_heart_rate_observation(
        window_from_rollup(SYNTHETIC_ROLLUP[0]),
        patient=patient,
        device=watch,
        tz=parse_google_utc_offset("0s"),
    )
    with pytest.raises(FhirInputError, match="no conditional-create request"):
        obs.request(Write.CREATE_IF_ABSENT)


# --- Setup: the one upsert ------------------------------------------------------------------------


def test_setup_bundle_upserts_full_patient_and_device():
    bundle = patient_setup_transaction(SYNTHETIC_PATIENT, devices=[SYNTHETIC_WATCH])
    assert bundle["type"] == "transaction"
    assert requests_by_type(bundle) == {"Patient": [PATIENT_UPSERT], "Device": [WATCH_UPSERT]}
    patient = bundle["entry"][0]["resource"]
    assert patient["gender"] == "female"
    assert patient["birthDate"] == "1980-04-12"
    assert {i["system"] for i in patient["identifier"]} == {
        "urn:pulsebridge:patient",
        "urn:pulsebridge:google-health-user-id",
    }


def test_setup_refuses_an_incomplete_patient():
    with pytest.raises(FhirInputError, match="missing gender, birthDate"):
        patient_setup_transaction(SYNTHETIC_PATIENT_FROM_PID)
    with pytest.raises(FhirInputError, match="complete patient"):
        build_setup_bundle(patient=build_patient(SYNTHETIC_PATIENT_REF))


def test_ifnoneexist_escaping_matches_conditional_update_escaping():
    tricky = PatientInput(patient_id="PB|1,x$\\")
    built = build_patient(tricky)
    expected_query = "identifier=urn:pulsebridge:patient|PB%5C|1%5C%2Cx%5C%24%5C%5C"
    assert built.create_if_absent == {
        "method": "POST",
        "url": "Patient",
        "ifNoneExist": expected_query,
    }
    assert built.update == {"method": "PUT", "url": f"Patient?{expected_query}"}


# --- Minimal patient and the strict check --------------------------------------------------------


def test_minimal_patient_from_identifier_and_name_only():
    """What v2-to-fhir has from the cuff message: PID-3 and PID-5."""
    patient = build_patient(SYNTHETIC_PATIENT_FROM_PID).resource
    assert patient == {
        "resourceType": "Patient",
        "identifier": [
            {
                "use": "official",
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                            "code": "PT",
                            "display": "Patient external identifier",
                        }
                    ]
                },
                "system": "urn:pulsebridge:patient",
                "value": "PB-SYN-0001",
            }
        ],
        "name": [{"use": "official", "family": "Example", "given": ["Avery"]}],
    }


def test_identifier_only_patient_has_no_name():
    patient = build_patient(SYNTHETIC_PATIENT_REF).resource
    assert set(patient) == {"resourceType", "identifier"}


def test_family_only_name():
    patient = build_patient(PatientInput("PB-1", family="Example")).resource
    assert patient["name"] == [{"use": "official", "family": "Example"}]


def test_complete_patient_passes_strict_check():
    check_complete_patient(build_patient(SYNTHETIC_PATIENT).resource)


@pytest.mark.parametrize(
    ("changes", "missing"),
    [
        ({"gender": None}, "gender"),
        ({"birth_date": None}, "birthDate"),
        ({"family": None}, "an official name with both a family and a given name"),
        ({"given": ()}, "an official name with both a family and a given name"),
        ({"family": None, "given": ()}, "a name with use 'official'"),
    ],
)
def test_strict_check_names_what_is_missing(changes, missing):
    patient = build_patient(replace(SYNTHETIC_PATIENT, **changes)).resource
    with pytest.raises(FhirInputError, match=f"missing .*{missing}"):
        check_complete_patient(patient)


@pytest.mark.parametrize(
    ("mutate", "missing"),
    [
        (lambda p: p["name"][0].update(use="usual"), "a name with use 'official'"),
        (lambda p: p["identifier"][0].pop("type"), "a PT-typed identifier"),
        (lambda p: p.update(birthDate="1980"), "birthDate"),
        (lambda p: p.update(gender="F"), "gender"),
    ],
)
def test_strict_check_on_a_resource_read_back(mutate, missing):
    """T15 checks a Patient read from the store, which our builder may not have written."""
    patient = build_patient(SYNTHETIC_PATIENT).resource
    mutate(patient)
    with pytest.raises(FhirInputError, match=f"missing .*{missing}"):
        check_complete_patient(patient)


def test_strict_check_rejects_non_patient():
    with pytest.raises(FhirInputError, match="expected a Patient"):
        check_complete_patient({"resourceType": "Device"})


def test_patient_input_still_validates_what_is_given():
    with pytest.raises(FhirInputError, match="gender 'F'"):
        PatientInput("PB-1", gender="F")
    with pytest.raises(FhirInputError, match="birth_date must be a date"):
        PatientInput("PB-1", birth_date="1980-04-12")
    PatientInput("PB-1", birth_date=date(1980, 4, 12))
