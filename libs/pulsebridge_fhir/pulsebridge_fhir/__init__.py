"""FHIR R4 builders for PulseBridge.

Pure Python, no network and no GCP calls. Callers pass plain values (the dataclasses in
``inputs``) and get plain dicts back, ready to ``json.dumps`` and send. See README.md.

Bundles come from two functions only: ``build_ingest_bundle`` (new readings; Patient and Device
are created if absent, never replaced) and ``build_setup_bundle`` (the full Patient record,
written once by the setup step).
"""

from pulsebridge_fhir.bundle import (
    blood_pressure_transaction,
    build_ingest_bundle,
    build_setup_bundle,
    patient_setup_transaction,
)
from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.google_health import (
    GoogleDataSource,
    heart_rate_resources,
    heart_rate_transaction,
    parse_google_utc_offset,
    rollup_source,
    window_from_rollup,
    window_status,
)
from pulsebridge_fhir.inputs import (
    BloodPressureReading,
    DeviceInput,
    HeartRateWindow,
    PatientInput,
    SourceInfo,
)
from pulsebridge_fhir.resources import (
    BuiltResource,
    Write,
    blood_pressure_key,
    build_blood_pressure_observation,
    build_device,
    build_heart_rate_observation,
    build_patient,
    build_provenance,
    check_complete_patient,
    fhir_datetime,
    heart_rate_key,
    patient_for_sharing,
    request_for,
)

__all__ = [
    "BloodPressureReading",
    "BuiltResource",
    "DeviceInput",
    "FhirInputError",
    "GoogleDataSource",
    "HeartRateWindow",
    "PatientInput",
    "SourceInfo",
    "Write",
    "blood_pressure_key",
    "blood_pressure_transaction",
    "build_blood_pressure_observation",
    "build_device",
    "build_heart_rate_observation",
    "build_ingest_bundle",
    "build_patient",
    "build_provenance",
    "build_setup_bundle",
    "check_complete_patient",
    "fhir_datetime",
    "heart_rate_key",
    "heart_rate_resources",
    "heart_rate_transaction",
    "parse_google_utc_offset",
    "patient_for_sharing",
    "patient_setup_transaction",
    "request_for",
    "rollup_source",
    "window_from_rollup",
    "window_status",
]
