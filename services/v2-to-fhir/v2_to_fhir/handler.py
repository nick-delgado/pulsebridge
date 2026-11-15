"""Pub/Sub push handler: one HL7v2 store notification in, one HTTP status out.

Steps (spike S5, "T12, v2-to-fhir"):

1. Read the push envelope; ``message.data`` is the stored message's resource name and
   ``message.attributes.msgType`` is MSH-9.1.
2. ``msgType == "ACK"``: acknowledge and stop (an ACK stored in the store must not loop).
3. ``messages.get`` with ``view=RAW_ONLY``; ``data`` is the exact ER7 bytes.
4. Map (:mod:`v2_to_fhir.mapping`).
5. If accepted, write the Bundle with ``executeBundle``.
6. Patch ``application_outcome`` and ``reason`` in one ``messages.patch`` with
   ``updateMask=labels``.
7. Answer 204, which acknowledges the push.

Acknowledge or retry. Pub/Sub treats 102, 200, 201, 202 and 204 as "done" and anything else as
"deliver again later"; after ``max_delivery_attempts`` (10 in T05) it moves the notification to
the dead-letter topic. So the rule is:

| Situation | Answer | Labels |
| --- | --- | --- |
| content the mapper refuses (any reason in ``reasons.py``) | 204 | ``error`` + reason |
| mapped and written | 204 | ``accepted`` + ``mapped`` |
| FHIR refuses the Bundle, 4xx other than 401/403/404/409 | 204 | ``error`` + ``fhir-rejected`` |
| notification for an ``ACK`` message | 204 | none |
| message no longer exists (404 on get or on the patch) | 204 | none (nothing to label) |
| FHIR or HL7v2 store 5xx, 429, 408, timeout, network, token refresh | 503 | none: stays "pending" |
| 401 / 403 (IAM missing or still propagating), FHIR 404 (store missing), FHIR 409 | 503 | none |
| envelope that is not a Pub/Sub push, or a message outside our HL7v2 store | 400 | none |

Retrying never does harm: the Observation is keyed on the control ID and the Provenance on the
Observation, so a second run rewrites the same resources, and the label patch overwrites both
keys. Labels are patched only after the outcome is final, so a message whose write failed is
never labelled as if it had been mapped.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from pulsebridge_gcp import (
    ClientError,
    ConflictError,
    HealthcareApiError,
    Hl7v2Message,
    MalformedResponseError,
    NotFoundError,
    operation_outcome_summary,
)

from v2_to_fhir.config import ServiceConfig
from v2_to_fhir.mapping import MappingResult, map_message
from v2_to_fhir.reasons import Outcome, Reason

# Label keys this service owns on a stored HL7v2 message (D7). Other labels, such as the cuff
# simulator's ``transport``, are left alone: the store merges a patch into existing labels.
OUTCOME_LABEL = "application_outcome"
REASON_LABEL = "reason"

ACKNOWLEDGE = 204
RETRY = 503
NOT_A_PUSH = 400


class Client(Protocol):
    """The calls the handler makes; :class:`pulsebridge_gcp.HealthcareClient` provides them."""

    def is_message_in_store(self, name: str, store: str) -> bool: ...
    def get_message(self, name: str, *, view: str = ...) -> Hl7v2Message: ...
    def execute_bundle(self, fhir_store: str, bundle: dict) -> dict: ...
    def patch_labels(self, name: str, labels: dict[str, str]) -> Hl7v2Message: ...


@dataclass(frozen=True)
class PushResult:
    """What happened, for the HTTP answer and one log line. ``details`` never holds PHI: no
    message content, no patient fields, no free text from a server."""

    status: int
    event: str
    outcome: Outcome | None = None
    reason: Reason | None = None
    details: dict = field(default_factory=dict)


class EnvelopeError(ValueError):
    """The request body is not a Pub/Sub push envelope we can use."""


@dataclass(frozen=True)
class Notification:
    message_name: str  # the stored HL7v2 message's resource name
    msg_type: str  # attributes.msgType: MSH-9.1 ("ORU", "ACK", or "" if MSH-9 is empty)
    pubsub_message_id: str | None
    delivery_attempt: int | None


def parse_envelope(envelope: object) -> Notification:
    """Read Pub/Sub's push body: ``{"message": {"data", "attributes", "messageId"},
    "subscription", "deliveryAttempt"}`` (spike S5, section 9)."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("message"), dict):
        raise EnvelopeError("body is not a JSON object with a 'message' object")
    message = envelope["message"]
    data = message.get("data")
    if not isinstance(data, str) or not data:
        raise EnvelopeError("message.data is missing")
    try:
        name = base64.b64decode(data, validate=True).decode("utf-8").strip()
    except (binascii.Error, ValueError) as exc:
        raise EnvelopeError("message.data is not base64 of UTF-8 text") from exc
    attributes = message.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise EnvelopeError("message.attributes is not an object")
    msg_type = attributes.get("msgType", "")
    attempt = envelope.get("deliveryAttempt")
    message_id = message.get("messageId")
    return Notification(
        message_name=name,
        msg_type=msg_type if isinstance(msg_type, str) else "",
        pubsub_message_id=message_id if isinstance(message_id, str) else None,
        delivery_attempt=attempt if isinstance(attempt, int) else None,
    )


