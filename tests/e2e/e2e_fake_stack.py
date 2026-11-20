"""An in-process PulseBridge stack for the smoke test's unit tests. Synthetic data only.

What is real and what is fake:

- **real**: fhir-proxy (``fhir_proxy.app.create_app``, with the share extension: routes, auth,
  status, the traces, the share), cuff-sim (``cuff_sim.app.create_app``: request checks, build
  and validate, the AuditEvent) and v2-to-fhir's mapping (``v2_to_fhir.mapping.map_message``);
- **fake**: the three Cloud Healthcare stores (``fhir_proxy.testing.FakeHealthcare``, plus
  :class:`AuditWriter` for cuff-sim's one write), the edge VM's MLLP adapter with the HL7v2
  store's parse and the Pub/Sub push to the mapper (:class:`FakeEdge`), and OpenEMR
  (``fhir_proxy.share.testing.FakeOpenEmr``).

The smoke test talks to the proxy through :class:`FlaskTransport`, which can also **tamper**
with an answer (``stack.tamper(method, path_regex, fn)``): that is how each assertion is
break-checked ("the stack returns the wrong thing; the smoke test must fail, saying why").

:class:`FakeEdge` imitates what spikes S4/S5 recorded of the real store: a message that does
not start with an MSH segment, or uses another segment terminator than ``\\r``, or whose
segments do not start with a segment name and the declared field separator, is refused with
``AE`` and not stored; anything else is stored (deduplicated by exact bytes, as S5 found),
acknowledged ``AA``, and handed to the mapper, as the Pub/Sub push to v2-to-fhir would.
The ACK is built with hl7apy, never by hand.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import cuff_sim.service
from cuff_sim.app import create_app as create_cuff_sim
from cuff_sim.config import ServiceConfig as CuffConfig
from cuff_sim.message import derived_message_name
from cuff_sim.outcome import NoAck, read_ack
from cuff_sim.ratelimit import TokenBucket
from fhir_proxy.config import SamplePatient
from fhir_proxy.share.testing import ShareEnv, build_share_env, stored_patient
from fhir_proxy.testing import (
    AUDIT_STORE,
    DATASET,
    DEMO_LIMITS,
    FHIR_STORE,
    HEALTH_URL,
    HL7_STORE,
    LOCATION,
    PROJECT,
    SIGNING_KEY,  # obviously synthetic; the proxy's test configuration writes it to its file
    STORE_NAME,
    FakeHealthcare,
    apply_transaction,
)
from hl7apy.core import Message
from pulsebridge_gcp import HealthcareClient, HttpResponse
from pulsebridge_hl7v2.mllp import MllpDecoder
from pulsebridge_smoke.http import Response
from v2_to_fhir.mapping import MappingConfig, map_message

SYNTHETIC_PATIENT = "SYN-E2E-0001"
# The deployment the smoke test is told about, with the defaults' shape: the secret name prefix
# (module identity's identity_name_prefix, the secret_name_prefix output, "pb" by default) is NOT
# the name prefix module phr builds TOKEN_ISSUER from ("pulsebridge" by default). The issuer is
# built here exactly as module phr builds it, so a smoke test that derived it from the secret
# prefix would sign with the wrong issuer and fail S1.
SECRET_PREFIX = "pb"  # noqa: S105 - a name prefix, not a secret
NAME_PREFIX = "pulsebridge"
TOKEN_ISSUER = f"pulsebridge-fhir-proxy:{PROJECT}:{NAME_PREFIX}"  # infra/modules/phr/fhir_proxy.tf
REAL_PATIENT = "E2E-PERSON-0001"  # what a real-data stack's patient id looks like (no SYN-)

SEGMENT = re.compile(rb"[A-Z][A-Z0-9]{2}")


def build_ack(code: str, control_id: str, reason: str | None) -> bytes:
    """An ACK in the HL7v2 store's shape (spike S4): MSH with the store as sender, MSA-1 the
    acknowledgment code, MSA-2 the control ID it answers, MSA-3 the reason for an AE."""
    ack = Message("ACK", version="2.5.1")
    ack.msh.msh_3 = "PHR"
    ack.msh.msh_4 = "PULSEBRIDGE"
    ack.msh.msh_5 = "CUFFSIM"
    ack.msh.msh_6 = "PULSEBRIDGE"
    ack.msh.msh_7 = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    ack.msh.msh_9 = "ACK"
    ack.msh.msh_10 = str(uuid.uuid4())
    ack.msh.msh_11 = "P"
    ack.msh.msh_12 = "2.5.1"
    ack.add_segment("MSA")
    ack.msa.msa_1 = code
    if control_id:
        ack.msa.msa_2 = control_id
    if reason:
        ack.msa.msa_3 = reason
    return ack.to_er7().encode("ascii")


def _control_id(payload: bytes) -> str:
    """MSH-10 for the ACK. The store reads it itself; for a message it cannot parse the
    MSA-2 is empty (S4), which is what an empty string gives."""
    from fhir_proxy import hl7

    return hl7.control_id(payload) or ""


def store_refusal(payload: bytes) -> str | None:
    """Why the HL7v2 store would refuse these bytes with AE (S4/S5), or None."""
    if not payload.startswith(b"MSH|"):
        return "Invalid MSH header"
    if b"\n" in payload:
        return "invalid segment terminator, store expects CR"
    separator = payload[3:4]
    for segment in payload.rstrip(b"\r").split(b"\r"):
        if not (SEGMENT.fullmatch(segment[:3]) and segment[3:4] in (separator, b"")):
            return "Invalid segment name"
    return None


@dataclass
class FakeEdge:
    """The edge VM's MLLP adapter, the HL7v2 store and the push to v2-to-fhir, in one call."""

    fake: FakeHealthcare
    patient_id: str
    ack_override: str | None = None  # e.g. "AE": the store answers this whatever it got
    map_messages: bool = True  # False: the mapper never runs (labels stay pending)
    tamper_bundle: Callable[[dict], None] | None = None  # edit the mapper's Bundle in place
    received: list[bytes] = field(default_factory=list)
    _mapped: int = 0

    def send_mllp(self, wire_bytes: bytes, *, host: str, port: int, timeout: float):  # noqa: ANN201, ARG002
        frames = MllpDecoder().feed(wire_bytes)
        if not frames:
            return NoAck("timeout", "no complete MLLP frame; the adapter waits")
        payload = frames[0]
        self.received.append(payload)
        refusal = store_refusal(payload)
        code = self.ack_override or ("AE" if refusal else "AA")
        if code == "AA":
            self._store(payload)
        return read_ack(build_ack(code, _control_id(payload), refusal if code != "AA" else None))

    def _store(self, payload: bytes) -> None:
        name = derived_message_name(STORE_NAME, payload)
        if name in self.fake.messages:
            return  # S5: the same bytes are stored once and notified once
        now = datetime.now(UTC)
        labels = None
        if self.map_messages:
            result = map_message(
                payload,
                message_name=name,
                config=MappingConfig(self.patient_id),
                recorded=now.replace(microsecond=0),
            )
            labels = {
                "application_outcome": result.outcome.value,
                "reason": result.reason.value,
            }
            if result.bundle is not None:
                if self.tamper_bundle is not None:
                    self.tamper_bundle(result.bundle)
                self._mapped += 1
                apply_transaction(self.fake, result.bundle, prefix=f"m{self._mapped}")
        self.fake.add_message(
            name,
            payload,
            labels=labels,
            create_time=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            patient_ids=[self.patient_id],
        )


