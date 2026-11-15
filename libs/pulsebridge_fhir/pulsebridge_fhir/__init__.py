"""FHIR R4 builders for PulseBridge.

Pure Python, no network and no GCP calls. Callers pass plain values (the dataclasses in
``inputs``) and get plain dicts back, ready to ``json.dumps`` and send. See README.md.

Send attempts from the cuff simulator are recorded as ``AuditEvent``s
(``build_send_attempt_audit_event``, written alone with ``audit_event_write``) and read back with
``parse_send_attempt``.

Bundles come from two functions only: ``build_ingest_bundle`` (new readings; Patient and Device
are created if absent, never replaced) and ``build_setup_bundle`` (the full Patient record,
written once by the setup step).
"""

from pulsebridge_fhir.audit_event import (
    NotASendAttempt,
    SendAttemptRecord,
    UnreadableSendAttempt,
    audit_event_write,
    build_send_attempt_audit_event,
    is_send_attempt,
    parse_send_attempt,
    send_attempt_key,
    send_attempt_search_params,
)
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
    Acknowledged,
    BloodPressureReading,
    DeviceInput,
    HeartRateWindow,
    NotAcknowledged,
    PatientInput,
    SendAttempt,
    SourceInfo,
    now_ms,
    sanitise_reason,
    truncate_to_ms,
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
    "Acknowledged",
    "BloodPressureReading",
    "BuiltResource",
    "DeviceInput",
    "FhirInputError",
    "GoogleDataSource",
    "HeartRateWindow",
    "NotASendAttempt",
    "NotAcknowledged",
    "PatientInput",
    "SendAttempt",
    "SendAttemptRecord",
    "SourceInfo",
    "UnreadableSendAttempt",
    "Write",
    "audit_event_write",
    "blood_pressure_key",
    "blood_pressure_transaction",
    "build_blood_pressure_observation",
    "build_device",
    "build_heart_rate_observation",
    "build_ingest_bundle",
    "build_patient",
    "build_provenance",
    "build_send_attempt_audit_event",
    "build_setup_bundle",
    "check_complete_patient",
    "fhir_datetime",
    "heart_rate_key",
    "heart_rate_resources",
    "heart_rate_transaction",
    "is_send_attempt",
    "now_ms",
    "parse_google_utc_offset",
    "parse_send_attempt",
    "patient_for_sharing",
    "patient_setup_transaction",
    "request_for",
    "rollup_source",
    "sanitise_reason",
    "send_attempt_key",
    "send_attempt_search_params",
    "truncate_to_ms",
    "window_from_rollup",
    "window_status",
]
