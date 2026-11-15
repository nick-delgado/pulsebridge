"""Builders for Patient, Device, Observation (heart rate, blood pressure) and Provenance.

Each builder returns a :class:`BuiltResource`: the resource as a plain dict, plus the requests a
transaction Bundle can use to write it idempotently. The builders never choose how a Patient or
Device is written; the two bundle functions in ``bundle.py`` do (see README.md, "Who owns the
Patient record").

Write strategy, per resource:

==============  ==========================================================  ======================
Resource        Request                                                     Key
==============  ==========================================================  ======================
Patient         ingest: ``POST Patient`` + ``ifNoneExist: identifier=…``    configured patient id
                setup:  ``PUT Patient?identifier=urn:pulsebridge:patient|…``
Device          ingest: ``POST Device`` + ``ifNoneExist: identifier=…``     UUIDv5(patient, name)
                setup:  ``PUT Device?identifier=urn:pulsebridge:device|…``
Observation HR  ``PUT Observation?identifier=…|<uuid>``                     UUIDv5(patient,
                                                                            8867-4, start UTC)
Observation BP  ``PUT Observation?identifier=…|<uuid>``                     UUIDv5(patient,
                                                                            85354-9, source key)
Provenance      ``PUT Provenance/<uuid>`` (update-as-create)                UUIDv5(Observation)
AuditEvent      ``PUT AuditEvent/<uuid>`` alone, never in a Bundle (T31)    UUIDv5(sender,
                                                                            transport, MSH-10,
                                                                            sent time)
==============  ==========================================================  ======================

R4 Provenance and AuditEvent have no ``identifier`` element and the Cloud Healthcare API's
conditional update matches on identifier only, so these two are written by id; that needs the
FHIR store's ``enableUpdateCreate`` setting (task T05).
"""

import copy
import re
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from enum import StrEnum

from pulsebridge_fhir import codes
from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.inputs import (
    GENDERS,
    OBSERVATION_STATUSES,
    BloodPressureReading,
    DeviceInput,
    HeartRateWindow,
    PatientInput,
    SourceInfo,
)
from pulsebridge_fhir.keys import conditional_url, deterministic_uuid, identifier_query, urn_uuid


class Write(StrEnum):
    """How a Bundle entry writes its resource.

    - ``UPDATE``: ``PUT``. A conditional update (``PUT Type?identifier=…``) or an update by id
      (``PUT Type/<id>``). Replaces the whole stored resource, or creates it if absent.
    - ``CREATE_IF_ABSENT``: ``POST`` with ``ifNoneExist``, a conditional create. Creates the
      resource only if no resource matches; an existing one is left exactly as it is.
    """

    UPDATE = "update"
    CREATE_IF_ABSENT = "create-if-absent"


# The identifier each conditionally written resource type is matched on.
_IDENTITY_SYSTEMS = {
    "Patient": codes.PB_PATIENT_ID,
    "Device": codes.PB_DEVICE_ID,
    "Observation": codes.PB_OBSERVATION_ID,
}
# Only these may be written with a conditional create (ingest); the rest are only ever updated.
CREATE_IF_ABSENT_TYPES = ("Patient", "Device")
# Resource types with no identifier element in R4, written by a deterministic id instead
# ("update as create", PUT <Type>/<id>; needs the store's enableUpdateCreate, task T05).
UPDATE_BY_ID_TYPES = ("Provenance", "AuditEvent")
# R4 id datatype: [A-Za-z0-9\-\.]{1,64}
_FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


def _own_identifier_value(resource: dict, system: str) -> str:
    """The value of the resource's one identifier in ``system``; an error if there is not one."""
    rt = resource.get("resourceType")
    identifiers = resource.get("identifier", [])
    if not isinstance(identifiers, list):
        raise FhirInputError(f"{rt}: identifier must be a list")
    matches = [i for i in identifiers if isinstance(i, dict) and i.get("system") == system]
    if len(matches) != 1:
        raise FhirInputError(
            f"{rt}: expected exactly one identifier with system {system}, found {len(matches)}"
        )
    value = matches[0].get("value")
    if not isinstance(value, str) or not value.strip():
        raise FhirInputError(f"{rt}: the {system} identifier has no value")
    return value