@dataclass
class AuditWriter:
    """cuff-sim's one Cloud Healthcare call: ``PUT AuditEvent/<id>`` into the audit store."""

    fake: FakeHealthcare

    def request(self, method, url, *, params, body, headers, timeout):  # noqa: ANN001, ANN201, ARG002
        path = urlsplit(url).path
        if method == "PUT" and re.fullmatch(
            rf".*/fhirStores/{AUDIT_STORE}/fhir/AuditEvent/[^/]+", path
        ):
            resource = json.loads(body.decode("utf-8"))
            self.fake.put(AUDIT_STORE, resource)
            return HttpResponse(
                200, json.dumps(resource).encode(), {"content-type": "application/fhir+json"}
            )
        raise AssertionError(f"cuff-sim made an unexpected call: {method} {path}")


Tamper = Callable[[Response], Response]


@dataclass
class FlaskTransport:
    """The smoke test's transport, over the proxy's Flask test client. Records every request
    (method, path, headers, body) so a test can check what the smoke test sent."""

    client: object
    clock: object
    tampers: list[tuple[str, str, Tamper]] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)

    def send(self, method, path, *, headers, body, timeout):  # noqa: ANN001, ANN201, ARG002
        # The proxy's injected clock follows real time, so cuff-sim's AuditEvents (stamped with
        # the real time) are inside the messages log's window.
        self.clock.now = datetime.now(UTC)
        self.requests.append(
            {"method": method, "path": path, "headers": dict(headers), "body": body}
        )
        answer = self.client.open(path, method=method, data=body, headers=dict(headers))
        response = Response(
            answer.status_code,
            answer.get_data(),
            {k.lower(): v for k, v in answer.headers.items()},
        )
        for tamper_method, pattern, fn in self.tampers:
            if tamper_method == method and re.fullmatch(pattern, path):
                response = fn(response)
        return response


