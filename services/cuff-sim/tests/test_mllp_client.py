"""The MLLP client against a fake adapter on 127.0.0.1: every outcome spike S4 found, and more."""

import os
import socket
import time

import pytest
from cuff_sim import mllp_client
from cuff_sim.mllp_client import send_mllp
from cuff_sim.outcome import AckReceived, NoAck
from pulsebridge_hl7v2 import frame

MESSAGE = (
    b"MSH|^~\\&|CUFFSIM|PULSEBRIDGE|PHR|PULSEBRIDGE|20261115100000+0000||"
    b"ORU^R01^ORU_R01|T1|P|2.5.1\r"
)


def send(port, wire=None, timeout=0.6, host="127.0.0.1"):
    return send_mllp(
        frame(MESSAGE) if wire is None else wire, host=host, port=port, timeout=timeout
    )


def assert_closed_by_client(adapter):
    """The client closed its socket (the adapter saw EOF), so it is never left open."""
    assert adapter.connections, "no connection was made"
    assert adapter.connections[-1].client_closed.wait(2)


def test_ack_frame_from_s4_is_parsed(adapter, h):
    adapter.reply(h.S4_AA)
    outcome = send(adapter.port)
    assert isinstance(outcome, AckReceived)
    assert outcome.ack.code == "AA"
    assert outcome.ack.acknowledged_control_id == "S4-205329-0001"
    assert outcome.ack.message_type == "ACK"  # the store's type, not ACK^R01
    assert outcome.ack.raw == h.S4_AA  # frame bytes removed, nothing else changed
    assert_closed_by_client(adapter)


def test_wire_bytes_are_written_exactly(adapter, h):
    adapter.reply(h.S4_AA)
    send(adapter.port)
    assert bytes(adapter.connections[0].received) == b"\x0b" + MESSAGE + b"\x1c\r"


def test_nack_for_no_msh_is_an_answer_with_reason_and_empty_msa2(adapter, h):
    adapter.reply(h.S4_NACK_NO_MSH)
    outcome = send(adapter.port)
    assert isinstance(outcome, AckReceived)
    assert outcome.ack.code == "AE"
    assert outcome.ack.reason == "Invalid MSH header"
    assert outcome.ack.acknowledged_control_id is None  # not required to match on a NACK


def test_nack_reason_escape_is_decoded(adapter, h):
    adapter.reply(h.S4_NACK_LF)
    outcome = send(adapter.port)
    assert outcome.ack.reason == 'invalid segment terminator, store expects: "\\r"'


def test_two_frames_in_one_reply_the_first_wins(adapter, h):
    adapter.reply_raw(frame(h.S4_AA) + frame(h.S4_NACK_NO_MSH))
    outcome = send(adapter.port)
    assert outcome.ack.code == "AA"
    assert outcome.ack.acknowledged_control_id == "S4-205329-0001"  # visible: not our T1


def test_close_with_no_data_is_connection_closed(adapter):
    adapter.close()
    outcome = send(adapter.port)
    assert isinstance(outcome, NoAck)
    assert outcome.why == "connection-closed"
    assert "with no reply" in outcome.detail
    assert outcome.http_status is None


def test_close_after_part_of_a_frame_is_connection_closed(adapter):
    def partial_then_close(s, _p):
        s.sendall(b"\x0bMSH|^~\\&|")
        s.shutdown(socket.SHUT_WR)

    adapter._action = partial_then_close
    outcome = send(adapter.port)
    assert outcome.why == "connection-closed"
    assert "after part of a reply" in outcome.detail


def test_silence_is_a_timeout_within_the_deadline(adapter):
    adapter.silent()
    started = time.monotonic()
    outcome = send(adapter.port, timeout=0.5)
    elapsed = time.monotonic() - started
    assert isinstance(outcome, NoAck) and outcome.why == "timeout"
    assert 0.45 <= elapsed < 1.0
    assert_closed_by_client(adapter)


def test_trickling_bytes_still_time_out_on_one_deadline(adapter):
    adapter.trickle()  # a byte every 50 ms, never an end block
    started = time.monotonic()
    outcome = send(adapter.port, timeout=0.5)
    assert outcome.why == "timeout"
    assert time.monotonic() - started < 1.0  # not reset by each byte
    assert_closed_by_client(adapter)


def test_reset_is_connection_closed(adapter):
    adapter.reset()
    outcome = send(adapter.port)
    assert outcome.why == "connection-closed"


def test_oversized_reply_is_unreadable(adapter):
    adapter.reply(b"MSH|" + b"X" * 5000)
    outcome = send(adapter.port)
    assert outcome.why == "unreadable-reply"
    assert outcome.reply_prefix is None


def test_garbage_frame_is_unreadable_and_keeps_the_bytes(adapter):
    adapter.reply(b"HELLO NOT HL7")
    outcome = send(adapter.port)
    assert outcome.why == "unreadable-reply"
    assert outcome.reply_prefix == b"HELLO NOT HL7"


def test_reply_that_crashes_the_parser_is_unreadable(adapter):
    # T07's parse_ack raises IndexError (not AckParseError) on this; reported to T07.
    adapter.reply(b"MSH^~MSH|")
    outcome = send(adapter.port)
    assert outcome.why == "unreadable-reply"
    assert outcome.reply_prefix == b"MSH^~MSH|"


