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
- the peer closes or resets the connection -> :class:`MllpConnectionClosed`
- nothing arrives in time         -> :class:`MllpTimeout`

A reply larger than ``max_frame_bytes`` (default 1 MiB) raises :class:`MllpFrameTooLarge`; the
caller should treat it like the other two failures, as "no usable acknowledgment".

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


# Largest frame payload the decoder buffers. An HL7v2 ACK is a few hundred bytes and our ORU^R01
# about 460; 1 MiB leaves ample room while stopping a peer that never sends the end bytes from
# growing the buffer without limit.
DEFAULT_MAX_FRAME_BYTES = 1024 * 1024


class MllpFrameTooLarge(ValueError):
    """A frame grew past ``max_frame_bytes``. The decoder drops what it had buffered."""


class MllpDecoder:
    """Turn a stream of received byte chunks into complete MLLP frame payloads.

    Feed it whatever ``recv`` returned; it returns the payloads of every frame completed by that
    chunk, in order, and keeps any partial frame for the next call. Bytes outside a frame are
    discarded and counted in ``discarded_bytes``.

    A frame payload longer than ``max_frame_bytes`` raises :class:`MllpFrameTooLarge` and clears
    the buffer. Each byte is searched for the end bytes once, so feeding a long frame in many
    small chunks costs time in proportion to its length, not to its length squared.
    """

    def __init__(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        if max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be at least 1")
        self.max_frame_bytes = max_frame_bytes
        self.discarded_bytes = 0
        # Invariant between calls: the buffer is empty or starts with START_BLOCK.
        self._buffer = bytearray()
        # How far into the buffer the end bytes have already been searched for.
        self._scanned = 0

    @property
    def has_partial_frame(self) -> bool:
        """True when a frame has started but its end bytes have not arrived yet."""
        return bool(self._buffer)

    def _find_end(self, start: int) -> int:
        return self._buffer.find(FRAME_END, start)

    def _reset(self) -> None:
        self.discarded_bytes += len(self._buffer)
        self._buffer.clear()
        self._scanned = 0

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        frames: list[bytes] = []
        while self._buffer:
            if self._scanned == 0:
                start = self._buffer.find(START_BLOCK)
                if start < 0:
                    self._reset()  # no frame has started: everything buffered is noise
                    break
                self.discarded_bytes += start
                del self._buffer[:start]
                self._scanned = 1  # the start byte itself
            # Resume one byte back, in case the previous chunk ended between 0x1C and 0x0D.
            end = self._find_end(max(self._scanned - 1, 1))
            if end < 0:
                self._scanned = len(self._buffer)
                if len(self._buffer) - 1 > self.max_frame_bytes:
                    size = len(self._buffer) - 1
                    self._reset()
                    raise MllpFrameTooLarge(
                        f"MLLP frame exceeds {self.max_frame_bytes} bytes ({size} buffered "
                        "without end bytes)"
                    )
                break  # wait for more bytes
            body = bytes(self._buffer[1:end])
            del self._buffer[: end + len(FRAME_END)]
            self._scanned = 0
            # A second start byte inside a frame means the first frame was abandoned by the
            # sender; keep only what follows the last start byte.
            restart = body.rfind(START_BLOCK)
            if restart >= 0:
                self.discarded_bytes += restart + 1
                body = body[restart + 1 :]
            if len(body) > self.max_frame_bytes:
                self._reset()
                raise MllpFrameTooLarge(
                    f"MLLP frame exceeds {self.max_frame_bytes} bytes ({len(body)} bytes)"
                )
            frames.append(body)
        return frames


class MllpTimeout(TimeoutError):
    """No complete frame arrived before the deadline ("no acknowledgment received")."""


class MllpConnectionClosed(ConnectionError):
    """The peer closed the connection before a complete frame arrived."""

    def __init__(self, partial: bool, reason: str | None = None) -> None:
        self.partial = partial
        self.reason = reason  # set when a socket error, such as a reset, ended the connection
        detail = "after part of a frame" if partial else "with no data"
        suffix = f" ({reason})" if reason else ""
        super().__init__(f"connection closed by peer {detail}{suffix}")


class MllpReader:
    """Read MLLP frames from a connected socket, one at a time, with a deadline.

    Frames that arrive together are queued, so the second one is returned by the next call.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        recv_size: int = 4096,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        self._sock = sock
        self._decoder = MllpDecoder(max_frame_bytes=max_frame_bytes)
        self._ready: list[bytes] = []
        self._recv_size = recv_size

    def read_frame(self, timeout: float) -> bytes:
        """Return the next frame's payload, waiting at most ``timeout`` seconds in total.

        The deadline covers the whole frame: a peer that trickles bytes without ever finishing a
        frame still times out. Raises :class:`MllpTimeout` or :class:`MllpConnectionClosed`
        (including a connection reset or other socket error), or :class:`MllpFrameTooLarge` if
        the reply passes ``max_frame_bytes``.
        """
        deadline = time.monotonic() + timeout
        while not self._ready:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MllpTimeout(f"no complete MLLP frame within {timeout} s")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(self._recv_size)
            except TimeoutError as exc:  # a subclass of OSError, so it must come first
                raise MllpTimeout(f"no complete MLLP frame within {timeout} s") from exc
            except OSError as exc:
                # A reset (RST) or any other socket failure: the connection is gone, which is the
                # same outcome for the caller as an orderly close.
                raise MllpConnectionClosed(
                    partial=self._decoder.has_partial_frame, reason=str(exc) or type(exc).__name__
                ) from exc
            if not chunk:
                raise MllpConnectionClosed(partial=self._decoder.has_partial_frame)
            self._ready.extend(self._decoder.feed(chunk))
        return self._ready.pop(0)
