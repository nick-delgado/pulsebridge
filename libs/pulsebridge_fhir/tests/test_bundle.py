"""Transaction Bundle (T06 description: "a transaction Bundle using conditional updates")."""

import pytest
from pulsebridge_fhir import (
    FhirInputError,
    GoogleDataSource,
    build_device,
    build_patient,
    build_transaction_bundle,
    heart_rate_transaction,
    parse_google_utc_offset,
)
from pulsebridge_fhir.examples import (
    SYNTHETIC_PATIENT,
    SYNTHETIC_RECORDED,
    SYNTHETIC_ROLLUP,
    SYNTHETIC_WATCH,
)


def transaction(points=SYNTHETIC_ROLLUP):
    return heart_rate_transaction(
        points,
        patient=SYNTHETIC_PATIENT,
        device=SYNTHETIC_WATCH,
        data_source=GoogleDataSource(),
        tz=parse_google_utc_offset("-18000s"),
        recorded=SYNTHETIC_RECORDED,
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


def test_is_a_transaction_of_puts():
    bundle = transaction()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "transaction"
    assert all(e["request"]["method"] == "PUT" for e in bundle["entry"])


def test_contains_patient_device_and_observation_plus_provenance_per_window():
    types = [e["resource"]["resourceType"] for e in transaction()["entry"]]
    assert types == ["Patient", "Device", "Observation", "Provenance", "Observation", "Provenance"]


def test_conditional_entries_use_identifier_search_and_have_no_id():
    for entry in transaction()["entry"]:
        resource, url = entry["resource"], entry["request"]["url"]
        if resource["resourceType"] == "Provenance":
            assert url == f"Provenance/{resource['id']}"
            assert entry["fullUrl"] == f"urn:uuid:{resource['id']}"
        else:
            # Conditional update: the server finds the resource by identifier, so the body carries
            # no id (a body id that disagreed with the match would be an error).
            assert url.startswith(f"{resource['resourceType']}?identifier=")
            assert "id" not in resource
            system, value = url.split("=", 1)[1].split("|")
            assert {"system": system, "value": value}.items() <= next(
                i for i in resource["identifier"] if i["system"] == system
            ).items()


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


def test_dangling_reference_is_rejected():
    patient = build_patient(SYNTHETIC_PATIENT)
    watch = build_device(SYNTHETIC_WATCH, patient=patient)
    with pytest.raises(FhirInputError, match="not in the bundle"):
        build_transaction_bundle([watch])  # the Device refers to a Patient that is missing


def test_empty_bundle_is_rejected():
    with pytest.raises(FhirInputError, match="no resources"):
        build_transaction_bundle([])


def test_entry_is_a_copy():
    patient = build_patient(SYNTHETIC_PATIENT)
    bundle = build_transaction_bundle([patient])
    bundle["entry"][0]["resource"]["gender"] = "other"
    assert patient.resource["gender"] == "female"
