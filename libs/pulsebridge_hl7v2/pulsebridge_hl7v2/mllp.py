"""MLLP framing for the client side (the cuff simulator talking to Google's MLLP adapter).

MLLP, the Minimal Lower Layer Protocol (HL7v2 2.5.1 Appendix C, "MLLP release 1"), wraps each
HL7v2 message for TCP:

    <VT> message bytes <FS><CR>        VT = 0x0B, FS = 0x1C, CR = 0x0D

TCP is a byte stream: one ``send`` can arrive as several ``recv`` calls, and two messages can
arrive in one. The start and end bytes let the receiver find message boundaries. VT and FS were
chosen because they never occur in HL7 text.

Spike S4 found that Google's adapter sends nothing at all for a badly framed message (it waits for
the end bytes forever), and closes the connection without a reply when the Healthcare API call
fails. So a client has three outcomes, each a distinct result here:

- an ACK frame arrives            -> :meth:`MllpReader.read_frame` returns its bytes
- the peer closes the connection  -> :class:`MllpConnectionClosed`
- nothing arrives in time         -> :class:`MllpTimeout`

This is framing only; what is inside a frame is read by hl7apy (see :mod:`pulsebridge_hl7v2.ack`).
"""

from __future__ import annotations

import socket
import time

START_BLOCK = b"\x0b"  # VT, vertical tab
END_BLOCK = b"\x1c"  # FS, file separator
CARRIAGE_RETURN = b"\x0d"
FRAME_END = END_BLOCK + CARRIAGE_RETURN


def frame(payload: bytes) -> bytes:
    """Wrap one message in an MLLP frame.

    Raises ``ValueError`` if the payload contains a start or end byte, because the receiver would
    cut the message there.
    """
    if START_BLOCK in payload or END_BLOCK in payload:
        raise ValueError("payload contains an MLLP start (0x0B) or end (0x1C) byte")
    return START_BLOCK + payload + FRAME_END


class MllpDecoder:
    """Turn a stream of received byte chunks into complete MLLP frame payloads.

    Feed it whatever ``recv`` returned; it returns the payloads of every frame completed by that
    chunk, in order, and keeps any partial frame for the next call. Bytes outside a frame are
    discarded and counted in ``discarded_bytes``.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.discarded_bytes = 0

    @property
    def has_partial_frame(self) -> bool:
        """True when a frame has started but its end bytes have not arrived yet."""
        return START_BLOCK[0] in self._buffer

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(START_BLOCK)
            if start < 0:
                # No frame has started: everything buffered is noise.
                self.discarded_bytes += len(self._buffer)
                self._buffer.clear()
                return frames
            if start > 0:
                self.discarded_bytes += start
                del self._buffer[:start]
            end = self._buffer.find(FRAME_END, 1)
            if end < 0:
                return frames  # wait for more bytes
            body = bytes(self._buffer[1:end])
            del self._buffer[: end + len(FRAME_END)]
            # A second start byte inside a frame means the first frame was abandoned by the
            # sender; keep only what follows the last start byte.
            restart = body.rfind(START_BLOCK)
            if restart >= 0:
                self.discarded_bytes += restart + 1
                body = body[restart + 1 :]
            frames.append(body)


class MllpTimeout(TimeoutError):
    """No complete frame arrived before the deadline ("no acknowledgment received")."""


class MllpConnectionClosed(ConnectionError):
    """The peer closed the connection before a complete frame arrived."""

    def __init__(self, partial: bool) -> None:
        self.partial = partial
        detail = "after part of a frame" if partial else "with no data"
        super().__init__(f"connection closed by peer {detail}")


class MllpReader:
    """Read MLLP frames from a connected socket, one at a time, with a deadline.

    Frames that arrive together are queued, so the second one is returned by the next call.
    """

    def __init__(self, sock: socket.socket, *, recv_size: int = 4096) -> None:
        self._sock = sock
        self._decoder = MllpDecoder()
        self._ready: list[bytes] = []
        self._recv_size = recv_size

    def read_frame(self, timeout: float) -> bytes:
        """Return the next frame's payload, waiting at most ``timeout`` seconds in total."""
        deadline = time.monotonic() + timeout
        while not self._ready:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MllpTimeout(f"no complete MLLP frame within {timeout} s")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(self._recv_size)
            except TimeoutError as exc:
                raise MllpTimeout(f"no complete MLLP frame within {timeout} s") from exc
            if not chunk:
                raise MllpConnectionClosed(partial=self._decoder.has_partial_frame)
            self._ready.extend(self._decoder.feed(chunk))
        return self._ready.pop(0)
