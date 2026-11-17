"""Every assertion of Part A and the status checks, break-checked against the stack.

Each case makes the in-process stack return the wrong thing, either by breaking a fake part
underneath the real services (the store answers AE, the mapper writes the wrong unit, the
mapper never runs) or by tampering with one proxy answer. The smoke test must then fail
**that** check, with a message saying what was wrong, and exit 1. The happy path, with nothing
broken, is in ``test_smoke_flow.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import e2e_fake_stack
import pytest
from e2e_fake_stack import Stack
from e2e_runs import bp_component, edit_json, nth_only, run
from pulsebridge_smoke.http import Response


@dataclass(frozen=True)
class Break:
    name: str
    setup: Callable[[Stack, pytest.MonkeyPatch], None]
    check_id: str
    says: str  # a fragment of the failure detail
    args: tuple[str, ...] = ()


def _bundle_observation(bundle: dict) -> dict:
    return next(
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"
    )


def mapper_edit(edit: Callable[[dict], None]) -> Callable[[Stack, pytest.MonkeyPatch], None]:
    """Change what v2-to-fhir's mapping writes (the Observation in its Bundle)."""

    def setup(stack: Stack, _: pytest.MonkeyPatch) -> None:
        stack.edge.tamper_bundle = lambda bundle: edit(_bundle_observation(bundle))

    return setup


def answer_edit(
    method: str, path: str, edit: Callable[[dict], None], nth: int | None = None
) -> Callable[[Stack, pytest.MonkeyPatch], None]:
    def setup(stack: Stack, _: pytest.MonkeyPatch) -> None:
        fn = edit_json(edit)
        stack.tamper(method, path, nth_only(nth, fn) if nth else fn)

    return setup


def answer(
    method: str, path: str, status: int, body: bytes = b"{}"
) -> Callable[[Stack, pytest.MonkeyPatch], None]:
    def setup(stack: Stack, _: pytest.MonkeyPatch) -> None:
        stack.tamper(method, path, lambda r: Response(status, body, r.headers))

    return setup


def _first_trace_observation(body: dict) -> dict:
    return next(
        e["resource"]
        for e in body["resources"]["entry"]
        if e["resource"]["resourceType"] == "Observation"
    )


def trace_edit(edit: Callable[[dict], None]) -> Callable[[Stack, pytest.MonkeyPatch], None]:
    """Edit the reading's trace (the attempt whose Observation is present)."""

    def change(body: dict) -> None:
        if any(e["resource"]["resourceType"] == "Observation" for e in body["resources"]["entry"]):
            edit(body)

    return answer_edit("GET", r"/trace/cuff-attempts/.+", change)


def stored_in_openemr(edit: Callable[[dict], None], nth: int | None = None):  # noqa: ANN201
    return answer_edit(
        "POST", "/share", lambda body: edit(body["readBack"]["observation"]["stored"]), nth
    )


def store_accepts_everything(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e2e_fake_stack, "store_refusal", lambda _payload: None)


