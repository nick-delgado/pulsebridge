"""Transaction Bundles: many writes that succeed or fail together.

A Bundle of ``type: transaction`` is posted to the FHIR base URL; the server applies every entry
or none (R4 http.html#transaction). Entries refer to each other by their ``fullUrl``
(``urn:uuid:…``), and the server rewrites those references to the real ids when it commits.

There are exactly two ways to build one, and they differ in who may change the Patient:

- :func:`build_ingest_bundle` (``health-sync``, ``v2-to-fhir``): Patient and Device are
  **conditional creates** (``POST`` + ``ifNoneExist``). Normally the setup step has already
  written them, and they are left untouched; the create is only a safety net for a missing
  record. Only
  Observations and Provenance are written with ``PUT``. An ingest Bundle cannot replace a
  Patient or Device: passing one as a record is an error.
- :func:`build_setup_bundle` (the connect script T11, the seeder T09): the one place that writes
  the full Patient with a **conditional update** (``PUT``), after checking it is complete.

See README.md, "Who owns the Patient record".
"""

import copy
from collections.abc import Iterable, Iterator
from datetime import datetime

from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.inputs import BloodPressureReading, DeviceInput, PatientInput, SourceInfo
from pulsebridge_fhir.resources import (
    BuiltResource,
    Write,
    build_blood_pressure_observation,
    build_device,
    build_patient,
    build_provenance,
    check_complete_patient,
    request_for,
)

INGEST_RECORD_TYPES = ("Observation", "Provenance")


