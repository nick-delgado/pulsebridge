"""Transaction Bundles: structure and integrity checks."""

from dataclasses import replace

import pytest
from pulsebridge_fhir import (
    FhirInputError,
    build_device,
    build_heart_rate_observation,
    build_ingest_bundle,
    build_patient,
    heart_rate_transaction,
    parse_google_utc_offset,
    window_from_rollup,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_DATA_SOURCE,
    SYNTHETIC_PATIENT_REF,
    SYNTHETIC_RECORDED,
    SYNTHETIC_ROLLUP,
    SYNTHETIC_WATCH,
)


def transaction(points=SYNTHETIC_ROLLUP):
    return heart_rate_transaction(
        points,
        patient=SYNTHETIC_PATIENT_REF,
        device=SYNTHETIC_WATCH,
        data_source=SYNTHETIC_DATA_SOURCE,
        tz=parse_google_utc_offset("-18000s"),
        recorded=SYNTHETIC_RECORDED,
        complete_before=None,
    )


def refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference":
                yield v
            else:
                yield from refs(v)
    elif isinstance(node, list):
        for item in node:
            yield from refs(item)


def test_is_a_transaction():
    bundle = transaction()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "transaction"


def test_contains_patient_device_and_observation_plus_provenance_per_window():
    types = [e["resource"]["resourceType"] for e in transaction()["entry"]]
    assert types == ["Patient", "Device", "Observation", "Provenance", "Observation", "Provenance"]


def test_observation_and_provenance_entries_are_puts_without_body_id_for_observations():
    for entry in transaction()["entry"]:
        resource, request = entry["resource"], entry["request"]
        if resource["resourceType"] == "Provenance":
            assert request == {"method": "PUT", "url": f"Provenance/{resource['id']}"}
            assert entry["fullUrl"] == f"urn:uuid:{resource['id']}"
        elif resource["resourceType"] == "Observation":
            # Conditional update: the server finds the resource by identifier, so the body carries
            # no id (a body id that disagreed with the match would be an error).
            assert request["method"] == "PUT"
            assert request["url"].startswith("Observation?identifier=urn:pulsebridge:observation|")
            assert "id" not in resource


def test_every_reference_resolves_inside_the_bundle():
    bundle = transaction()
    full_urls = {e["fullUrl"] for e in bundle["entry"]}
    all_refs = list(refs(bundle))
    assert all_refs, "expected references"
    for ref in all_refs:
        assert ref.startswith("urn:uuid:")
        assert ref in full_urls


def test_duplicate_window_is_rejected():
    with pytest.raises(FhirInputError, match="duplicate entry"):
        transaction([SYNTHETIC_ROLLUP[0], SYNTHETIC_ROLLUP[0]])


def test_two_entries_writing_the_same_target_are_rejected():
    """Different fullUrls, same request target: still two writes to one resource."""
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    obs = build_heart_rate_observation(
        window_from_rollup(SYNTHETIC_ROLLUP[0]),
        patient=patient,
        device=watch,
        tz=parse_google_utc_offset("0s"),
    )
    twin = replace(obs, full_url="urn:uuid:00000000-0000-5000-8000-000000000000")
    with pytest.raises(FhirInputError, match="two entries write PUT Observation"):
        build_ingest_bundle(patient=patient, devices=[watch], records=[obs, twin])


def test_two_conditional_creates_of_the_same_device_are_rejected():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    twin = replace(watch, full_url="urn:uuid:00000000-0000-5000-8000-000000000001")
    with pytest.raises(FhirInputError, match="two entries write POST Device"):
        build_ingest_bundle(patient=patient, devices=[watch, twin], records=[])


def test_dangling_reference_is_rejected():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    other = build_patient(replace(SYNTHETIC_PATIENT_REF, patient_id="PB-SYN-0002"))
    with pytest.raises(FhirInputError, match="not in the bundle"):
        # The Device refers to a Patient that is not in this Bundle.
        build_ingest_bundle(patient=other, devices=[watch], records=[])


def test_entry_is_a_copy():
    patient = build_patient(SYNTHETIC_PATIENT_REF)
    bundle = build_ingest_bundle(patient=patient, devices=[], records=[])
    bundle["entry"][0]["resource"]["identifier"][0]["value"] = "changed"
    assert patient.resource["identifier"][0]["value"] == "PB-SYN-0001"