def store_refuses_unmappable(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    original = e2e_fake_stack.store_refusal
    monkeypatch.setattr(
        e2e_fake_stack,
        "store_refusal",
        lambda payload: "refused" if b"|HIGH|" in payload else original(payload),
    )


def rejected_trace_has_observation(stack: Stack, _: pytest.MonkeyPatch) -> None:
    def change(body: dict) -> None:
        if body["attempt"]["result"] == "rejected":
            body["resources"]["entry"].append({"resource": {"resourceType": "Observation"}})

    stack.tamper("GET", r"/trace/cuff-attempts/.+", edit_json(change))


def unmappable_labelled_accepted(stack: Stack, _: pytest.MonkeyPatch) -> None:
    def change(body: dict) -> None:
        application = body["attempt"].get("application") or {}
        if application.get("reason") == "non-numeric-obx-5":
            application["outcome"] = "accepted"

    stack.tamper("GET", r"/trace/cuff-attempts/.+", edit_json(change))


def set_path(*keys: str, value: object) -> Callable[[dict], None]:
    def edit(body: dict) -> None:
        for key in keys[:-1]:
            body = body[key]
        body[keys[-1]] = value

    return edit


def _component_value(loinc: str, value: object) -> Callable[[dict], None]:
    return lambda o: bp_component(o, loinc)["valueQuantity"].update(value=value)


def _set_ack_code(code: str, *, when_result: str) -> Callable[[dict], None]:
    def edit(body: dict) -> None:
        if body.get("result") == when_result:
            body["ack"]["code"] = code

    return edit


def rejected_attempt_names_an_observation(stack: Stack, _: pytest.MonkeyPatch) -> None:
    def change(body: dict) -> None:
        if body["attempt"]["result"] == "rejected":
            body["attempt"]["observation"] = "Observation/x"

    stack.tamper("GET", r"/trace/cuff-attempts/.+", edit_json(change))


INVALID_ANSWERED_200 = (
    b'{"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "invalid"}]}'
)


BREAKS = [
    # --- status ---------------------------------------------------------------------------
    Break("status answers 500", answer("GET", "/status", 500), "S2", "HTTP 500"),
    Break("status is slow", lambda s, m: None, "S3", "took", ("--status-max-seconds", "0")),
    Break(
        "FHIR store unreachable",
        answer_edit("GET", "/status", set_path("fhirStore", "reachable", value=False)),
        "S4",
        "fhirStore.detail",
    ),
    Break(
        "counts unknown",
        answer_edit("GET", "/status", set_path("observationCounts", "cuff", value=None)),
        "S5",
        "null means",
    ),
    Break(
        "OpenEMR unreachable",
        answer_edit("GET", "/status", set_path("openemr", "reachable", value=False)),
        "S6",
        "openemr.detail",
    ),
    # --- the send -------------------------------------------------------------------------
    Break(
        "cuff-sim unreachable",
        answer("POST", "/cuff/readings", 502),
        "A1",
        "HTTP 502",
    ),
    Break(
        "the store answers AE",
        lambda s, m: setattr(s.edge, "ack_override", "AE"),
        "A2",
        "ACK 'AE'",
    ),
    Break(
        "an accepted send whose ACK is CA, not AA",
        answer_edit("POST", "/cuff/readings", _set_ack_code("CA", when_result="accepted"), nth=1),
        "A2",
        "ACK 'CA'",
    ),
    Break(
        "no AuditEvent recorded",
        answer_edit(
            "POST",
            "/cuff/readings",
            set_path("audit", value={"recorded": False, "detail": "write-failed"}),
            nth=1,
        ),
        "A3",
        "audit = ",
    ),
    Break(
        "the message sent carries other values",
        answer_edit(
            "POST",
            "/cuff/readings",
            lambda b: b.update(messageSent=b["messageSent"].replace("mm[Hg]^", "mmHg^")),
            nth=1,
        ),
        "A4",
        "OBX-6 units",
    ),
    Break(
        "the message sent carries another systolic value",
        answer_edit(
            "POST",
            "/cuff/readings",
            lambda b: b.update(
                messageSent=re.sub(
                    r"(OBX\|1\|NM\|8480-6\^[^|]*\^LN\|\|)\d+", r"\g<1>999", b["messageSent"]
                )
            ),
            nth=1,
        ),
        "A4",
        "OBX-5 values are {'8480-6': '999'",
    ),
    Break(
        "the message sent is not an ORU^R01",
        answer_edit(
            "POST",
            "/cuff/readings",
            lambda b: b.update(
                messageSent=b["messageSent"].replace("ORU^R01^ORU_R01", "ADT^A01^ADT_A01")
            ),
            nth=1,
        ),
        "A4",
        "MSH-9 is 'ADT^A01",
    ),
    Break(
        "the message names another control ID",
        answer_edit("POST", "/cuff/readings", set_path("controlId", value="OTHER"), nth=1),
        "A4",
        "MSH-10 differs",
    ),
    # --- the mapping ----------------------------------------------------------------------
    Break(
        "the mapper never runs",
        lambda s, m: setattr(s.edge, "map_messages", False),
        "A6",
        "application outcome 'pending'",
    ),
    Break(
        "the panel code is wrong",
        trace_edit(
            lambda b: _first_trace_observation(b)["code"]["coding"][0].update(code="55284-4")
        ),
        "A7a",
        "85354-9",
    ),
    Break(
        "no vital-signs category",
        mapper_edit(lambda o: o["category"][0]["coding"][0].update(code="laboratory")),
        "A7b",
        "vital-signs",
    ),
    Break(
        "status preliminary",
        mapper_edit(lambda o: o.update(status="preliminary")),
        "A7c",
        "'preliminary'",
    ),
    Break(
        "a value differs",
        mapper_edit(_component_value("8462-4", 1)),
        "A7d",
        "expected",
    ),
    Break(
        "the unit is not UCUM",
        mapper_edit(lambda o: bp_component(o, "8480-6")["valueQuantity"].update(code="mmHg")),
        "A7e",
        "'mmHg'",
    ),
    Break(
        "the time differs",
        mapper_edit(lambda o: o.update(effectiveDateTime="2026-01-01T00:00:00+00:00")),
        "A7f",
        "the message said",
    ),
    Break(
        "no device reference",
        mapper_edit(lambda o: o.pop("device")),
        "A7g",
        "device.reference is None",
    ),
    Break(
        "no subject",
        trace_edit(lambda b: _first_trace_observation(b).update(subject={"reference": "Group/x"})),
        "A7h",
        "Group/x",
    ),
    Break(
        "no Provenance",
        trace_edit(
            lambda b: b["resources"].update(
                entry=[
                    e
                    for e in b["resources"]["entry"]
                    if e["resource"]["resourceType"] != "Provenance"
                ]
            )
        ),
        "A8",
        "no Provenance",
    ),
    Break(
        "the Provenance names another source",
        trace_edit(
            lambda b: next(
                e["resource"]
                for e in b["resources"]["entry"]
                if e["resource"]["resourceType"] == "Provenance"
            )["entity"][0]["what"]["identifier"].update(value="x/messages/other")
        ),
        "A8",
        "does not name the stored HL7v2 message",
    ),
    Break(
        "the read route returns other values",
        answer_edit("GET", r"/fhir/Observation/.+", _component_value("8480-6", 999)),
        "A9",
        "999",
    ),
    Break(
        "the read route answers 404",
        answer("GET", r"/fhir/Observation/.+", 404),
        "A9",
        "HTTP 404",
    ),
    # --- the share ------------------------------------------------------------------------
    Break("the share answers 502", answer("POST", "/share", 502), "A10", "HTTP 502"),
    Break(
        "OpenEMR keeps another code",
        stored_in_openemr(lambda o: o["code"]["coding"][0].update(code="8867-4"), nth=1),
        "A11a",
        "85354-9",
    ),
    Break(
        "OpenEMR keeps other values",
        stored_in_openemr(_component_value("8480-6", 50), nth=1),
        "A11b",
        "'systolic': 50",
    ),
    Break(
        "OpenEMR keeps another time",
        stored_in_openemr(lambda o: o.update(effectiveDateTime="2026-01-01T00:00:00Z"), nth=1),
        "A11c",
        "the message said",
    ),
    Break(
        "OpenEMR's read-back is missing",
        answer_edit(
            "POST", "/share", set_path("readBack", "observation", "stored", value=None), nth=1
        ),
        "A11a",
        "not an Observation",
    ),
    Break(
        "the second share creates a copy",
        answer_edit("POST", "/share", set_path("outcome", value="shared"), nth=2),
        "A12",
        "outcome 'shared'",
    ),
    Break(
        "the second share names another OpenEMR Observation",
        stored_in_openemr(lambda o: o.update(id="another-uuid"), nth=2),
        "A12",
        "another OpenEMR Observation",
    ),
    # --- the negatives --------------------------------------------------------------------
    Break(
        "an invalid reading is accepted",
        answer("POST", "/cuff/readings", 200, b'{"attemptId": "x"}'),
        "N1",
        "HTTP 200",
    ),
    Break(
        "an invalid reading answered 200 with the refusal's body",
        lambda s, m: s.tamper(
            "POST", "/cuff/readings", nth_only(2, lambda r: Response(200, INVALID_ANSWERED_200))
        ),
        "N1",
        "HTTP 200",
    ),
    Break("the store accepts an unparseable message", store_accepts_everything, "N2", "ACK 'AA'"),
    Break(
        "the unparseable message is rejected with AR, not AE",
        answer_edit("POST", "/cuff/readings", _set_ack_code("AR", when_result="rejected")),
        "N2",
        "ACK 'AR'",
    ),
    Break(
        "the rejected attempt names an Observation",
        rejected_attempt_names_an_observation,
        "N3",
        "HTTP 200",
    ),
    Break(
        "the rejected message shows an Observation",
        rejected_trace_has_observation,
        "N3",
        "Observation in the trace: True",
    ),
    Break("the unmappable message is refused", store_refuses_unmappable, "N4", "ACK 'AE'"),
    Break(
        "the unmappable message is labelled accepted",
        unmappable_labelled_accepted,
        "N5",
        "application outcome 'accepted'",
    ),
]


@pytest.mark.parametrize("case", BREAKS, ids=lambda b: b.name)
def test_a_broken_stack_fails_that_check(
    case: Break, stack: Stack, password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case.setup(stack, monkeypatch)
    result = run(stack, password, *case.args)
    assert result.status_of(case.check_id) == "FAIL", result.text
    assert case.says in result.line_of(case.check_id), result.line_of(case.check_id)
    assert result.code == 1
    assert result.lines[-1].startswith("SMOKE RESULT: FAILED")
