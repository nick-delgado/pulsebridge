"""Send one message by HTTPS ingest (``messages:ingest``) through the shared ``pulsebridge_gcp``.

Spike S5 found the HL7v2 store gives the same ACK content over HTTPS as over MLLP, wrapped
differently: base64 in JSON, with no MLLP frame and no trailing carriage return. The mapping:

| HTTPS response                                         | Result                              |
| ------------------------------------------------------ | ----------------------------------- |
| 200 with ``hl7Ack``                                    | ``AckReceived`` (the store's AA)    |
| 400 or 409 with ``error.details[].hl7Nack``            | ``AckReceived`` (the NACK, AE): an  |
|                                                        | answer, not a transport failure     |
| any other 4xx/5xx (incl. 401, 403, 404, 429), no NACK  | ``NoAck("api-error", status)``      |
| no HTTP response: a timeout                            | ``NoAck("timeout")``                |
| no HTTP response: anything else (network, token)       | ``NoAck("connection-closed")``      |
| 200 whose ``hl7Ack`` cannot be read                    | ``NoAck("unreadable-reply")``       |

**A failure with no HTTP status** has no dedicated reason in T31's closed set (``api-error``
needs a status). ``pulsebridge_gcp`` raises ``RetryableError(status=None)`` and chains the
original exception as ``__cause__``:

- a ``requests.Timeout`` (connect or read): ``timeout``. After a read timeout the request was
  sent, so the message **may have been stored**; that is what "no acknowledgment" means.
- anything else (connection refused or reset, DNS, a token refresh failure): ``connection-closed``,
  the exchange ended with no reply of any kind, as for a refused MLLP connection. A token
  refresh failure means nothing was sent at all; T31's set has no exact reason for that, and
  ``connection-closed`` is the nearest.

Every ingest carries the label ``transport=https`` (spike S5): the MLLP adapter cannot set labels,
so the messages log can tell the two apart by its absence.
"""

from __future__ import annotations

from dataclasses import dataclass

# requests is pulsebridge_gcp's HTTP library (RequestsTransport); only its exception types are
# used here, to read what the transport chained.
import requests
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


def _no_response(exc: RetryableError) -> NoAck:
    """A failure with no HTTP response, classified by the exception the transport chained."""
    if isinstance(exc.__cause__, requests.Timeout):
        return NoAck("timeout", "the ingest API did not answer in time")
    return NoAck("connection-closed", "no HTTP response from the ingest API")


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
        if exc.status is None:
            return HttpsDelivery(_no_response(exc))
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
