"""MLLP framing: AC "handles a message split across TCP reads and two messages in one read".

The decoder tests feed byte chunks directly; the reader tests use a real TCP connection on the
loopback interface (127.0.0.1) inside the test process. No external network is touched.
"""

import socket
import struct
import threading
import time

import pytest
from pulsebridge_hl7v2.mllp import (
    DEFAULT_MAX_FRAME_BYTES,
    MllpConnectionClosed,
    MllpDecoder,
    MllpFrameTooLarge,
    MllpReader,
    MllpTimeout,
    frame,
)

MSG_A = b"MSH|^~\\&|PHR|PB|CUFFSIM|PB|20261113205330||ACK|A1|P|2.5.1\rMSA|AA|SYN-1"
MSG_B = b"MSH|^~\\&|PHR|PB|CUFFSIM|PB|20261113205331||ACK|A2|P|2.5.1\rMSA|AE|SYN-2|bad"


def test_frame_wraps_with_start_and_end_bytes():
    assert frame(b"MSH|x") == b"\x0bMSH|x\x1c\r"


@pytest.mark.parametrize("bad", [b"MSH|\x0bx", b"MSH|\x1cx"])
def test_frame_refuses_payload_containing_framing_bytes(bad):
    with pytest.raises(ValueError, match="MLLP"):
        frame(bad)


def test_message_split_across_reads_byte_by_byte():
    decoder = MllpDecoder()
    wire = frame(MSG_A)
    out = []
    for i in range(len(wire)):
        out += decoder.feed(wire[i : i + 1])
        if i < len(wire) - 1:
            assert out == [], f"frame completed early at byte {i}"
    assert out == [MSG_A]


def test_split_between_end_byte_and_final_carriage_return():
    decoder = MllpDecoder()
    wire = frame(MSG_A)
    assert decoder.feed(wire[:-1]) == []  # ends with 0x1C: not complete without 0x0D
    assert decoder.has_partial_frame
    assert decoder.feed(wire[-1:]) == [MSG_A]
    assert not decoder.has_partial_frame


def test_two_messages_in_one_read():
    assert MllpDecoder().feed(frame(MSG_A) + frame(MSG_B)) == [MSG_A, MSG_B]


def test_one_and_a_half_messages_then_the_rest():
    decoder = MllpDecoder()
    wire = frame(MSG_A) + frame(MSG_B)
    cut = len(frame(MSG_A)) + 10
    assert decoder.feed(wire[:cut]) == [MSG_A]
    assert decoder.feed(wire[cut:]) == [MSG_B]


def test_bytes_outside_frames_are_discarded_and_counted():
    decoder = MllpDecoder()
    assert decoder.feed(b"noise" + frame(MSG_A) + b"\r\n") == [MSG_A]
    assert decoder.discarded_bytes == len(b"noise") + 2


def test_restarted_frame_keeps_only_the_new_message():
    decoder = MllpDecoder()
    assert decoder.feed(b"\x0bMSH|abandoned" + frame(MSG_A)) == [MSG_A]
    assert decoder.discarded_bytes == len(b"\x0bMSH|abandoned")


def test_missing_end_bytes_never_yields_a_frame():
    decoder = MllpDecoder()
    assert decoder.feed(b"\x0b" + MSG_A) == []
    assert decoder.feed(b"\x0b" + MSG_A + b"\x1c") == []


# --- MllpReader over a loopback TCP connection --------------------------------------------------


@pytest.fixture
def server():
    """A one-connection TCP server on 127.0.0.1 that runs ``behaviour(conn)`` in a thread."""
    listener = socket.create_server(("127.0.0.1", 0))
    threads = []

    def start(behaviour):
        def run():
            conn, _ = listener.accept()
            with conn:
                behaviour(conn)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        threads.append(thread)
        return socket.create_connection(listener.getsockname(), timeout=2)

    yield start
    listener.close()
    for thread in threads:
        thread.join(timeout=2)


def test_reader_reassembles_a_frame_sent_in_pieces(server):
    wire = frame(MSG_A)
    done = threading.Event()

    def send_in_pieces(conn):
        for i in range(0, len(wire), 7):
            conn.sendall(wire[i : i + 7])
        done.wait(2)

    client = server(send_in_pieces)
    with client:
        reader = MllpReader(client, recv_size=5)
        assert reader.read_frame(timeout=2) == MSG_A
        done.set()


def test_reader_returns_two_frames_from_one_send_in_order(server):
    done = threading.Event()

    def send_both(conn):
        conn.sendall(frame(MSG_A) + frame(MSG_B))
        done.wait(2)

    client = server(send_both)
    with client:
        reader = MllpReader(client)
        assert reader.read_frame(timeout=2) == MSG_A
        assert reader.read_frame(timeout=2) == MSG_B
        done.set()


