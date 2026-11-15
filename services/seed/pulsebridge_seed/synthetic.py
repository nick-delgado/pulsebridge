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
5. **Every resource carries the security label ``HTEST``** in ``meta.security``: HL7 v3
   ActReason ``HTEST`` "test health data", the code HL7 defines for data that is for testing and
   not about a real person. R4's security-labels page lists it among the purpose-of-use labels,
   so ``meta.security`` (bound to "All Security Labels") is its place, not ``meta.tag``. It is the
   one marker a search can use: ``Observation?_security=<system>|HTEST``. It is added to T06's
   output here because T06's builders take no ``meta``; it changes no request. An earlier draft
   used ``meta.tag``; a label in either place is reported. A label never makes a stored
   Patient acceptable on its own: the content rule in :func:`refusal_reason` decides.

   ``patient_for_sharing`` (T15) sends OpenEMR only the identifier, name, gender and birth date,
   so after a share the synthetic name is the only marker the clinic sees.

T06 limits what can be said: the Provenance ``entity`` source record type is always
``google-health-heart-rate-rollup`` (``heart_rate_transaction`` fixes it), which here describes
the *shape* of the source window, not that Google sent it. The other markers make the origin
plain; a ``record_type`` argument on ``heart_rate_transaction`` would be the clean fix (a
follow-up for the library, not changed by this task).
"""

from __future__ import annotations

import copy
from datetime import date

from pulsebridge_fhir import (
    DeviceInput,
    FhirInputError,
    GoogleDataSource,
    PatientInput,
    check_complete_patient,
    codes,
)

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
TEST_DATA_LABEL = {
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


def has_test_label(resource: dict) -> bool:
    """``HTEST`` in ``meta.security`` (where the seeder puts it) or ``meta.tag`` (an earlier
    draft). Information for the report only: it never makes a Patient acceptable by itself
    (:func:`refusal_reason`)."""
    meta = resource.get("meta") if isinstance(resource, dict) else None
    if not isinstance(meta, dict):
        return False
    for element in ("security", "tag"):
        codings = meta.get(element)
        if isinstance(codings, list) and any(
            isinstance(c, dict)
            and c.get("system") == TEST_DATA_LABEL["system"]
            and c.get("code") == TEST_DATA_LABEL["code"]
            for c in codings
        ):
            return True
    return False


def _is_synthetic_name(name: object) -> bool:
    return (
        isinstance(name, dict)
        and name.get("family") == FAMILY
        and name.get("given") == list(GIVEN)
        and set(name) <= {"use", "family", "given"}
    )


def has_synthetic_name(resource: dict) -> bool:
    """At least one name, and **every** name is the synthetic one: a Patient with a real name
    beside a synthetic one is not ours."""
    names = resource.get("name") if isinstance(resource, dict) else None
    return isinstance(names, list) and bool(names) and all(_is_synthetic_name(n) for n in names)


# What a stored Patient may hold and still be one the seeder may write over: the server's own
# ``id`` and ``meta``, the SYN- identifier and, at most, the synthetic name and the seeder's own
# invented gender and birth date. Anything else (telecom, address, another identifier, a
# different gender or birth date) looks like a real person's record.
_ALLOWED_ELEMENTS = {"resourceType", "id", "meta", "identifier", "name", "gender", "birthDate"}

NOT_SYNTHETIC = "patient-not-synthetic"
NAME_NOT_SYNTHETIC = "patient-name-not-synthetic"


def _identifiers_are_ours(resource: dict, patient_id: str) -> bool:
    """Every identifier a ``SYN-`` value in PulseBridge's patient system, and one of them the
    configured patient id (a search answer about another ``SYN-`` Patient is not ours)."""
    identifiers = resource.get("identifier")
    if not isinstance(identifiers, list) or not identifiers:
        return False
    for identifier in identifiers:
        if not (
            isinstance(identifier, dict)
            and identifier.get("system") == codes.PB_PATIENT_ID
            and isinstance(identifier.get("value"), str)
            and identifier["value"].startswith(PATIENT_ID_PREFIX)
        ):
            return False
    return any(i["value"] == patient_id for i in identifiers)


def refusal_reason(resource: dict, patient_id: str) -> str | None:
    """``None`` if the stored Patient is one the seeder may write over, else the reason.

    The same content rule applies to **every** Patient, labelled ``HTEST`` or not. A label can
    outlive the content it described: if another writer (for example the connect script, run
    by mistake with a ``SYN-`` id) replaces the record and the store keeps the old
    ``meta.security``, a real name and a Google Health identifier would sit under our label.
    So the label alone proves nothing; it is reported as information only
    (:func:`has_test_label`).

    - every identifier a ``SYN-`` value in ``urn:pulsebridge:patient``, one of them
      ``patient_id``;
    - gender and birth date absent or the seeder's own (``other``, 1980-01-01);
    - nothing else beyond the server-added ``id`` and ``meta``;
    - every name (if any) the synthetic name. When this is the only problem the reason is
      ``patient-name-not-synthetic``: usually the cuff simulator's PID-5 was not set to the
      synthetic name, and ``v2-to-fhir`` created the Patient with it (README, "Safety").

    This accepts the identifier-only Patient ``v2-to-fhir`` or ``health-sync`` creates by
    conditional create when an ingest reaches the store before the seeder (D11).
    """
    if not isinstance(resource, dict) or resource.get("resourceType") != "Patient":
        return NOT_SYNTHETIC
    if not set(resource) <= _ALLOWED_ELEMENTS or not _identifiers_are_ours(resource, patient_id):
        return NOT_SYNTHETIC
    if resource.get("gender", GENDER) != GENDER:
        return NOT_SYNTHETIC
    if resource.get("birthDate", BIRTH_DATE.isoformat()) != BIRTH_DATE.isoformat():
        return NOT_SYNTHETIC
    if "name" in resource and not has_synthetic_name(resource):
        return NAME_NOT_SYNTHETIC
    return None


def is_synthetic_patient(resource: dict, patient_id: str) -> bool:
    return refusal_reason(resource, patient_id) is None


def is_complete(resource: dict) -> bool:
    """Whether the stored Patient already has the full record (T06 ``check_complete_patient``):
    then the setup Bundle need not be sent again (D11: written once)."""
    try:
        check_complete_patient(resource)
    except FhirInputError:
        return False
    return True


def mark_synthetic(bundle: dict) -> dict:
    """A copy of ``bundle`` with the ``HTEST`` security label on every entry's resource."""
    out = copy.deepcopy(bundle)
    for entry in out.get("entry", []):
        resource = entry["resource"]
        labels = resource.setdefault("meta", {}).setdefault("security", [])
        if not any(
            isinstance(c, dict)
            and c.get("system") == TEST_DATA_LABEL["system"]
            and c.get("code") == TEST_DATA_LABEL["code"]
            for c in labels
        ):
            labels.append(dict(TEST_DATA_LABEL))
    return out
