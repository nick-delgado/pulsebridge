"""One send, start to finish: build (or pick a sample), send, classify the answer, record it.

    request ─> build ORU^R01 ─> validate ─┐            (fails: HL7ValidationError, nothing sent)
            └> or: named malformed sample ┤            (no validation: the point of the sample)
                                          ├─> MLLP frame to the adapter ─┐
                                          └─> HTTPS messages:ingest ─────┤
                                                                         v
                       one result: ACK (AA / AE) or no acknowledgment (why) ─> AuditEvent PUT
                                                                         v
                                                                    SendResult (JSON)

The AuditEvent is written once, after the final outcome is known, and its success or failure is
reported next to the result without changing it.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from pulsebridge_fhir import now_ms
from pulsebridge_gcp import HealthcareClient
from pulsebridge_hl7v2 import new_control_id

from cuff_sim.audit import AuditStatus, build_attempt, record_attempt
from cuff_sim.config import SERVICE_NAME, ServiceConfig
from cuff_sim.https_client import send_https
from cuff_sim.message import (
    Outgoing,
    build_reading,
    derived_message_name,
    find_sample,
    from_sample,
)
from cuff_sim.mllp_client import send_mllp
from cuff_sim.outcome import AckReceived, NoAck, TransportOutcome
from cuff_sim.request import SendRequest

ACCEPTED, REJECTED, NO_ACK = "accepted", "rejected", "no-acknowledgment"
# Where messageName came from: the ingest response, or computed by us (see derived_message_name).
NAME_FROM_INGEST = "ingest-response"
NAME_DERIVED = "derived-sha256-unverified"


@dataclass(frozen=True)
class SendResult:
    outgoing: Outgoing
    transport: str
    sent_at: datetime
    outcome: TransportOutcome
    message_name: str | None
    message_name_source: str | None
    audit: AuditStatus
    duration_ms: int
    log_fields: dict = field(default_factory=dict)

    @property
    def result(self) -> str:
        if isinstance(self.outcome, NoAck):
            return NO_ACK
        return ACCEPTED if self.outcome.ack.accepted else REJECTED

    def to_json(self) -> dict:
        """The dashboard's ``CuffSendResult`` (web/src/api/contract.ts), plus ``result``,
        ``framing`` and ``messageNameSource``."""
        outcome = self.outcome
        if isinstance(outcome, AckReceived):
            ack = outcome.ack
            ack_json = {
                "received": True,
                "code": str(ack.code),  # MSA-1
                "controlId": ack.acknowledged_control_id,  # MSA-2; None on a NACK it could not read
                "reason": ack.reason,  # MSA-3, escape sequences decoded
                "raw": ack.raw.decode("utf-8", "replace"),  # segments separated by "\r"
            }
        else:
            ack_json = {
                "received": False,
                "why": outcome.why,
                "httpStatus": outcome.http_status,
                "detail": outcome.detail,
                "replyPrefixBase64": (
                    base64.b64encode(outcome.reply_prefix).decode("ascii")
                    if outcome.reply_prefix
                    else None
                ),
            }
        audit = (
            {"recorded": True}
            if self.audit.recorded
            else {"recorded": False, "detail": self.audit.detail}
        )
        return {
            "attemptId": self.audit.attempt_id,
            "sentAt": self.sent_at.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "transport": self.transport,
            "controlId": self.outgoing.control_id,
            "sample": self.outgoing.sample_name,
            "messageSent": self.outgoing.payload.decode("utf-8", "replace"),
            "framing": self.outgoing.framing,
            "messageName": self.message_name,
            "messageNameSource": self.message_name_source,
            "result": self.result,
            "ack": ack_json,
            "audit": audit,
        }


class CuffSimulator:
    """Holds the configuration and the shared client; ``send`` does one attempt."""

    def __init__(
        self,
        config: ServiceConfig,
        client: HealthcareClient,
        *,
        control_ids: Callable[[], str] = new_control_id,
        clock: Callable[[], datetime] = now_ms,
    ) -> None:
        self.config = config
        self.client = client
        self.store_name = client.hl7v2_store_name(config.hl7v2_store_id)
        self._control_ids = control_ids
        self._clock = clock

    def prepare(self, request: SendRequest) -> Outgoing:
        """The bytes to send. A built reading is validated here; ``HL7ValidationError`` (or the
        builder's ``ValueError``) propagates and nothing is sent."""
        if request.sample_name is not None:
            sample = find_sample(request.sample_name, request.transport)
            if sample is None:  # parse_request already refused this; do not guess
                raise ValueError("sample does not apply to this transport")
            # The sample carries T07's own patient (SYN-000123), not the configured one.
            # FOLLOW-UP (one line): when T07 offers with_patient, pass
            # sample.with_patient(patient(self.config)) here instead of sample.
            return from_sample(sample, request.transport)
        outgoing = build_reading(
            self.config,
            systolic=request.systolic,
            diastolic=request.diastolic,
            # A NEW control ID for every reading: the mapper keys the Observation on MSH-10, so
            # a reused one would overwrite an earlier reading (T12).
            control_id=self._control_ids(),
            # MSH-7, OBR-7 and OBX-14: whole seconds with the UTC offset (HL7 DTM).
            observed_at=datetime.now(UTC).replace(microsecond=0),
        )
        # MLLP framing applies only to the MLLP transport; HTTPS carries the bare bytes.
        return outgoing if request.transport == "mllp" else replace(outgoing, framing=None)

    def send(self, outgoing: Outgoing, transport: str) -> SendResult:
        started = time.monotonic()
        sent_at = self._clock()  # just before the first byte goes out (T31: whole milliseconds)
        message_name = source = None
        log_fields: dict = {}
        try:
            if transport == "mllp":
                outcome = send_mllp(
                    outgoing.wire_bytes,
                    host=self.config.mllp_host,
                    port=self.config.mllp_port,
                    timeout=self.config.mllp_timeout,
                )
                if isinstance(outcome, AckReceived) and outcome.ack.accepted:
                    # DERIVED: the adapter's ACK does not carry the name (derived_message_name).
                    message_name = derived_message_name(self.store_name, outgoing.payload)
                    source = NAME_DERIVED
            else:
                delivery = send_https(self.client, self.config.hl7v2_store_id, outgoing.payload)
                outcome = delivery.outcome
                if delivery.message_name is not None:
                    message_name, source = delivery.message_name, NAME_FROM_INGEST
        except Exception as exc:  # noqa: BLE001 - see below
            # Every expected failure is already a result. Anything else (a library bug) may
            # strike after bytes went out, so it is still an attempt with no acknowledgment and
            # is recorded; the exception type is logged, never its text.
            outcome = NoAck("connection-closed", "the send failed unexpectedly")
            message_name = source = None
            log_fields["send_error_type"] = type(exc).__name__

        # The AuditEvent carries a message name only when the store itself returned it (T31:
        # "HTTPS 200 only"); the derived MLLP name stays out of the permanent record until
        # the derivation is confirmed and T31 has a field for a derived name.
        # FOLLOW-UP (one line): pass the derived name to that new field here.
        audit_name = message_name if source == NAME_FROM_INGEST else None
        audit = record_attempt(
            self.client,
            self.config.audit_fhir_store_id,
            lambda: build_attempt(
                outcome=outcome,
                sent_at=sent_at,
                transport=transport,
                control_id=outgoing.control_id,
                patient_id=self.config.patient_id,
                agent=SERVICE_NAME,
                destination=self.store_name,
                message_name=audit_name,
                sample_name=outgoing.sample_name,
            ),
            recorded=max(self._clock(), sent_at),
        )
        if isinstance(outcome, AckReceived) and outcome.ack.accepted:
            # Over a fresh connection the ACK is ours; a different MSA-2 would be worth knowing.
            log_fields["ack_matches_control_id"] = (
                outcome.ack.acknowledged_control_id == outgoing.control_id
            )
        return SendResult(
            outgoing=outgoing,
            transport=transport,
            sent_at=sent_at,
            outcome=outcome,
            message_name=message_name,
            message_name_source=source,
            audit=audit,
            duration_ms=int((time.monotonic() - started) * 1000),
            log_fields=log_fields,
        )