def test_reader_reports_timeout_when_peer_stays_silent(server):
    done = threading.Event()
    client = server(lambda conn: done.wait(2))
    with client:
        with pytest.raises(MllpTimeout):
            MllpReader(client).read_frame(timeout=0.2)
        done.set()


def test_reader_reports_timeout_when_end_bytes_never_come(server):
    done = threading.Event()

    def partial(conn):
        conn.sendall(b"\x0b" + MSG_A)
        done.wait(2)

    client = server(partial)
    with client:
        with pytest.raises(MllpTimeout):
            MllpReader(client).read_frame(timeout=0.3)
        done.set()


def test_reader_reports_connection_closed_with_no_data(server):
    client = server(lambda conn: None)  # accept, then close at once (S4 row e1)
    with client, pytest.raises(MllpConnectionClosed) as caught:
        MllpReader(client).read_frame(timeout=2)
    assert caught.value.partial is False
    assert not isinstance(caught.value, MllpTimeout)


def test_reader_reports_connection_closed_mid_frame(server):
    client = server(lambda conn: conn.sendall(b"\x0b" + MSG_A[:20]))
    with client, pytest.raises(MllpConnectionClosed) as caught:
        MllpReader(client).read_frame(timeout=2)
    assert caught.value.partial is True


def test_reader_reports_a_reset_as_connection_closed(server):
    def reset(conn):
        # SO_LINGER with a zero timeout makes close() send a TCP reset (RST), not an orderly FIN.
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    client = server(reset)
    with client:
        time.sleep(0.1)  # let the reset arrive before we read
        with pytest.raises(MllpConnectionClosed) as caught:
            MllpReader(client).read_frame(timeout=2)
    assert caught.value.reason  # set only on the socket-error path, not on an orderly close


def test_reader_deadline_covers_the_whole_frame_not_each_read(server):
    stop = threading.Event()

    def trickle(conn):
        # One byte every 50 ms for at most 3 s, never the end bytes, then close. A reader that
        # applied its timeout to each recv would keep reading and end with "connection closed"
        # instead of a timeout after 0.3 s.
        conn.sendall(b"\x0b")
        give_up = time.monotonic() + 3
        while not stop.wait(0.05) and time.monotonic() < give_up:
            try:
                conn.sendall(b"M")
            except OSError:
                return

    client = server(trickle)
    with client:
        started = time.monotonic()
        with pytest.raises(MllpTimeout):
            MllpReader(client).read_frame(timeout=0.3)
        elapsed = time.monotonic() - started
        stop.set()
    assert elapsed < 1.0, f"timed out after {elapsed:.2f} s, not about 0.3 s"


# --- frame size limit and scanning cost -------------------------------------------------------


def test_default_frame_limit_is_one_mebibyte():
    assert DEFAULT_MAX_FRAME_BYTES == 1024 * 1024
    assert MllpDecoder().max_frame_bytes == DEFAULT_MAX_FRAME_BYTES


def test_frame_at_the_limit_is_accepted():
    assert MllpDecoder(max_frame_bytes=10).feed(frame(b"0123456789")) == [b"0123456789"]


def test_unfinished_frame_past_the_limit_raises_and_the_decoder_recovers():
    decoder = MllpDecoder(max_frame_bytes=10)
    assert decoder.feed(b"\x0b0123456789") == []  # 10 bytes so far: still allowed
    with pytest.raises(MllpFrameTooLarge):
        decoder.feed(b"A")
    assert not decoder.has_partial_frame
    assert decoder.feed(frame(b"ok")) == [b"ok"]


def test_complete_frame_past_the_limit_in_one_chunk_raises():
    with pytest.raises(MllpFrameTooLarge):
        MllpDecoder(max_frame_bytes=10).feed(frame(b"0123456789A"))


def test_reader_passes_the_limit_to_its_decoder(server):
    done = threading.Event()

    def big(conn):
        conn.sendall(frame(b"X" * 100))
        done.wait(2)

    client = server(big)
    with client:
        with pytest.raises(MllpFrameTooLarge):
            MllpReader(client, max_frame_bytes=50).read_frame(timeout=2)
        done.set()


class CountingDecoder(MllpDecoder):
    """Counts how many buffered bytes the end-byte search looks at (the real search runs)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bytes_searched = 0

    def _find_end(self, start):
        self.bytes_searched += len(self._buffer) - start
        return super()._find_end(start)


def test_end_byte_search_resumes_instead_of_rescanning():
    # 1 MiB arriving in 1 KiB chunks. Rescanning from the start on every chunk would look at
    # about 512 MiB; resuming looks at each byte about once.
    decoder = CountingDecoder(max_frame_bytes=4 * 1024 * 1024)
    total = 0
    decoder.feed(b"\x0b")
    for _ in range(1024):
        decoder.feed(b"M" * 1024)
        total += 1024
    assert decoder.feed(b"\x1c\r") == [b"M" * total]
    assert decoder.bytes_searched <= 2 * total + 4096