def _short(name: str) -> str:
    """The message id at the end of a resource name: a hash of the bytes, not PHI."""
    return name.rsplit("/", 1)[-1]


def handle_push(
    envelope: object,
    *,
    client: Client,
    config: ServiceConfig,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PushResult:
    # 1. The envelope.
    try:
        notification = parse_envelope(envelope)
    except EnvelopeError as exc:
        return PushResult(NOT_A_PUSH, "rejected-envelope", details={"problem": str(exc)})
    details: dict = {
        "pubsub_message_id": notification.pubsub_message_id,
        "delivery_attempt": notification.delivery_attempt,
    }
    if not client.is_message_in_store(notification.message_name, config.hl7v2_store_id):
        # Not a message of the store this service serves. Cloud Run's OIDC check already limits
        # callers to the push identity; this guards against a wrong topic or a forged body.
        return PushResult(NOT_A_PUSH, "rejected-foreign-message", details=details)
    name = notification.message_name
    details["hl7v2_message"] = _short(name)

    # 2. ACK messages are acknowledged and ignored (S5: a stored ACK also notifies).
    if notification.msg_type == "ACK":
        return PushResult(ACKNOWLEDGE, "ignored-ack-message", details=details)

    # 3. Fetch the raw bytes.
    try:
        stored = client.get_message(name, view="RAW_ONLY")
    except NotFoundError:
        return PushResult(ACKNOWLEDGE, "message-gone", details=details)
    except MalformedResponseError:
        stored = None  # e.g. data not base64: content we cannot read, recorded as an error
    except HealthcareApiError as exc:
        return PushResult(RETRY, "fetch-failed", details={**details, "status": exc.status})

    # 4. Map.
    if stored is None or stored.data is None:
        result = MappingResult(Outcome.ERROR, Reason.UNPARSEABLE_BY_MAPPER)
    else:
        result = map_message(stored.data, message_name=name, config=config.mapping, recorded=now())
    details["control_id"] = result.control_id
    outcome, reason = result.outcome, result.reason

    # 5. Write the Bundle.
    if result.accepted:
        try:
            client.execute_bundle(config.fhir_store_id, result.bundle)  # type: ignore[arg-type]
        except ConflictError:
            # 409: two writes to one resource at once (e.g. two deliveries in parallel).
            return PushResult(RETRY, "fhir-write-conflict", details={**details, "status": 409})
        except ClientError as exc:
            # The store refused the Bundle as it stands: retrying the same Bundle cannot help.
            outcome, reason = Outcome.ERROR, Reason.FHIR_REJECTED
            details["fhir_status"] = exc.status
            details["fhir_issues"] = operation_outcome_summary(exc.operation_outcome)
        except HealthcareApiError as exc:
            # 5xx, 429, network, 401/403, 404 (store missing): nothing was written (a
            # transaction is all-or-nothing). Leave the message unlabelled and let Pub/Sub retry.
            return PushResult(RETRY, "fhir-write-failed", details={**details, "status": exc.status})

    # 6. Record the outcome: both keys in one patch, so a stale reason cannot remain.
    labels = {OUTCOME_LABEL: outcome.value, REASON_LABEL: reason.value}
    try:
        client.patch_labels(name, labels)
    except NotFoundError:
        return PushResult(ACKNOWLEDGE, "message-gone", outcome, reason, details)
    except HealthcareApiError as exc:
        return PushResult(
            RETRY, "label-patch-failed", outcome, reason, {**details, "status": exc.status}
        )

    # 7. Acknowledge.
    return PushResult(ACKNOWLEDGE, "processed", outcome, reason, details)