@dataclass
class Stack:
    env: ShareEnv
    edge: FakeEdge
    transport: FlaskTransport
    cuff_client: object
    patient_id: str

    @property
    def store(self) -> FakeHealthcare:
        return self.env.store

    def tamper(self, method: str, path: str, fn: Tamper) -> None:
        self.transport.tampers.append((method, path, fn))

    def observations(self) -> list[dict]:
        return [r for (t, _), r in self.store.fhir[FHIR_STORE].items() if t == "Observation"]


def _answer(answer) -> tuple[int, dict, bytes]:  # noqa: ANN001 - a Flask test response
    """A Flask test response as the proxy's upstream sender returns one."""
    return answer.status_code, {k.lower(): v for k, v in answer.headers.items()}, answer.get_data()


def build_stack(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
    key,  # noqa: ANN001
    cert,  # noqa: ANN001
    *,
    real_data: bool = False,
    patient_id: str | None = None,
) -> Stack:
    patient = patient_id or (REAL_PATIENT if real_data else SYNTHETIC_PATIENT)
    env = build_share_env(
        tmp_path,
        key,
        cert,
        seed=False,
        patient_id=patient,
        # Synthetic data: the stack deploys no health-sync, so the proxy has no URL for it.
        health_sync_url=HEALTH_URL if real_data else None,
        sample_patient=SamplePatient("PULSEBRIDGE", "Synthetic", "Demo"),
        token_issuer=TOKEN_ISSUER,
        **DEMO_LIMITS,
    )
    env.store.put(FHIR_STORE, stored_patient(value=patient))
    edge = FakeEdge(env.store, patient)
    monkeypatch.setattr(cuff_sim.service, "send_mllp", edge.send_mllp)
    cuff_config = CuffConfig.from_env(
        {
            "GCP_PROJECT_ID": PROJECT,
            "HEALTHCARE_LOCATION": LOCATION,
            "HEALTHCARE_DATASET_ID": DATASET,
            "HL7V2_STORE_ID": HL7_STORE,
            "AUDIT_FHIR_STORE_ID": AUDIT_STORE,
            "MLLP_HOST": "192.0.2.10",
            "PATIENT_ID": patient,
            "PATIENT_ASSIGNING_AUTHORITY": "PULSEBRIDGE",
            "PATIENT_FAMILY_NAME": "Synthetic",
            "PATIENT_GIVEN_NAME": "Demo",
        }
    )
    client = HealthcareClient(
        project=PROJECT, location=LOCATION, dataset=DATASET, transport=AuditWriter(env.store)
    )
    cuff_app = create_cuff_sim(cuff_config, client, bucket=TokenBucket(per_minute=600, burst=100))
    cuff_client = cuff_app.test_client()

    def readings(*, timeout: float, body: bytes | None):  # noqa: ANN202, ARG001
        return _answer(
            cuff_client.post("/cuff/readings", data=body, content_type="application/json")
        )

    def samples(*, timeout: float, body: bytes | None):  # noqa: ANN202, ARG001
        return _answer(cuff_client.get("/cuff/samples"))

    env.harness.sender.routes["/cuff/readings"] = readings
    env.harness.sender.routes["/cuff/samples"] = samples
    transport = FlaskTransport(env.harness.client, env.clock)
    return Stack(env, edge, transport, cuff_client, patient)


__all__ = ["NAME_PREFIX", "SECRET_PREFIX", "SIGNING_KEY", "TOKEN_ISSUER", "Stack", "build_stack"]
