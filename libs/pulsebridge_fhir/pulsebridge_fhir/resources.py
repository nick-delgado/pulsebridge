"""Builders for Patient, Device, Observation (heart rate, blood pressure) and Provenance.

Each builder returns a :class:`BuiltResource`: the resource as a plain dict, plus what a
transaction Bundle needs to write it idempotently (the bundle-local ``fullUrl`` and the request
URL). See README.md for the design; the comments below name the rule each element follows.

Write strategy, per resource:

==============  ====================================================  ===========================
Resource        Request                                               Key
==============  ====================================================  ===========================
Patient         ``PUT Patient?identifier=urn:pulsebridge:patient|…``  the configured patient id
Device          ``PUT Device?identifier=urn:pulsebridge:device|…``    UUIDv5(patient, device name)
Observation HR  ``PUT Observation?identifier=…|<uuid>``               UUIDv5(patient, 8867-4,
                                                                      window start in UTC)
Observation BP  ``PUT Observation?identifier=…|<uuid>``               UUIDv5(patient, 85354-9,
                                                                      source key)
Provenance      ``PUT Provenance/<uuid>`` (update-as-create)          UUIDv5(target Observation)
==============  ====================================================  ===========================

R4 Provenance has no ``identifier`` element and the Cloud Healthcare API's conditional update
matches on identifier only, so Provenance alone is written by id; that needs the FHIR store's
``enableUpdateCreate`` setting (task T05).
"""

import copy
from dataclasses import dataclass
from datetime import datetime, tzinfo

from pulsebridge_fhir import codes
from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.inputs import (
    OBSERVATION_STATUSES,
    BloodPressureReading,
    DeviceInput,
    HeartRateWindow,
    PatientInput,
    SourceInfo,
)
from pulsebridge_fhir.keys import conditional_url, deterministic_uuid, urn_uuid


@dataclass(frozen=True)
class BuiltResource:
    """A resource plus the information needed to write it in a transaction Bundle.

    - ``full_url``: ``urn:uuid:…``, the resource's identity inside one Bundle. Other entries refer
      to it by this value; the server replaces it with the real ``Type/id`` when it commits
      (R4 bundle.html, "Resolving references in Bundles").
    - ``request_url``: the ``Bundle.entry.request.url`` of the ``PUT``.
    - ``key``: the deterministic key the request matches on (identifier value or resource id).
    """

    resource: dict
    full_url: str
    request_url: str
    key: str

    @property
    def resource_type(self) -> str:
        return self.resource["resourceType"]

    def reference(self) -> dict:
        """A FHIR Reference to this resource, valid inside the same transaction Bundle."""
        return {"reference": self.full_url}

    def bundle_entry(self) -> dict:
        """The ``Bundle.entry`` for this resource; a deep copy, so the source stays unchanged."""
        return {
            "fullUrl": self.full_url,
            "resource": copy.deepcopy(self.resource),
            "request": {"method": "PUT", "url": self.request_url},
        }


def _patient_key(patient: BuiltResource) -> str:
    if patient.resource_type != "Patient":
        raise FhirInputError(f"expected a Patient, got {patient.resource_type}")
    return patient.key


def _require_type(built: BuiltResource, resource_type: str, role: str) -> None:
    if built.resource_type != resource_type:
        raise FhirInputError(f"{role} must be a {resource_type}, got {built.resource_type}")


