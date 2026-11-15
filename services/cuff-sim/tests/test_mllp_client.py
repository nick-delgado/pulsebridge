"""The MLLP client against a fake adapter on 127.0.0.1: every outcome spike S4 found, and more."""

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


def send(port, wire=None, ack_timeout=0.6, connect_timeout=1.0):
    return send_mllp(
        frame(MESSAGE) if wire is None else wire,
        host="127.0.0.1",
        port=port,
        connect_timeout=connect_timeout,
        ack_timeout=ack_timeout,
    )


def assert_closed_by_client(adapter):
    """The client closed its socket before send_mllp returned (so it is never left open)."""
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
    outcome = send(adapter.port, ack_timeout=0.5)
    elapsed = time.monotonic() - started
    assert isinstance(outcome, NoAck) and outcome.why == "timeout"
    assert 0.45 <= elapsed < 1.5
    assert_closed_by_client(adapter)


def test_trickling_bytes_still_time_out_on_one_deadline(adapter):
    adapter.trickle()  # a byte every 50 ms, never an end block
    started = time.monotonic()
    outcome = send(adapter.port, ack_timeout=0.5)
    assert outcome.why == "timeout"
    assert time.monotonic() - started < 1.5  # not reset by each byte
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


def test_connect_timeout_is_timeout(monkeypatch):
    def hang(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(mllp_client.socket, "create_connection", hang)
    outcome = send(1)
    assert outcome.why == "timeout"
    assert "connect" in outcome.detail


def test_unresolvable_host_is_connection_closed(monkeypatch):
    def unknown(*_args, **_kwargs):
        raise socket.gaierror("name not known")

    monkeypatch.setattr(mllp_client.socket, "create_connection", unknown)
    assert send(1).why == "connection-closed"


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
    monkeypatch.setattr(mllp_client.socket, "create_connection", lambda *_a, **_k: fake)
    assert send(1).why == why
    assert fake.closed


def test_connect_uses_the_connect_timeout(monkeypatch):
    seen = {}

    def record(address, timeout):
        seen["address"], seen["timeout"] = address, timeout
        raise ConnectionRefusedError

    monkeypatch.setattr(mllp_client.socket, "create_connection", record)
    send(2575, connect_timeout=2.5)
    assert seen == {"address": ("127.0.0.1", 2575), "timeout": 2.5}
