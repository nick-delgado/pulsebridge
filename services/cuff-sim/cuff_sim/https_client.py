"""Send one message by HTTPS ingest (``messages:ingest``) through the shared ``pulsebridge_gcp``.

Spike S5 found the HL7v2 store gives the same ACK content over HTTPS as over MLLP, wrapped
differently: base64 in JSON, with no MLLP frame and no trailing carriage return. The mapping:

| HTTPS response                                         | Result                              |
| ------------------------------------------------------ | ----------------------------------- |
| 200 with ``hl7Ack``                                    | ``AckReceived`` (the store's AA)    |
| 400 or 409 with ``error.details[].hl7Nack``            | ``AckReceived`` (the NACK, AE): an  |
|                                                        | answer, not a transport failure     |
| any other 4xx/5xx (incl. 401, 403, 404, 429), no NACK  | ``NoAck("api-error", status)``      |
| no HTTP response at all (network, token refresh)       | ``NoAck("connection-closed")``      |
| 200 whose ``hl7Ack`` cannot be read                    | ``NoAck("unreadable-reply")``       |

**A network failure has no HTTP status**, and T31's closed set of reasons has no value for it
(``api-error`` needs a status). It is mapped to ``connection-closed``: the exchange ended without
any reply, which is what that reason says, and it is how a refused or reset MLLP connection is
reported too. ``timeout`` was rejected: ``pulsebridge_gcp`` reports every request failure with no
response as one ``RetryableError`` without saying whether it timed out, and guessing from the
exception's text would tie this mapping to the transport's internals.

Every ingest carries the label ``transport=https`` (spike S5): the MLLP adapter cannot set labels,
so the messages log can tell the two apart by its absence.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulsebridge_gcp import (
    ClientError,
    HealthcareApiError,
    HealthcareClient,
    MalformedResponseError,
    RetryableError,
)

from cuff_sim.outcome import AckReceived, NoAck, TransportOutcome, read_ack

INGEST_LABELS = {"transport": "https"}


@dataclass(frozen=True)
class HttpsDelivery:
    outcome: TransportOutcome
    # The stored message's resource name from the ingest response: only for an accepting ACK,
    # and only if it is a message of the configured store.
    message_name: str | None = None


def _api_error(status: int | None) -> NoAck:
    if status is None:
        return NoAck("connection-closed", "no HTTP response from the ingest API")
    if not 400 <= status <= 599:
        # A status the API does not document for errors (a 3xx): not an answer we can read.
        return NoAck("unreadable-reply", f"the ingest API answered HTTP {status}")
    return NoAck("api-error", f"the ingest API returned HTTP {status} with no HL7 reply", status)


def send_https(client: HealthcareClient, hl7v2_store: str, payload: bytes) -> HttpsDelivery:
    """Ingest ``payload`` (the exact message bytes) and classify the answer. Never raises for an
    API or network failure: every outcome is a result."""
    try:
        result = client.ingest(hl7v2_store, payload, labels=INGEST_LABELS)
    except ClientError as exc:  # 400, 409 and other 4xx except 401/403/404; ConflictError too
        if exc.hl7_nack is not None:
            # The store read the message and refused it: the NACK is the answer (S5).
            return HttpsDelivery(read_ack(exc.hl7_nack))
        return HttpsDelivery(_api_error(exc.status))
    except MalformedResponseError:
        # A 2xx whose hl7Ack (or message) is missing or not base64.
        return HttpsDelivery(
            NoAck("unreadable-reply", "the ingest response did not carry a readable ACK")
        )
    except RetryableError as exc:  # 408, 429, 5xx, or no response at all (status None)
        return HttpsDelivery(_api_error(exc.status))
    except HealthcareApiError as exc:  # NotFoundError (404), PermissionDeniedError (401, 403)
        return HttpsDelivery(_api_error(exc.status))

    outcome = read_ack(result.hl7_ack)
    name = None
    if (
        isinstance(outcome, AckReceived)
        and outcome.ack.accepted
        and client.is_message_in_store(result.message.name, hl7v2_store)
    ):
        name = result.message.name
    return HttpsDelivery(outcome, name)