def request_for(resource: dict, write: Write) -> dict:
    """The ``Bundle.entry.request`` that writes ``resource``, derived from the resource itself.

    The request is never stored, so it cannot disagree with the resource it writes:

    - Patient, Device, Observation, update: ``PUT <Type>?identifier=<system>|<value>`` on the
      resource's own identifier in that type's PulseBridge system.
    - Patient, Device, create if absent: ``POST <Type>`` with
      ``ifNoneExist: identifier=<system>|<value>`` (R4 Bundle.entry.request.ifNoneExist: "just
      the query portion of the URL - what follows the '?' (not including the '?')"). Same query
      as the update, so both always match the same resource.
    - Provenance and AuditEvent, update only: ``PUT <Type>/<its id>``. Neither has an
      ``identifier`` element in R4, so there is nothing to match a conditional write on.
    """
    if not isinstance(resource, dict):
        raise FhirInputError(f"expected a resource dict, got {type(resource).__name__}")
    if not isinstance(write, Write):
        raise FhirInputError(f"expected a Write mode, got {write!r}")
    rt = resource.get("resourceType")
    if rt in UPDATE_BY_ID_TYPES:
        if write is not Write.UPDATE:
            raise FhirInputError(f"{rt} has no conditional-create request")
        rid = resource.get("id")
        if not isinstance(rid, str) or not _FHIR_ID.fullmatch(rid):
            raise FhirInputError(f"{rt}: id {rid!r} is not a valid FHIR id")
        return {"method": "PUT", "url": f"{rt}/{rid}"}
    system = _IDENTITY_SYSTEMS.get(rt)
    if system is None:
        raise FhirInputError(f"no write rule for resourceType {rt!r}")
    value = _own_identifier_value(resource, system)
    if write is Write.UPDATE:
        return {"method": "PUT", "url": conditional_url(rt, system, value)}
    if rt not in CREATE_IF_ABSENT_TYPES:
        raise FhirInputError(f"{rt} has no conditional-create request")
    return {"method": "POST", "url": rt, "ifNoneExist": identifier_query(system, value)}


