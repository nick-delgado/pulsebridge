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

**One deadline for the whole exchange.** ``timeout`` is computed into a deadline once, and every
step gets only what is left of it: each address tried by connect, the write, and the read of the
complete ACK frame (T07's ``MllpReader``, whose own deadline covers the whole frame, so a peer
trickling bytes still times out). So a send never waits longer than ``timeout`` on the socket,
plus name resolution: ``getaddrinfo`` cannot be given a timeout in the standard library, which is
why ``MLLP_HOST`` should be an IP address (T22 sets the VM's private address). The socket is
always closed before this returns.

If the peer sends two frames at once, the first is the reply and the rest is ignored: the
connection is used for one message only.

The bytes are written exactly as given. A built message's segments end in ``\\r`` (T07's
``to_er7_bytes``) and nothing here converts them; a malformed sample may carry its own broken
framing (for example no end block), which is written as it is, on purpose.
"""

from __future__ import annotations

import socket
import time

from pulsebridge_hl7v2 import MllpConnectionClosed, MllpFrameTooLarge, MllpReader, MllpTimeout

from cuff_sim.outcome import MAX_ACK_BYTES, NoAck, TransportOutcome, read_ack


class _ConnectTimeout(Exception):
    pass


def _connect(host: str, port: int, deadline: float) -> socket.socket:
    """Connect to the first address of ``host`` that answers, each try bounded by what is left
    of ``deadline`` (``socket.create_connection`` would give every address the full timeout).

    Raises ``_ConnectTimeout`` when the deadline passes, ``OSError`` when every address refuses
    or the name cannot be resolved."""
    last_error: OSError | None = None
    for family, kind, proto, _name, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ConnectTimeout
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(remaining)
            sock.connect(address)
            return sock
        except TimeoutError:
            sock.close()
            raise _ConnectTimeout from None
        except OSError as exc:
            sock.close()
            last_error = exc
    raise last_error or OSError("no address")


def send_mllp(wire_bytes: bytes, *, host: str, port: int, timeout: float) -> TransportOutcome:
    """Connect, write ``wire_bytes`` (framing included), read one reply frame, close; all within
    ``timeout`` seconds in total."""
    deadline = time.monotonic() + timeout
    try:
        sock = _connect(host, port, deadline)
    except _ConnectTimeout:
        return NoAck("timeout", "could not connect to the MLLP adapter in time")
    except OSError:
        # Refused, unreachable, name not resolvable: no connection, so no acknowledgment.
        return NoAck("connection-closed", "could not connect to the MLLP adapter")
    with sock:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            sock.sendall(wire_bytes)
        except TimeoutError:  # a subclass of OSError, so it must come first
            return NoAck("timeout", "the message could not be written to the MLLP adapter in time")
        except OSError:
            return NoAck("connection-closed", "the MLLP adapter closed the connection")
        reader = MllpReader(sock, max_frame_bytes=MAX_ACK_BYTES)
        try:
            payload = reader.read_frame(timeout=max(0.0, deadline - time.monotonic()))
        except MllpTimeout:
            return NoAck("timeout", "no acknowledgment arrived before the deadline")
        except MllpConnectionClosed as exc:
            when = "after part of a reply" if exc.partial else "with no reply"
            return NoAck("connection-closed", f"the MLLP adapter closed the connection {when}")
        except MllpFrameTooLarge:
            return NoAck("unreadable-reply", "the reply is larger than an HL7v2 ACK can be")
    return read_ack(payload)
