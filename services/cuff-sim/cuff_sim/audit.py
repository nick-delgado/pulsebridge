"""Record one send attempt as a FHIR R4 AuditEvent in the audit FHIR store (D12, D15).

The HL7v2 store keeps neither rejected messages nor any ACK bytes (spikes S4, S5), so this
record, written by the sender, is the only place a rejected or unanswered attempt survives. The
simulator builds it itself, with T31's builder, from values it produced or validated: the
configured patient and store, its own control ID, the ACK it parsed. It never takes FHIR from a
caller.

Written once, after the final outcome is known, with ``PUT AuditEvent/<id>`` (T31's
deterministic id) to the **audit** store only. T31's rule is "last write wins", so writing once
keeps the record equal to what happened.

Recording can fail (the store is down, a role is missing). That is reported in the response and
logged without content, and never changes or hides the send's own result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from pulsebridge_fhir import (
    Acknowledged,
    FhirInputError,
    NotAcknowledged,
    SendAttempt,
    audit_event_write,
    build_send_attempt_audit_event,
)
from pulsebridge_gcp import HealthcareApiError, HealthcareClient

from cuff_sim.outcome import AckReceived, TransportOutcome


@dataclass(frozen=True)
class AuditStatus:
    recorded: bool
    attempt_id: str | None  # the AuditEvent id, known as soon as it is built
    detail: str | None = None  # a fixed sentence when not recorded
    failure: str | None = None  # a short code for the log: build-failed, write-failed, ...
    http_status: int | None = None


def result_for(outcome: TransportOutcome) -> Acknowledged | NotAcknowledged:
    """T31's result type for a transport outcome."""
    if isinstance(outcome, AckReceived):
        ack = outcome.ack
        return Acknowledged(code=ack.code, raw_ack=ack.raw, reason=ack.reason)
    return NotAcknowledged(
        why=outcome.why, http_status=outcome.http_status, reply_prefix=outcome.reply_prefix
    )


def build_attempt(
    *,
    outcome: TransportOutcome,
    sent_at: datetime,
    transport: str,
    control_id: str | None,
    patient_id: str,
    agent: str,
    destination: str,
    message_name: str | None,
    sample_name: str | None,
) -> SendAttempt:
    return SendAttempt(
        sent_at=sent_at,
        transport=transport,
        control_id=control_id,
        result=result_for(outcome),
        patient_id=patient_id,
        agent=agent,
        destination=destination,
        message_name=message_name,
        sample_name=sample_name,
    )


def record_attempt(
    client: HealthcareClient,
    audit_fhir_store: str,
    build: Callable[[], SendAttempt],  # called here, so a refusal of its values is caught here
    *,
    recorded: datetime,
) -> AuditStatus:
    """Build the AuditEvent and PUT it to the audit store. Never raises."""
    try:
        built = build_send_attempt_audit_event(build(), recorded=recorded)
        write = audit_event_write(built)
    except FhirInputError:
        return AuditStatus(
            False, None, "the attempt could not be expressed as an AuditEvent", "build-failed"
        )
    except Exception:  # noqa: BLE001 - the send already happened; never fail the request here
        return AuditStatus(
            False, None, "the attempt could not be expressed as an AuditEvent", "build-error"
        )
    attempt_id = built.resource["id"]
    resource_type, _, resource_id = write["url"].partition("/")
    try:
        if write["method"] != "PUT" or resource_type != "AuditEvent" or resource_id != attempt_id:
            raise ValueError("unexpected write")  # T31 contract changed; do not guess
        client.put_resource(audit_fhir_store, resource_type, resource_id, write["body"])
    except HealthcareApiError as exc:
        shown = f"HTTP {exc.status}" if exc.status is not None else "no HTTP response"
        return AuditStatus(
            False,
            attempt_id,
            f"the audit store write failed ({shown})",
            "write-failed",
            exc.status,
        )
    except Exception:  # noqa: BLE001 - recording must never fail the request
        return AuditStatus(False, attempt_id, "the audit store write failed", "write-error")
    return AuditStatus(True, attempt_id)
