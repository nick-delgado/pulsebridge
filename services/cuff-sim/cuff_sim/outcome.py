"""What came back from one send: the single result shape both transports produce.

Two kinds of answer, and the difference matters (spikes S4, S5; D7):

- :class:`AckReceived`: the HL7v2 store answered with an HL7v2 acknowledgment message (an ACK).
  Its MSA-1 code says what it did: ``AA`` accepted and stored, ``AE`` refused (a "NACK"). A NACK
  is an answer: the message was read and rejected, and MSA-3 says why.
- :class:`NoAck`: no readable acknowledgment arrived at all: a timeout, a closed connection, an
  API error with no HL7 reply, or bytes that are not an ACK. The message's fate is unknown.

``why`` is one of T31's closed set of reasons (``pulsebridge_fhir.NO_ACK_REASONS``), so the
result maps to the AuditEvent without translation.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulsebridge_hl7v2 import Ack, AckParseError, parse_ack

# T31 keeps at most this many bytes of a raw ACK (and of an unreadable reply). A reply longer
# than that is not an ACK the store would send (its ACKs are under 200 bytes, spike S4).
MAX_ACK_BYTES = 4096


@dataclass(frozen=True)
class AckReceived:
    """A readable HL7v2 ACK came back (MLLP frame payload, or decoded ``hl7Ack``/``hl7Nack``)."""

    ack: Ack


@dataclass(frozen=True)
class NoAck:
    """No readable ACK. ``detail`` is a fixed sentence written by this service, never server text.

    - ``why``: ``timeout``, ``connection-closed``, ``api-error`` or ``unreadable-reply``.
    - ``http_status``: only for ``api-error`` (HTTPS), 400 to 599.
    - ``reply_prefix``: only for ``unreadable-reply``, the first bytes that came back, if any.
    """

    why: str
    detail: str
    http_status: int | None = None
    reply_prefix: bytes | None = None


TransportOutcome = AckReceived | NoAck


def read_ack(raw: bytes) -> TransportOutcome:
    """Read reply bytes (no MLLP frame) as an ACK with T07's ``parse_ack``.

    Anything ``parse_ack`` refuses, or that it accepts but T31 could not store as an ACK (over
    4096 bytes, or holding an MLLP control byte), is an ``unreadable-reply`` carrying its first
    bytes, so the attempt is still recorded with what came back. (T31 also requires the ACK to
    start with ``MSH``; ``parse_ack`` already refuses anything else, such as a leading BOM.)
    """
    prefix = raw[:MAX_ACK_BYTES] or None  # T31 refuses an empty prefix: an empty frame has none
    if len(raw) > MAX_ACK_BYTES or b"\x0b" in raw or b"\x1c" in raw:
        return NoAck("unreadable-reply", "the reply is not a readable HL7v2 ACK", None, prefix)
    try:
        return AckReceived(parse_ack(raw))
    except AckParseError:
        return NoAck("unreadable-reply", "the reply is not a readable HL7v2 ACK", None, prefix)
    except Exception:  # noqa: BLE001 - see below
        # The reply comes from outside, and parse_ack (through hl7apy) can raise other errors on
        # odd input: an IndexError for b"MSH^~MSH|", reported to T07. Whatever the parser does,
        # the message has already been sent, so the outcome must still be a result and recorded.
        return NoAck("unreadable-reply", "the reply is not a readable HL7v2 ACK", None, prefix)
