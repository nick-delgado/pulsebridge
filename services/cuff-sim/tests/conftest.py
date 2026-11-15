"""Shared fixtures: a fake MLLP adapter on 127.0.0.1, configuration, and a client over
``FakeTransport``. No network beyond loopback, no credentials; every value is synthetic."""

from __future__ import annotations

import base64
import contextlib
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from cuff_sim.config import ServiceConfig
from pulsebridge_gcp import HealthcareClient
from pulsebridge_gcp.testing import FakeTransport

PROJECT = "demo-project"
STORE_NAME = f"projects/{PROJECT}/locations/us-central1/datasets/demo/hl7V2Stores/main"
AUDIT_PATH_SUFFIX = "/fhirStores/audit/fhir/AuditEvent"

# Replies recorded in the spikes, byte for byte (the MLLP frame is added by the listener).
# S4, valid message: the store's AA, relayed by the adapter.
S4_AA = (
    b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113205330||ACK|"
    b"6c52ee99-ac83-49c0-b976-cd739886d858|P|2.5.1\rMSA|AA|S4-205329-0001"
)
# S4 row b1: unparseable message. Blank header fields, empty MSA-2.
S4_NACK_NO_MSH = (
    b"MSH|^~\\&|||||20261113205348||ACK|63a5f7b7-0ae4-42a6-b517-eed75c1861a3\r"
    b"MSA|AE||Invalid MSH header"
)
# S4 row a6: line-feed terminators. MSA-3 holds the HL7 escape \E\ (a backslash).
S4_NACK_LF = (
    b"MSH|^~\\&|||||20261113205348||ACK|57eb795c-d742-4a29-8bdc-4a8ed3c7f356\r"
    b'MSA|AE||invalid segment terminator, store expects: "\\E\\r"'
)
# S5: hl7Ack of a valid message and hl7Nack of a message with no MSH (no frame, no final CR).
S5_AA = (
    b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113224607||ACK|"
    b"2478b790-9ee0-4adf-b7de-a897e8fecc6b|P|2.5.1\rMSA|AA|S5-0001"
)
S5_NACK_NO_MSH = (
    b"MSH|^~\\&|||||20261113225233||ACK|cf6fbef5-243b-4873-bdb2-dc5bcca37253\r"
    b"MSA|AE||Invalid MSH header"
)
# S5 with rejectDuplicateMessage: 409 and this MSA (the spike recorded the MSA segment only).
S5_NACK_DUPLICATE = (
    b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261113224700||ACK|"
    b"0b8d5a62-6f0e-4c1b-9d55-3a4f1e7c2b90|P|2.5.1\rMSA|AE|S5-RD|message already exists"
)

ENV = {
    "GCP_PROJECT_ID": PROJECT,
    "HEALTHCARE_LOCATION": "us-central1",
    "HEALTHCARE_DATASET_ID": "demo",
    "HL7V2_STORE_ID": "main",
    "AUDIT_FHIR_STORE_ID": "audit",
    "MLLP_HOST": "127.0.0.1",
    "PATIENT_ID": "SYN-000123",
    "PATIENT_ASSIGNING_AUTHORITY": "PULSEBRIDGE",
    "PATIENT_FAMILY_NAME": "TESTPATIENT",
    "PATIENT_GIVEN_NAME": "SYNTHIA",
}


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def ack_for(control_id: str, code: str = "AA") -> bytes:
    """An ACK in the store's shape (spike S4) answering ``control_id``."""
    return (
        b"MSH|^~\\&|PHR|PULSEBRIDGE|CUFFSIM|PULSEBRIDGE|20261115100001||ACK|"
        b"d2c1f7a0-1b2c-4d3e-8f90-a1b2c3d4e5f6|P|2.5.1\r"
        + f"MSA|{code}|{control_id}".encode("ascii")
    )


# ---- Fake MLLP adapter ----------------------------------------------------------------------


@dataclass
class Connection:
    received: bytearray = field(default_factory=bytearray)
    client_closed: threading.Event = field(default_factory=threading.Event)


