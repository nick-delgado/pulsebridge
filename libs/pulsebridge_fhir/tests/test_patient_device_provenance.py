"""Patient, Device and Provenance (T06 AC 5), including what OpenEMR needs (spike S1)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pulsebridge_fhir import (
    DeviceInput,
    FhirInputError,
    GoogleDataSource,
    build_device,
    build_heart_rate_observation,
    build_patient,
    build_provenance,
    rollup_source,
    window_from_rollup,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_BP_SOURCE,
    SYNTHETIC_PATIENT,
    SYNTHETIC_RECORDED,
    SYNTHETIC_ROLLUP,
    SYNTHETIC_WATCH,
)

PT = {
    "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
    "code": "PT",
    "display": "Patient external identifier",
}

# --- Patient -------------------------------------------------------------------------------------


def test_patient_meets_openemr_requirements():
    """S1: PT-typed identifier, name with use official, gender, birth date."""
    patient = build_patient(SYNTHETIC_PATIENT).resource
    pt = [i for i in patient["identifier"] if PT in i.get("type", {}).get("coding", [])]
    assert len(pt) == 1
    assert pt[0]["value"] == "PB-SYN-0001"
    assert patient["name"] == [{"use": "official", "family": "Example", "given": ["Avery"]}]
    assert patient["gender"] == "female"
    assert patient["birthDate"] == "1980-04-12"


def test_patient_carries_google_health_user_id():
    patient = build_patient(SYNTHETIC_PATIENT).resource
    google = [
        i for i in patient["identifier"] if i["system"] == "urn:pulsebridge:google-health-user-id"
    ]
    assert google == [
        {
            "use": "secondary",
            "system": "urn:pulsebridge:google-health-user-id",
            "value": "synthetic-health-user-0001",
        }
    ]


def test_google_id_is_optional_and_not_the_key():
    without = build_patient(replace(SYNTHETIC_PATIENT, google_health_user_id=None))
    with_id = build_patient(SYNTHETIC_PATIENT)
    assert len(without.resource["identifier"]) == 1
    assert (
        without.request_url
        == with_id.request_url
        == ("Patient?identifier=urn:pulsebridge:patient|PB-SYN-0001")
    )


# --- Device --------------------------------------------------------------------------------------


def test_device_identifier_is_derived_and_stable():
    patient = build_patient(SYNTHETIC_PATIENT)
    a = build_device(SYNTHETIC_WATCH, patient=patient)
    b = build_device(DeviceInput(display_name="Pixel Watch 4"), patient=patient)
    assert a.key == b.key
    assert a.resource["identifier"] == [{"system": "urn:pulsebridge:device", "value": a.key}]
    assert a.request_url == f"Device?identifier=urn:pulsebridge:device|{a.key}"
    assert a.resource["deviceName"] == [{"name": "Pixel Watch 4", "type": "user-friendly-name"}]
    assert a.resource["patient"] == {"reference": patient.full_url}


def test_device_identifier_differs_by_patient_and_name():
    patient = build_patient(SYNTHETIC_PATIENT)
    other = build_patient(replace(SYNTHETIC_PATIENT, patient_id="PB-SYN-0002"))
    base = build_device(SYNTHETIC_WATCH, patient=patient).key
    assert build_device(SYNTHETIC_WATCH, patient=other).key != base
    assert build_device(DeviceInput("Pixel Watch 3"), patient=patient).key != base


# --- Provenance ----------------------------------------------------------------------------------


@pytest.fixture
def hr_and_provenance():
    patient = build_patient(SYNTHETIC_PATIENT)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    window = window_from_rollup(SYNTHETIC_ROLLUP[0])
    obs = build_heart_rate_observation(window, patient=patient, device=watch, tz=UTC)
    prov = build_provenance(
        target=obs,
        device=watch,
        source=rollup_source(window, GoogleDataSource()),
        recorded=SYNTHETIC_RECORDED,
        assembler="PulseBridge health-sync",
    )
    return watch, obs, prov


def agent(prov, type_code):
    found = [a for a in prov["agent"] if a["type"]["coding"][0]["code"] == type_code]
    assert len(found) == 1, f"expected one {type_code} agent"
    assert found[0]["type"]["coding"][0]["system"] == (
        "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
    )
    return found[0]


def test_provenance_targets_observation_and_names_device(hr_and_provenance):
    watch, obs, prov = hr_and_provenance
    p = prov.resource
    assert p["target"] == [{"reference": obs.full_url}]
    assert agent(p, "performer")["who"] == {"reference": watch.full_url}
    assert agent(p, "assembler")["who"] == {"display": "PulseBridge health-sync"}
    assert p["recorded"] == "2026-01-01T14:20:03+00:00"


def test_provenance_records_platform_and_recording_method(hr_and_provenance):
    _, _, prov = hr_and_provenance
    p = prov.resource
    custodian = agent(p, "custodian")["who"]
    assert custodian["identifier"] == {
        "system": "urn:pulsebridge:source-platform",
        "value": "FITBIT",
    }
    assert p["activity"]["coding"] == [
        {"system": "urn:pulsebridge:recording-method", "code": "PASSIVELY_MEASURED"}
    ]


def test_provenance_entity_points_at_source_record(hr_and_provenance):
    _, _, prov = hr_and_provenance
    entity = prov.resource["entity"]
    assert len(entity) == 1
    assert entity[0]["role"] == "source"
    assert entity[0]["what"]["identifier"] == {
        "system": "urn:pulsebridge:source-record:google-health-heart-rate-rollup",
        "value": "2026-01-01T14:05:00Z/2026-01-01T14:10:00Z",
    }


def test_provenance_for_hl7v2_points_at_store_message():
    from pulsebridge_fhir import build_blood_pressure_observation
    from pulsebridge_fhir.examples import SYNTHETIC_BP, SYNTHETIC_CUFF

    patient = build_patient(SYNTHETIC_PATIENT)
    cuff = build_device(SYNTHETIC_CUFF, patient=patient)
    obs = build_blood_pressure_observation(SYNTHETIC_BP, patient=patient, device=cuff)
    prov = build_provenance(
        target=obs,
        device=cuff,
        source=SYNTHETIC_BP_SOURCE,
        recorded=SYNTHETIC_RECORDED,
        assembler="PulseBridge v2-to-fhir",
    ).resource
    assert prov["entity"][0]["what"]["identifier"]["value"] == (
        "hl7V2Stores/example-store/messages/synthetic-message-0001"
    )
    assert agent(prov, "custodian")["who"]["identifier"]["value"] == "HL7V2_STORE"


def test_provenance_without_recording_method_has_no_activity(hr_and_provenance):
    watch, obs, _ = hr_and_provenance
    prov = build_provenance(
        target=obs,
        device=watch,
        source=replace(SYNTHETIC_BP_SOURCE, recording_method=None),
        recorded=SYNTHETIC_RECORDED,
        assembler="x",
    ).resource
    assert "activity" not in prov


def test_provenance_id_is_deterministic_and_written_by_id(hr_and_provenance):
    watch, obs, prov = hr_and_provenance
    again = build_provenance(
        target=obs,
        device=watch,
        source=SYNTHETIC_BP_SOURCE,
        recorded=datetime(2027, 1, 1, tzinfo=UTC),  # a later sync
        assembler="PulseBridge health-sync",
    )
    assert again.key == prov.key
    assert prov.resource["id"] == prov.key
    assert prov.full_url == f"urn:uuid:{prov.key}"
    assert prov.request_url == f"Provenance/{prov.key}"
    assert prov.key != obs.key


def test_provenance_rejects_wrong_target_and_naive_time(hr_and_provenance):
    watch, obs, _ = hr_and_provenance
    with pytest.raises(FhirInputError, match="target must be an Observation"):
        build_provenance(
            target=watch, device=watch, source=SYNTHETIC_BP_SOURCE,
            recorded=SYNTHETIC_RECORDED, assembler="x",
        )  # fmt: skip
    with pytest.raises(FhirInputError, match="recorded"):
        build_provenance(
            target=obs, device=watch, source=SYNTHETIC_BP_SOURCE,
            recorded=datetime(2026, 1, 1), assembler="x",
        )  # fmt: skip