def fhir_datetime(value: datetime, tz: tzinfo | None = None) -> str:
    """Format an aware datetime as a FHIR ``dateTime`` with an explicit offset.

    With ``tz`` the instant is first shown in that zone, so a UTC time from Google becomes the
    user's local wall time with its offset (``2026-01-01T09:05:00-05:00``). The instant does not
    change, only how it is written; a reader sees the time the user saw on the watch.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise FhirInputError(f"datetime {value!r} has no time zone")
    if tz is not None:
        value = value.astimezone(tz)
    timespec = "seconds" if value.microsecond == 0 else "milliseconds"
    return value.isoformat(timespec=timespec)


def _quantity(value: int | float, ucum_code: str) -> dict:
    """A FHIR Quantity with a UCUM unit.

    ``code`` and ``system`` carry the computable unit (the vital-signs profiles fix both);
    ``unit`` is the human-readable text, which the profiles also require.
    """
    return {
        "value": value,
        "unit": codes.UNIT_DISPLAY[ucum_code],
        "system": codes.UCUM,
        "code": ucum_code,
    }


def _vital_signs_category() -> list[dict]:
    # Observation.category slice VSCat (vitalsigns profile, 1..1): this is what makes the
    # Observation a vital sign, and what OpenEMR routes on (spike S1).
    return [{"coding": [dict(codes.VITAL_SIGNS_CATEGORY)], "text": "Vital Signs"}]


def _observation_identifier(key: str) -> list[dict]:
    # use "official": this is the identifier PulseBridge itself assigns and the one the
    # conditional update matches on. It is a technical key, not shown to people.
    return [{"use": "official", "system": codes.PB_OBSERVATION_ID, "value": key}]


# ----------------------------------------------------------------------------------------------
# Patient and Device
# ----------------------------------------------------------------------------------------------


def build_patient(patient: PatientInput) -> BuiltResource:
    """Patient: the person. Usable as-is for the FHIR store and for OpenEMR.

    OpenEMR (spike S1) keeps an identifier only if it is ``PT``-typed, and needs a name with
    ``use: official``, a gender and a birth date; the Cloud Healthcare store accepts the same shape.
    """
    identifiers = [
        {
            "use": "official",
            "type": {"coding": [dict(codes.IDENTIFIER_TYPE_PT)]},
            "system": codes.PB_PATIENT_ID,
            "value": patient.patient_id,
        }
    ]
    if patient.google_health_user_id is not None:
        # Google's healthUserId (spike S3): identifies the person only within Google Health, so
        # use "secondary". It is not the matching key: if the deployer reconnects a different
        # Google account the PulseBridge patient stays the same.
        identifiers.append(
            {
                "use": "secondary",
                "system": codes.PB_GOOGLE_HEALTH_USER_ID,
                "value": patient.google_health_user_id,
            }
        )
    resource = {
        "resourceType": "Patient",
        "identifier": identifiers,
        "name": [{"use": "official", "family": patient.family, "given": list(patient.given)}],
        "gender": patient.gender,
        "birthDate": patient.birth_date.isoformat(),
    }
    return BuiltResource(
        resource=resource,
        full_url=urn_uuid(deterministic_uuid("Patient", codes.PB_PATIENT_ID, patient.patient_id)),
        request_url=conditional_url("Patient", codes.PB_PATIENT_ID, patient.patient_id),
        key=patient.patient_id,
    )


def build_device(device: DeviceInput, *, patient: BuiltResource) -> BuiltResource:
    """Device: the watch or cuff that took the measurement.

    Google gives no serial number, so the identifier is derived from the patient and the display
    name: the same watch on the same person always gets the same Device, and the same model worn
    by two people gets two.
    """
    patient_key = _patient_key(patient)
    key = deterministic_uuid("Device", patient_key, device.display_name)
    resource: dict = {
        "resourceType": "Device",
        "identifier": [{"system": codes.PB_DEVICE_ID, "value": key}],
        "status": "active",
        "deviceName": [{"name": device.display_name, "type": codes.DEVICE_NAME_USER_FRIENDLY}],
    }
    if device.manufacturer is not None:
        resource["manufacturer"] = device.manufacturer
    # Device.patient: the person the device is attached to (a personal wearable).
    resource["patient"] = patient.reference()
    return BuiltResource(
        resource=resource,
        full_url=urn_uuid(key),
        request_url=conditional_url("Device", codes.PB_DEVICE_ID, key),
        key=key,
    )


# ----------------------------------------------------------------------------------------------
# Observations
# ----------------------------------------------------------------------------------------------


def heart_rate_key(patient_key: str, window: HeartRateWindow) -> str:
    """Idempotency key for a heart-rate window: patient, LOINC code and window start (UTC).

    The end time and the values are left out on purpose: a window that was only partly synced has
    the same start but new values, and must update the same Observation (task T10).
    """
    return deterministic_uuid(
        "Observation", patient_key, codes.HEART_RATE["code"], window.start_utc
    )


def blood_pressure_key(patient_key: str, reading: BloodPressureReading) -> str:
    """Idempotency key for a cuff reading: patient, LOINC panel code and the source key."""
    return deterministic_uuid(
        "Observation", patient_key, codes.BP_PANEL["code"], reading.source_key
    )


def _check_status(status: str) -> None:
    if status not in OBSERVATION_STATUSES:
        raise FhirInputError(
            f"observation: status {status!r} is not one of {', '.join(OBSERVATION_STATUSES)}"
        )


def build_heart_rate_observation(
    window: HeartRateWindow,
    *,
    patient: BuiltResource,
    device: BuiltResource,
    tz: tzinfo,
    status: str = "final",
) -> BuiltResource:
    """Heart-rate Observation for one roll-up window (LOINC 8867-4, UCUM ``/min``).

    - ``effectivePeriod``: the window. A Period, not a dateTime, because the value summarises an
      interval, not one moment.
    - ``valueQuantity``: the window average. The heartrate profile fixes the code to 8867-4 and the
      unit to ``/min``; ``method`` states that the value is a mean.
    - ``component``: the minimum and maximum, each with its own LOINC code, in ``/min``.
    - ``tz``: the user's zone or fixed offset; times are written in it (see ``fhir_datetime``).
    """
    _require_type(device, "Device", "device")
    _check_status(status)
    key = heart_rate_key(_patient_key(patient), window)
    resource = {
        "resourceType": "Observation",
        "meta": {"profile": [codes.PROFILE_HEART_RATE]},
        "identifier": _observation_identifier(key),
        "status": status,
        "category": _vital_signs_category(),
        "code": {"coding": [dict(codes.HEART_RATE)], "text": "Heart rate"},
        "subject": patient.reference(),
        "effectivePeriod": {
            # FHIR Period bounds are inclusive; Google's window is [start, end). Writing Google's
            # end unchanged keeps adjacent windows' bounds identical and readable.
            "start": fhir_datetime(window.start, tz),
            "end": fhir_datetime(window.end, tz),
        },
        "valueQuantity": _quantity(window.bpm_avg, codes.UCUM_PER_MINUTE),
        "method": {
            "text": "Mean of the device's samples over effectivePeriod; "
            "components give the minimum and maximum"
        },
        "device": device.reference(),
        "component": [
            {
                "code": {"coding": [dict(codes.HEART_RATE_MIN)], "text": "Heart rate minimum"},
                "valueQuantity": _quantity(window.bpm_min, codes.UCUM_PER_MINUTE),
            },
            {
                "code": {"coding": [dict(codes.HEART_RATE_MAX)], "text": "Heart rate maximum"},
                "valueQuantity": _quantity(window.bpm_max, codes.UCUM_PER_MINUTE),
            },
        ],
    }
    return BuiltResource(
        resource=resource,
        full_url=urn_uuid(key),
        request_url=conditional_url("Observation", codes.PB_OBSERVATION_ID, key),
        key=key,
    )


def build_blood_pressure_observation(
    reading: BloodPressureReading,
    *,
    patient: BuiltResource,
    device: BuiltResource,
    status: str = "final",
) -> BuiltResource:
    """Blood-pressure panel Observation (LOINC 85354-9) with systolic and diastolic components.

    The bp profile requires this shape: the panel code on the Observation, no top-level value
    (``valueQuantity`` 0..0), and exactly one systolic (8480-6) and one diastolic (8462-4)
    component in ``mm[Hg]``. Both numbers are one measurement, so they share one time
    (``effectiveDateTime``, written with the offset the reading carried).
    """
    _require_type(device, "Device", "device")
    _check_status(status)
    key = blood_pressure_key(_patient_key(patient), reading)
    resource = {
        "resourceType": "Observation",
        "meta": {"profile": [codes.PROFILE_BLOOD_PRESSURE]},
        "identifier": _observation_identifier(key),
        "status": status,
        "category": _vital_signs_category(),
        "code": {"coding": [dict(codes.BP_PANEL)], "text": "Blood pressure"},
        "subject": patient.reference(),
        "effectiveDateTime": fhir_datetime(reading.effective),
        "device": device.reference(),
        "component": [
            {
                "code": {"coding": [dict(codes.BP_SYSTOLIC)], "text": "Systolic"},
                "valueQuantity": _quantity(reading.systolic, codes.UCUM_MM_HG),
            },
            {
                "code": {"coding": [dict(codes.BP_DIASTOLIC)], "text": "Diastolic"},
                "valueQuantity": _quantity(reading.diastolic, codes.UCUM_MM_HG),
            },
        ],
    }
    return BuiltResource(
        resource=resource,
        full_url=urn_uuid(key),
        request_url=conditional_url("Observation", codes.PB_OBSERVATION_ID, key),
        key=key,
    )


# ----------------------------------------------------------------------------------------------
# Provenance
# ----------------------------------------------------------------------------------------------


def build_provenance(
    *,
    target: BuiltResource,
    device: BuiltResource,
    source: SourceInfo,
    recorded: datetime,
    assembler: str,
) -> BuiltResource:
    """Provenance: who and what produced ``target``, and from which source record.

    - ``target``: the Observation this Provenance is about.
    - ``recorded``: when PulseBridge wrote it (an instant; the caller passes the current time,
      so the builder stays deterministic).
    - ``agent`` (who took part, each with a ``type``):
        - ``performer``: the Device that measured;
        - ``custodian``: the source platform that holds the original record (``FITBIT``,
          ``HL7V2_STORE``), as a logical reference by identifier;
        - ``assembler``: the PulseBridge service that mapped it, for example
          ``"PulseBridge health-sync"`` (by display only; it has no FHIR resource).
    - ``activity``: the source's recording method (``PASSIVELY_MEASURED``), when known.
    - ``entity`` with role ``source``: the original record (HL7v2 store message name, Google
      roll-up window), as a logical reference by identifier.

    Re-writing the same Observation re-writes the same Provenance (same id), so the store keeps one
    current Provenance per Observation and its version history records every write.
    """
    _require_type(device, "Device", "device")
    if target.resource_type != "Observation":
        raise FhirInputError(
            f"provenance target must be an Observation, got {target.resource_type}"
        )
    if not isinstance(assembler, str) or not assembler.strip():
        raise FhirInputError("provenance: assembler is missing or empty")
    if not isinstance(recorded, datetime) or recorded.tzinfo is None:
        raise FhirInputError("provenance: recorded must be a datetime with a time zone")

    key = deterministic_uuid("Provenance", target.key)
    resource: dict = {
        "resourceType": "Provenance",
        # The id equals the bundle-local uuid, so fullUrl and id agree.
        "id": key,
        "target": [target.reference()],
        # instant: seconds and an offset are required (R4 datatypes.html#instant).
        "recorded": fhir_datetime(recorded),
    }
    if source.recording_method is not None:
        resource["activity"] = {
            "coding": [codes.coding(codes.PB_RECORDING_METHOD, source.recording_method)],
            "text": f"Recording method reported by the source: {source.recording_method}",
        }
    resource["agent"] = [
        {"type": {"coding": [dict(codes.AGENT_PERFORMER)]}, "who": device.reference()},
        {
            "type": {"coding": [dict(codes.AGENT_CUSTODIAN)]},
            "who": {
                "identifier": {"system": codes.PB_SOURCE_PLATFORM, "value": source.platform},
                "display": f"Source platform {source.platform}",
            },
        },
        {"type": {"coding": [dict(codes.AGENT_ASSEMBLER)]}, "who": {"display": assembler}},
    ]
    resource["entity"] = [
        {
            "role": "source",
            "what": {
                "identifier": {
                    "system": codes.PB_SOURCE_RECORD_PREFIX + source.record_type,
                    "value": source.record_id,
                },
                "display": f"{source.record_type} {source.record_id}",
            },
        }
    ]
    return BuiltResource(
        resource=resource,
        full_url=urn_uuid(key),
        request_url=f"Provenance/{key}",
        key=key,
    )