class FakeMllpAdapter:
    """Listens on 127.0.0.1 (an ephemeral port) and plays one behaviour per connection.

    It reads until the MLLP end bytes arrive (or, for a frame that never ends, until the client
    stops sending for a moment), records exactly what it received, then:

    - ``reply(bytes)``: writes those bytes in a proper MLLP frame;
    - ``reply_raw(bytes)``: writes bytes as given (for garbage, partial or oversized replies);
    - ``reply_with(fn)``: writes ``frame(fn(received_payload))``;
    - ``close()``: closes with no data (S4: the adapter's API call failed);
    - ``silent()``: sends nothing and waits until the client closes (S4: bad framing);
    - ``trickle()``: sends a frame start and then one byte at a time, never the end;
    - ``reset()``: closes with a TCP reset (RST).

    After each connection it keeps reading until the client closes, and records that, so a test
    can assert the client never leaves a socket open.
    """

    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(5)
        self.port = self._server.getsockname()[1]
        self.connections: list[Connection] = []
        self._action: Callable[[socket.socket, bytes], None] = lambda _s, _p: None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._stop = threading.Event()
        self._thread.start()

    # behaviours
    def reply(self, ack: bytes) -> None:
        self._action = lambda s, _p: s.sendall(b"\x0b" + ack + b"\x1c\r")

    def reply_raw(self, data: bytes) -> None:
        self._action = lambda s, _p: s.sendall(data)

    def reply_with(self, fn: Callable[[bytes], bytes]) -> None:
        self._action = lambda s, p: s.sendall(b"\x0b" + fn(p) + b"\x1c\r")

    def close(self) -> None:
        self._action = lambda s, _p: s.shutdown(socket.SHUT_WR)

    def silent(self) -> None:
        self._action = lambda _s, _p: None

    def trickle(self) -> None:
        def act(s: socket.socket, _p: bytes) -> None:
            s.sendall(b"\x0bMSH")
            for _ in range(40):
                time.sleep(0.05)
                try:
                    s.sendall(b"|")
                except OSError:
                    return

        self._action = act

    def reset(self) -> None:
        def act(s: socket.socket, _p: bytes) -> None:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()

        self._action = act

    # plumbing
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            record = Connection()
            self.connections.append(record)
            threading.Thread(target=self._handle, args=(conn, record), daemon=True).start()

    def _handle(self, conn: socket.socket, record: Connection) -> None:
        conn.settimeout(0.3)
        try:
            while b"\x1c\r" not in record.received:
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    break  # a frame that never ends: act on what came
                if not chunk:
                    record.client_closed.set()
                    break
                record.received += chunk
            payload = bytes(record.received).removeprefix(b"\x0b").removesuffix(b"\x1c\r")
            self._action(conn, payload)
            conn.settimeout(10)
            while not record.client_closed.is_set():
                if not conn.recv(4096):
                    record.client_closed.set()
        except OSError:
            record.client_closed.set()
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def stop(self) -> None:
        self._stop.set()
        self._server.close()


@pytest.fixture
def adapter():
    fake = FakeMllpAdapter()
    yield fake
    fake.stop()


def closed_port() -> int:
    """A loopback port nothing listens on (bound, then released)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---- Configuration and client ------------------------------------------------------------


def make_config(port: int, **overrides: str) -> ServiceConfig:
    env = dict(ENV, MLLP_PORT=str(port), MLLP_ACK_TIMEOUT_SECONDS="0.6", **overrides)
    return ServiceConfig.from_env(env)


@pytest.fixture
def config(adapter):
    return make_config(adapter.port)


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def client(transport):
    return HealthcareClient(
        project=PROJECT, location="us-central1", dataset="demo", transport=transport
    )


@pytest.fixture
def h():
    """The helpers above, for test modules (importlib mode cannot import conftest by name)."""
    import types

    return types.SimpleNamespace(
        PROJECT=PROJECT,
        STORE_NAME=STORE_NAME,
        AUDIT_PATH_SUFFIX=AUDIT_PATH_SUFFIX,
        S4_AA=S4_AA,
        S4_NACK_NO_MSH=S4_NACK_NO_MSH,
        S4_NACK_LF=S4_NACK_LF,
        S5_AA=S5_AA,
        S5_NACK_NO_MSH=S5_NACK_NO_MSH,
        S5_NACK_DUPLICATE=S5_NACK_DUPLICATE,
        ENV=dict(ENV),
        b64=b64,
        ack_for=ack_for,
        closed_port=closed_port,
        make_config=make_config,
        FakeMllpAdapter=FakeMllpAdapter,
    )
