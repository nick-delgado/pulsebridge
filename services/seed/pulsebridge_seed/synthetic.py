"""What marks the seeder's data as synthetic, so nobody can mistake it for a real watch.

Five markers, from the most visible to the most machine-readable:

1. **The patient identifier starts with ``SYN-``** (configuration, checked at start-up). A
   deployment with real data uses another identifier, so synthetic and real Patients can never
   be the same record (README, "Safety").
2. **The Patient's name says so**: family ``Synthetic``, given ``Demo``. Demographics are fixed
   and invented (FHIR ``gender`` ``other``, birth date 1980-01-01): T06's setup Bundle requires
   a complete Patient (official name, gender, birth date), and OpenEMR needs the same (spike S1).
3. **The Device's name says so**: ``PulseBridge synthetic heart-rate generator``, manufacturer
   ``PulseBridge (synthetic data)``. No real hardware is named.
4. **Each Provenance says so** (T06 ``build_provenance``, the only fields a caller controls):
   - ``agent`` custodian: source platform ``PULSEBRIDGE_SYNTHETIC`` (where health-sync has
     Google's ``FITBIT``). The custodian "holds the original": here the generator, which can
     recreate every window exactly from the seed;
   - ``agent`` performer ``role``: recording method ``SYNTHETIC`` (where a watch has
     ``PASSIVELY_MEASURED``), so the Device's part is "generated", not "measured";
   - ``agent`` assembler: ``PulseBridge seed (synthetic data)``.
5. **Every resource carries the tag ``HTEST``** in ``meta.tag``: HL7 v3 ActReason ``HTEST``
   "test health data", the code HL7 defines for data that is for testing and not about a real
   person. ``meta.tag``'s binding is an example (R4 "Common Tags"), so any code is allowed
   there. It is the one marker a search can use: ``Observation?_tag=<system>|HTEST``. It is
   added to T06's output here because T06's builders take no ``meta``; it changes no request.

T06 limits what can be said: the Provenance ``entity`` source record type is always
``google-health-heart-rate-rollup`` (``heart_rate_transaction`` fixes it), which here describes
the *shape* of the source window, not that Google sent it. The other markers make the origin
plain; a ``record_type`` argument on ``heart_rate_transaction`` would be the clean fix (a
follow-up for the library, not changed by this task).
"""

from __future__ import annotations

import copy
from datetime import date

from pulsebridge_fhir import DeviceInput, GoogleDataSource, PatientInput

PATIENT_ID_PREFIX = "SYN-"
FAMILY = "Synthetic"
GIVEN = ("Demo",)
GENDER = "other"
BIRTH_DATE = date(1980, 1, 1)
DEVICE_NAME = "PulseBridge synthetic heart-rate generator"
DEVICE_MANUFACTURER = "PulseBridge (synthetic data)"
PLATFORM = "PULSEBRIDGE_SYNTHETIC"
RECORDING_METHOD = "SYNTHETIC"
ASSEMBLER = "PulseBridge seed (synthetic data)"

# HL7 v3 ActReason HTEST, "test health data" (terminology.hl7.org CodeSystem v3-ActReason).
TEST_DATA_TAG = {
    "system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
    "code": "HTEST",
    "display": "test health data",
}


def full_patient(patient_id: str) -> PatientInput:
    """The complete synthetic Patient for the setup Bundle (D11: written once, with ``PUT``)."""
    return PatientInput(
        patient_id, family=FAMILY, given=GIVEN, gender=GENDER, birth_date=BIRTH_DATE
    )


def ingest_patient(patient_id: str) -> PatientInput:
    """The Patient as ingest Bundles carry it: identifier only, conditional create (D11)."""
    return PatientInput(patient_id)


def device() -> DeviceInput:
    # The same name in the setup and ingest Bundles: T06 derives the Device key from the patient
    # and this name, so any difference would create a second Device.
    return DeviceInput(DEVICE_NAME, manufacturer=DEVICE_MANUFACTURER)


def data_source() -> GoogleDataSource:
    return GoogleDataSource(platform=PLATFORM, recording_method=RECORDING_METHOD)


def has_test_tag(resource: dict) -> bool:
    meta = resource.get("meta") if isinstance(resource, dict) else None
    tags = meta.get("tag") if isinstance(meta, dict) else None
    return isinstance(tags, list) and any(
        isinstance(t, dict)
        and t.get("system") == TEST_DATA_TAG["system"]
        and t.get("code") == TEST_DATA_TAG["code"]
        for t in tags
    )


def has_synthetic_name(resource: dict) -> bool:
    names = resource.get("name") if isinstance(resource, dict) else None
    return isinstance(names, list) and any(
        isinstance(n, dict) and n.get("family") == FAMILY and n.get("given") == list(GIVEN)
        for n in names
    )


def is_synthetic_patient(resource: dict) -> bool:
    """A stored Patient the seeder wrote: tagged ``HTEST``, or carrying the synthetic name (in
    case the store does not keep ``meta.tag``, which is unverified on Cloud Healthcare)."""
    return has_test_tag(resource) or has_synthetic_name(resource)


def mark_synthetic(bundle: dict) -> dict:
    """A copy of ``bundle`` with the ``HTEST`` tag on every entry's resource."""
    out = copy.deepcopy(bundle)
    for entry in out.get("entry", []):
        resource = entry["resource"]
        meta = resource.setdefault("meta", {})
        tags = meta.setdefault("tag", [])
        if not has_test_tag(resource):
            tags.append(dict(TEST_DATA_TAG))
    return out