def test_empty_frame_is_unreadable_with_no_prefix(adapter):
    adapter.reply(b"")
    outcome = send(adapter.port)
    assert outcome.why == "unreadable-reply"
    assert outcome.reply_prefix is None  # T31 refuses an empty prefix


def test_msh_frame_that_is_not_an_ack_is_unreadable(adapter):
    adapter.reply(MESSAGE.rstrip(b"\r"))  # an ORU, not an ACK: parse_ack refuses it
    outcome = send(adapter.port)
    assert outcome.why == "unreadable-reply"
    assert outcome.reply_prefix == MESSAGE.rstrip(b"\r")


def test_unframed_garbage_then_close_is_connection_closed(adapter):
    def junk(s, _p):
        s.sendall(b"junk with no frame")
        s.shutdown(socket.SHUT_WR)

    adapter._action = junk
    assert send(adapter.port).why == "connection-closed"


def test_refused_connection_is_connection_closed(h):
    outcome = send(h.closed_port())
    assert isinstance(outcome, NoAck)
    assert outcome.why == "connection-closed"
    assert "could not connect" in outcome.detail


def test_hanging_connect_is_a_timeout_within_the_deadline(hanging):
    started = time.monotonic()
    outcome = send(hanging.port, timeout=0.4)
    elapsed = time.monotonic() - started
    assert outcome.why == "timeout" and "connect" in outcome.detail
    assert 0.35 <= elapsed < 0.8


def test_several_hanging_addresses_share_one_deadline(hanging, monkeypatch):
    # Three addresses for one name, all hanging: socket.create_connection would give each the
    # full timeout (3 x 0.4 s); one deadline means the whole connect takes 0.4 s.
    address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", hanging.port))
    monkeypatch.setattr(mllp_client.socket, "getaddrinfo", lambda *_a, **_k: [address] * 3)
    started = time.monotonic()
    assert send(hanging.port, timeout=0.4, host="adapter.internal").why == "timeout"
    assert time.monotonic() - started < 0.8


def test_refused_first_address_falls_through_to_the_next(adapter, h, monkeypatch):
    refused = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", h.closed_port()))
    good = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", adapter.port))
    monkeypatch.setattr(mllp_client.socket, "getaddrinfo", lambda *_a, **_k: [refused, good])
    adapter.reply(h.S4_AA)
    assert send(adapter.port, host="adapter.internal").ack.code == "AA"


def test_one_deadline_covers_connect_and_read(adapter, monkeypatch):
    # A slow connect uses up part of the deadline; the read gets only what is left.
    real = mllp_client._connect

    def slow(host, port, deadline):
        time.sleep(0.3)
        return real(host, port, deadline)

    monkeypatch.setattr(mllp_client, "_connect", slow)
    adapter.silent()
    started = time.monotonic()
    assert send(adapter.port, timeout=0.5).why == "timeout"
    assert time.monotonic() - started < 0.75  # not 0.3 + 0.5


def test_unresolvable_host_is_connection_closed(monkeypatch):
    def unknown(*_args, **_kwargs):
        raise socket.gaierror("name not known")

    monkeypatch.setattr(mllp_client.socket, "getaddrinfo", unknown)
    assert send(1, host="no-such-host.invalid").why == "connection-closed"


class _FailingSocket:
    def __init__(self, error):
        self.error = error
        self.closed = False

    def settimeout(self, _t):
        pass

    def sendall(self, _data):
        raise self.error

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True


@pytest.mark.parametrize(
    ("error", "why"),
    [(TimeoutError(), "timeout"), (BrokenPipeError(), "connection-closed")],
)
def test_write_failures_close_the_socket(monkeypatch, error, why):
    fake = _FailingSocket(error)
    monkeypatch.setattr(mllp_client, "_connect", lambda *_a: fake)
    assert send(1).why == why
    assert fake.closed


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc (Linux)")
def test_failing_sends_leave_no_file_descriptors_open(h):
    behaviours = ["close", "reset", "silent"]
    adapters = [h.FakeMllpAdapter() for _ in behaviours]
    try:
        for fake, behaviour in zip(adapters, behaviours, strict=True):
            getattr(fake, behaviour)()
        refused = h.closed_port()
        send(refused)  # warm up anything lazily opened
        before = open_fds()
        for _ in range(10):
            send(refused)
            for fake in adapters:
                send(fake.port, timeout=0.05)
        # The fake adapters' own sockets (same process) close on their threads; give them a
        # moment. A socket leaked by the client stays open however long we wait.
        settle = time.monotonic() + 2
        while open_fds() != before and time.monotonic() < settle:
            time.sleep(0.05)
        assert open_fds() == before
    finally:
        for fake in adapters:
            fake.stop()


def test_deadline_used_up_by_connect_is_a_write_timeout(adapter, monkeypatch):
    real = mllp_client._connect

    def slow(host, port, deadline):
        sock = real(host, port, deadline)
        time.sleep(0.3)  # past the 0.2 s deadline before anything is written
        return sock

    monkeypatch.setattr(mllp_client, "_connect", slow)
    outcome = send(adapter.port, timeout=0.2)
    assert outcome.why == "timeout" and "written" in outcome.detail
    assert not adapter.connections[0].received  # nothing was sent


def test_no_address_at_all_is_connection_closed(monkeypatch):
    monkeypatch.setattr(mllp_client.socket, "getaddrinfo", lambda *_a, **_k: [])
    assert send(1, host="adapter.internal").why == "connection-closed"
