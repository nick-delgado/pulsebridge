"""Send one MLLP frame to the MLLP adapter and wait for its reply.

MLLP (Minimal Lower Layer Protocol, HL7v2 2.5.1 Appendix C) wraps each message for TCP as
``0x0B`` + message + ``0x1C 0x0D``. Google's MLLP adapter on the edge VM (D14) forwards each
complete frame to the HL7v2 store and writes the store's ACK back, also framed. There is no TLS at
this layer: the VM is reached over the private VPC path only (D14).

Spike S4 found exactly these outcomes, each mapped to one result here:

| What happens on the socket                         | Result                                |
| -------------------------------------------------- | ------------------------------------- |
| an ACK frame arrives                               | ``AckReceived`` (AA or AE)            |
| the adapter closes the connection with no data     | ``NoAck("connection-closed")``        |
|   (its API call failed: missing store, no permission)                                      |
| nothing arrives before the deadline                | ``NoAck("timeout")``                  |
|   (bad framing: the adapter waits for end bytes forever)                                   |
| a frame arrives but is not a readable ACK, or is   | ``NoAck("unreadable-reply")``         |
|   larger than 4096 bytes                                                                   |
| connection refused, host unreachable, reset        | ``NoAck("connection-closed")``, or    |
|                                                    | ``NoAck("timeout")`` if connect hung  |

Every wait is bounded: connecting and writing by ``connect_timeout``, the reply by one overall
``ack_timeout`` deadline for the whole frame (T07's ``MllpReader``, so a peer trickling bytes
still times out). The socket is always closed before this returns.

The bytes are written exactly as given. A built message's segments end in ``\\r`` (T07's
``to_er7_bytes``) and nothing here converts them; a malformed sample may carry its own broken
framing (for example no end block), which is written as it is, on purpose.
"""

from __future__ import annotations

import socket

from pulsebridge_hl7v2 import MllpConnectionClosed, MllpFrameTooLarge, MllpReader, MllpTimeout

from cuff_sim.outcome import MAX_ACK_BYTES, NoAck, TransportOutcome, read_ack


def send_mllp(
    wire_bytes: bytes,
    *,
    host: str,
    port: int,
    connect_timeout: float,
    ack_timeout: float,
) -> TransportOutcome:
    """Connect, write ``wire_bytes`` (framing included), read one reply frame, close."""
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except TimeoutError:  # a subclass of OSError, so it must come first
        return NoAck("timeout", "could not connect to the MLLP adapter in time")
    except OSError:
        # Refused, unreachable, name not resolvable: no connection, so no acknowledgment.
        return NoAck("connection-closed", "could not connect to the MLLP adapter")
    with sock:
        try:
            sock.settimeout(connect_timeout)
            sock.sendall(wire_bytes)
        except TimeoutError:
            return NoAck("timeout", "the message could not be written to the MLLP adapter in time")
        except OSError:
            return NoAck("connection-closed", "the MLLP adapter closed the connection")
        reader = MllpReader(sock, max_frame_bytes=MAX_ACK_BYTES)
        try:
            payload = reader.read_frame(timeout=ack_timeout)
        except MllpTimeout:
            return NoAck("timeout", "no acknowledgment arrived before the read timeout")
        except MllpConnectionClosed as exc:
            when = "after part of a reply" if exc.partial else "with no reply"
            return NoAck("connection-closed", f"the MLLP adapter closed the connection {when}")
        except MllpFrameTooLarge:
            return NoAck("unreadable-reply", "the reply is larger than an HL7v2 ACK can be")
    return read_ack(payload)
