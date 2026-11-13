"""FHIR R4 builders for PulseBridge.

Pure Python, no network and no GCP calls. Callers pass plain values (the dataclasses in
``inputs``) and get plain dicts back, ready to ``json.dumps`` and send. See README.md.
"""

from pulsebridge_fhir.bundle import build_transaction_bundle
from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.google_health import (
    GoogleDataSource,
    heart_rate_resources,
    heart_rate_transaction,
    parse_google_utc_offset,
    rollup_source,
    window_from_rollup,
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
    blood_pressure_key,
    build_blood_pressure_observation,
    build_device,
    build_heart_rate_observation,
    build_patient,
    build_provenance,
    fhir_datetime,
    heart_rate_key,
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
    "blood_pressure_key",
    "build_blood_pressure_observation",
    "build_device",
    "build_heart_rate_observation",
    "build_patient",
    "build_provenance",
    "build_transaction_bundle",
    "fhir_datetime",
    "heart_rate_key",
    "heart_rate_resources",
    "heart_rate_transaction",
    "parse_google_utc_offset",
    "rollup_source",
    "window_from_rollup",
]