def _references(node: object) -> Iterator[str]:
    """Every ``reference`` string anywhere inside a resource."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == "reference" and isinstance(value, str):
                yield value
            else:
                yield from _references(value)
    elif isinstance(node, list):
        for item in node:
            yield from _references(item)


def _request_target(request: dict) -> tuple[str, ...]:
    """What a request writes: the PUT URL, or the POST type plus its ifNoneExist query."""
    if request["method"] == "POST":
        return ("POST", request["url"], request.get("ifNoneExist", ""))
    return ("PUT", request["url"])


def _transaction(entries: list[tuple[BuiltResource, Write]]) -> dict:
    """Assemble and check a transaction Bundle.

    Every request is derived here from the entry's own resource (:func:`request_for`), never taken
    from the caller, so a request cannot name another resource type, another identifier or another
    method than the write mode this function was given.

    Strict checks, because the server would reject the whole transaction anyway and its error
    would be harder to trace back:

    - no two entries share a ``fullUrl`` or write the same target (two writes to one resource in
      one transaction are an error in R4);
    - every ``urn:uuid`` reference points at an entry in this Bundle (a dangling bundle-local
      reference cannot be resolved by the server).

    The Bundle has no ``id`` or ``timestamp``, so the same input gives byte-identical output.
    """
    if not entries:
        raise FhirInputError("bundle: no resources to write")
    full_urls: set[str] = set()
    targets: set[tuple[str, ...]] = set()
    requests: list[dict] = []
    for built, write in entries:
        if built.full_url in full_urls:
            raise FhirInputError(f"bundle: duplicate entry {built.full_url}")
        request = request_for(built.resource, write)
        target = _request_target(request)
        if target in targets:
            raise FhirInputError(f"bundle: two entries write {' '.join(target)}")
        full_urls.add(built.full_url)
        targets.add(target)
        requests.append(request)
    for built, _ in entries:
        for ref in _references(built.resource):
            if ref.startswith("urn:uuid:") and ref not in full_urls:
                raise FhirInputError(
                    f"bundle: {built.resource_type} {built.full_url} refers to {ref}, "
                    "which is not in the bundle"
                )
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"fullUrl": built.full_url, "resource": copy.deepcopy(built.resource), "request": req}
            for (built, _), req in zip(entries, requests, strict=True)
        ],
    }


def _built(value: object, role: str) -> BuiltResource:
    """``value`` as a BuiltResource with a resource dict, or a clear error."""
    if not isinstance(value, BuiltResource):
        raise FhirInputError(
            f"bundle: {role} must be a BuiltResource from a pulsebridge_fhir builder, "
            f"got {type(value).__name__}"
        )
    if not isinstance(value.resource, dict) or not isinstance(
        value.resource.get("resourceType"), str
    ):
        raise FhirInputError(f"bundle: {role} has no resource with a resourceType")
    if not isinstance(value.full_url, str) or not value.full_url.startswith("urn:uuid:"):
        raise FhirInputError(f"bundle: {role} has no urn:uuid fullUrl")
    return value


def _check_type(built: object, resource_type: str, role: str) -> BuiltResource:
    built = _built(built, role)
    if built.resource_type != resource_type:
        raise FhirInputError(f"bundle: {role} must be a {resource_type}, got {built.resource_type}")
    return built


def build_ingest_bundle(
    *,
    patient: BuiltResource,
    devices: Iterable[BuiltResource],
    records: Iterable[BuiltResource],
) -> dict:
    """Transaction for new readings. Never replaces a Patient or Device.

    - ``patient`` and each of ``devices``: conditional create (``POST`` + ``ifNoneExist``).
    - ``records``: Observations (conditional update) and their Provenance (update by id). A
      Patient or Device among them is rejected, so it cannot slip in as a ``PUT``.
    """
    patient = _check_type(patient, "Patient", "patient")
    devices = [_check_type(device, "Device", "device") for device in devices]
    records = [_built(record, "record") for record in records]
    for record in records:
        if record.resource_type not in INGEST_RECORD_TYPES:
            raise FhirInputError(
                f"bundle: an ingest bundle writes only {' and '.join(INGEST_RECORD_TYPES)} "
                f"records, got {record.resource_type}; the Patient record is written only by "
                "build_setup_bundle"
            )
    return _transaction(
        [(patient, Write.CREATE_IF_ABSENT)]
        + [(device, Write.CREATE_IF_ABSENT) for device in devices]
        + [(record, Write.UPDATE) for record in records]
    )


def build_setup_bundle(*, patient: BuiltResource, devices: Iterable[BuiltResource] = ()) -> dict:
    """Transaction that writes the full Patient record, and optionally its Devices, with ``PUT``.

    Only the setup step calls this (the connect script T11 or the seeder T09). The Patient must be
    complete (:func:`check_complete_patient`): this write replaces whatever is stored.
    """
    patient = _check_type(patient, "Patient", "patient")
    check_complete_patient(patient.resource)
    devices = [_check_type(device, "Device", "device") for device in devices]
    return _transaction([(patient, Write.UPDATE)] + [(device, Write.UPDATE) for device in devices])


def patient_setup_transaction(
    patient: PatientInput, *, devices: Iterable[DeviceInput] = ()
) -> dict:
    """Setup step: the full Patient (demographics and Google Health user id) as an upsert."""
    built_patient = build_patient(patient)
    return build_setup_bundle(
        patient=built_patient,
        devices=[build_device(device, patient=built_patient) for device in devices],
    )


def blood_pressure_transaction(
    reading: BloodPressureReading,
    *,
    patient: PatientInput,
    device: DeviceInput,
    source: SourceInfo,
    recorded: datetime,
    assembler: str = "PulseBridge v2-to-fhir",
    status: str = "final",
) -> dict:
    """``v2-to-fhir`` (T12): one cuff reading as an ingest Bundle.

    ``patient`` needs only PID-3 (``patient_id``) and, if present, PID-5 (``family``, ``given``).
    The Patient and Device are created if absent and never overwritten.
    """
    built_patient = build_patient(patient)
    built_device = build_device(device, patient=built_patient)
    observation = build_blood_pressure_observation(
        reading, patient=built_patient, device=built_device, status=status
    )
    provenance = build_provenance(
        target=observation,
        device=built_device,
        source=source,
        recorded=recorded,
        assembler=assembler,
    )
    return build_ingest_bundle(
        patient=built_patient, devices=[built_device], records=[observation, provenance]
    )
