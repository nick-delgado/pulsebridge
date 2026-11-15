"""Thin client for the Cloud Healthcare REST API (FHIR and HL7v2 stores). See README.md."""

from pulsebridge_gcp.client import MESSAGE_NAME, HealthcareClient, raise_for_status
from pulsebridge_gcp.errors import (
    ClientError,
    ConflictError,
    HealthcareApiError,
    MalformedResponseError,
    NotFoundError,
    PermissionDeniedError,
    RetryableError,
    TimedOutError,
    operation_outcome_summary,
)
from pulsebridge_gcp.models import (
    Hl7v2Message,
    IngestResult,
    MessagePage,
    PatientId,
    check_labels,
)
from pulsebridge_gcp.transport import (
    HttpResponse,
    RequestsTransport,
    Transport,
    default_transport,
)

__all__ = [
    "MESSAGE_NAME",
    "ClientError",
    "ConflictError",
    "HealthcareApiError",
    "HealthcareClient",
    "Hl7v2Message",
    "HttpResponse",
    "IngestResult",
    "MalformedResponseError",
    "MessagePage",
    "NotFoundError",
    "PatientId",
    "PermissionDeniedError",
    "RequestsTransport",
    "RetryableError",
    "TimedOutError",
    "Transport",
    "check_labels",
    "default_transport",
    "operation_outcome_summary",
    "raise_for_status",
]