@dataclass(frozen=True)
class BuiltResource:
    """A resource plus its bundle-local identity.

    - ``full_url``: ``urn:uuid:…``, the resource's identity inside one Bundle. Other entries refer
      to it by this value; the server replaces it with the real ``Type/id`` when it commits
      (R4 bundle.html, "Resolving references in Bundles").
    - ``key``: the deterministic key (identifier value or resource id), used to derive the keys of
      dependent resources.
    - ``update`` and ``create_if_absent``: the requests that would write it, computed on demand
      from ``resource`` by :func:`request_for` (``create_if_absent`` is ``None`` for Observation
      and Provenance, which are only ever updated). They are fresh dicts each time, so changing
      one changes nothing, and the bundle functions derive them again themselves.
    """

    resource: dict
    full_url: str
    key: str

    @property
    def resource_type(self) -> str:
        return self.resource["resourceType"]

    @property
    def update(self) -> dict:
        return request_for(self.resource, Write.UPDATE)

    @property
    def create_if_absent(self) -> dict | None:
        if self.resource_type not in CREATE_IF_ABSENT_TYPES:
            return None
        return request_for(self.resource, Write.CREATE_IF_ABSENT)

    def reference(self) -> dict:
        """A FHIR Reference to this resource, valid inside the same transaction Bundle."""
        return {"reference": self.full_url}

    def request(self, write: Write) -> dict:
        return request_for(self.resource, write)

    def bundle_entry(self, write: Write) -> dict:
        """The ``Bundle.entry``; the resource is deep-copied, so the source stays unchanged."""
        return {
            "fullUrl": self.full_url,
            "resource": copy.deepcopy(self.resource),
            "request": request_for(self.resource, write),
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
    """Patient: the person, with whatever demographics the caller has.

    Always carries the ``PT``-typed identifier (OpenEMR keeps an identifier only with that type,
    spike S1). Name, gender and birth date appear only when given. Use
    :func:`check_complete_patient` where the full record is required.
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
    resource: dict = {"resourceType": "Patient", "identifier": identifiers}
    if patient.family is not None or patient.given:
        # use "official": the person's legal name as the source gives it (PID-5 or
        # configuration). OpenEMR reads only an official name (spike S1).
        name: dict = {"use": "official"}
        if patient.family is not None:
            name["family"] = patient.family
        if patient.given:
            name["given"] = list(patient.given)
        resource["name"] = [name]
    if patient.gender is not None:
        resource["gender"] = patient.gender
    if patient.birth_date is not None:
        resource["birthDate"] = patient.birth_date.isoformat()
    return BuiltResource(
        resource=resource,
        full_url=urn_uuid(deterministic_uuid("Patient", codes.PB_PATIENT_ID, patient.patient_id)),
        key=patient.patient_id,
    )


# [0-9], not \d: \d also matches non-ASCII digits, which FHIR's date type does not allow.
_FULL_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _pt_identifiers(patient: dict) -> list[dict]:
    """Identifiers typed v2-0203 ``PT`` (Patient external identifier) that have a value."""
    return [
        ident
        for ident in patient.get("identifier", [])
        if isinstance(ident, dict)
        and _nonblank(ident.get("value"))
        and any(
            isinstance(c, dict)
            and c.get("system") == codes.V2_IDENTIFIER_TYPE
            and c.get("code") == "PT"
            for c in ident.get("type", {}).get("coding", [])
        )
    ]


def _complete_official_names(patient: dict) -> list[dict]:
    """Official names that have both a family name and at least one non-blank given name.

    Both parts must be on the same name: OpenEMR needs a first and a last name (spike S1), and a
    family name on one name with a given name on another is not one person's name.
    """
    return [
        name
        for name in patient.get("name", [])
        if isinstance(name, dict)
        and name.get("use") == "official"
        and _nonblank(name.get("family"))
        and isinstance(name.get("given"), list)
        and any(_nonblank(g) for g in name["given"])
    ]


def _is_full_date(value: object) -> bool:
    if not isinstance(value, str) or not _FULL_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)  # rejects 1980-13-45
    except ValueError:
        return False
    return True


def check_complete_patient(patient: dict) -> None:
    """Raise ``FhirInputError`` unless ``patient`` is a complete Patient record.

    Complete means what the setup upsert and the share to OpenEMR need (spike S1): a ``PT``-typed
    identifier with a value; one name with ``use: official`` that has both a family name and a
    given name; a gender from administrative-gender; and a real calendar birth date written
    ``YYYY-MM-DD`` in ASCII digits. It takes the resource dict, not the input, so T15 can check a
    Patient read back from the FHIR store. All missing parts are named in one message.
    """
    if not isinstance(patient, dict) or patient.get("resourceType") != "Patient":
        raise FhirInputError("complete patient: expected a Patient resource")
    problems = []
    if not _pt_identifiers(patient):
        problems.append("a PT-typed identifier")
    official = [
        n for n in patient.get("name", []) if isinstance(n, dict) and n.get("use") == "official"
    ]
    if not official:
        problems.append("a name with use 'official'")
    elif not _complete_official_names(patient):
        problems.append("an official name with both a family and a given name")
    if patient.get("gender") not in GENDERS:
        problems.append("gender")
    if not _is_full_date(patient.get("birthDate")):
        problems.append("birthDate (a real date, YYYY-MM-DD)")
    if problems:
        raise FhirInputError(f"complete patient: missing {', '.join(problems)}")


def patient_for_sharing(stored: dict) -> dict:
    """The Patient to send to the clinic (T15), built from the Patient stored in PulseBridge.

    Minimum necessary: only what OpenEMR needs to file the reading (spike S1). Kept: the ``PT``
    identifier, the official name, gender and birth date. Left out: the Google Health user id
    (an identifier at another company the clinic has no use for), ``id`` and ``meta`` (our store's
    bookkeeping), and anything else the stored record carries. Raises ``FhirInputError`` if the
    stored Patient is not complete (for example, the setup step has not run yet).
    """
    check_complete_patient(stored)
    pt = _pt_identifiers(stored)[0]
    name = _complete_official_names(stored)[0]
    identifier = {k: copy.deepcopy(pt[k]) for k in ("use", "type", "system", "value") if k in pt}
    shared_name = {"use": "official", "family": name["family"]}
    shared_name["given"] = [g for g in name["given"] if _nonblank(g)]
    return {
        "resourceType": "Patient",
        "identifier": [identifier],
        "name": [shared_name],
        "gender": stored["gender"],
        "birthDate": stored["birthDate"],
    }


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
    return BuiltResource(resource=resource, full_url=urn_uuid(key), key=key)


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


def _observation(key: str, resource: dict) -> BuiltResource:
    return BuiltResource(resource=resource, full_url=urn_uuid(key), key=key)


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
    - ``tz``: the user's time zone; times are written in it (see ``fhir_datetime``). Prefer a
      ``zoneinfo.ZoneInfo`` such as ``ZoneInfo("America/New_York")``: it applies the offset in force
      at each window. A fixed offset is right only for windows that do not cross a daylight-saving
      change.
    - ``status``: ``final`` for a complete window, ``preliminary`` for one that may still change.
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
    return _observation(key, resource)


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
    return _observation(key, resource)


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
    """Provenance: what activity produced ``target``, who took part, and from which record.

    - ``target``: the Observation this Provenance is about.
    - ``recorded``: when PulseBridge wrote it (an instant; the caller passes the current time,
      so the builder stays deterministic).
    - ``activity``: v3 DataOperation ``CREATE``, "the act of bringing an object into existence"
      (ProvenanceActivityType, the value set R4 binds here). The object is the observation data:
      the device measured it and PulseBridge assembled it into this FHIR record.
    - ``agent`` (who took part, each with a ``type``):
        - ``performer``: the Device. R4 defines performer as the one "who actually and
          principally carries out the activity"; the device took the measurement. When the source
          states its recording method (``PASSIVELY_MEASURED``), it goes in this agent's ``role``:
          how the device took part.
        - ``custodian``: the source platform that holds the original (``FITBIT``,
          ``HL7V2_STORE``), as a logical reference by identifier. Left out when the caller does
          not know the platform, rather than guessed.
        - ``assembler``: the PulseBridge service that mapped it, for example
          ``"PulseBridge health-sync"`` (by display only; it has no FHIR resource).
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
    performer: dict = {"type": {"coding": [dict(codes.AGENT_PERFORMER)]}}
    if source.recording_method is not None:
        # Provenance.agent.role: "the function of the agent with respect to the activity" (here,
        # how the device measured). Its binding is an example, so a local code system is allowed
        # without a validator warning.
        performer["role"] = [
            {
                "coding": [codes.coding(codes.PB_RECORDING_METHOD, source.recording_method)],
                "text": f"Recording method reported by the source: {source.recording_method}",
            }
        ]
    performer["who"] = device.reference()
    agents = [performer]
    if source.platform is not None:
        agents.append(
            {
                "type": {"coding": [dict(codes.AGENT_CUSTODIAN)]},
                "who": {
                    "identifier": {"system": codes.PB_SOURCE_PLATFORM, "value": source.platform},
                    "display": f"Source platform {source.platform}",
                },
            }
        )
    agents.append(
        {"type": {"coding": [dict(codes.AGENT_ASSEMBLER)]}, "who": {"display": assembler}}
    )
    resource: dict = {
        "resourceType": "Provenance",
        # The id equals the bundle-local uuid, so fullUrl and id agree.
        "id": key,
        "target": [target.reference()],
        # instant: seconds and an offset are required (R4 datatypes.html#instant).
        "recorded": fhir_datetime(recorded),
        "activity": {
            "coding": [dict(codes.ACTIVITY_CREATE)],
            "text": "Observation data brought into existence: measured by the device, "
            "assembled by PulseBridge",
        },
        "agent": agents,
        "entity": [
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
        ],
    }
    return BuiltResource(resource=resource, full_url=urn_uuid(key), key=key)
