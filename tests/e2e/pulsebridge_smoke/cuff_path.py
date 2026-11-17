"""Part A: the status strip, the real-data guard, and the cuff path end to end.

The flow (each step is a check; ``tests/e2e/README.md`` lists them with their ids):

1. ``GET /status`` answers quickly and reports the FHIR store and OpenEMR reachable. Its
   ``watchSync`` tells whether the stack uses real data: with ``use_synthetic_data = true`` the
   stack deploys no health-sync, and the proxy says ``reason: health-sync-not-deployed``.
2. The guard: on a real-data stack Part A's writes are refused unless
   ``--allow-real-data-writes`` was given (they add a reading to a real person's record).
3. ``POST /cuff/readings`` makes cuff-sim build an HL7v2 ORU^R01 and send it over MLLP (the
   Minimal Lower Layer Protocol: the message between a start byte 0x0B and the end bytes
   0x1C 0x0D, over plain TCP) to the edge VM's adapter, which stores it in the HL7v2 store and
   relays the store's ACK. The ACK's MSA-1 must be ``AA`` ("application accept").
4. The HL7v2 store notifies v2-to-fhir (Pub/Sub push), which maps the message to a FHIR
   Observation and Provenance and labels the message ``accepted``. The smoke test polls the
   proxy's cuff-attempt trace (``GET /trace/cuff-attempts/<attemptId>``) until the Observation
   is there. The trace finds it by identifier, which Cloud Healthcare indexes synchronously, so
   no search-indexing delay is involved.
5. ``POST /share`` sends the Observation to OpenEMR; the answer's read-back is the Observation
   as OpenEMR stored it. A second share must answer ``already-shared`` (idempotence: OpenEMR
   refuses a second reading with the same code, Encounter and time).
6. Negatives: an invalid reading is refused before anything is sent (422), a message the store
   cannot parse gets ``AE`` ("application error"), and a message that parses but cannot be
   mapped is stored and labelled ``error``. None of the three creates an Observation.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pulsebridge_smoke import fhir
from pulsebridge_smoke.hl7 import DIASTOLIC, SYSTOLIC, read_sent_reading
from pulsebridge_smoke.http import ApiClient, Response, problem_reason
from pulsebridge_smoke.report import Report, Stop

SYNTHETIC_PATIENT_PREFIX = "SYN-"  # module phr refuses any other patient_id with synthetic data
NOT_DEPLOYED = "health-sync-not-deployed"
# T07 samples, by name (libs/pulsebridge_hl7v2/samples/samples.json): one the store rejects with
# AE ("Invalid MSH header": no MSH segment), one it stores and the mapper refuses (OBX-5 "HIGH"
# where OBX-2 says numeric). Both list the MLLP transport.
REJECTED_SAMPLE = "unparseable_no_msh"
UNMAPPABLE_SAMPLE = "unmappable_non_numeric_obx5"


@dataclass(frozen=True)
class Settings:
    status_max_seconds: float = 5.0
    poll_timeout: float = 180.0
    poll_interval: float = 5.0
    allow_real_data_writes: bool = False
    transport: str = "mllp"
    rate_limit_retries: int = 3


def random_reading() -> tuple[int, int]:
    """A plausible synthetic reading, different on most runs: systolic 101-139, diastolic
    61-89 (inside cuff-sim's 1-400 and the mapper's plausible ranges, systolic above
    diastolic). Uniqueness does not depend on it: every reading gets a new control ID."""
    return 101 + secrets.randbelow(39), 61 + secrets.randbelow(29)


@dataclass
class Context:
    api: ApiClient
    report: Report
    settings: Settings = field(default_factory=Settings)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    choose_reading: Callable[[], tuple[int, int]] = random_reading


@dataclass(frozen=True)
class StackFacts:
    """What /status says about the stack. ``real_data`` is True unless the stack positively
    reports synthetic data: an unreadable answer counts as real (the safe direction)."""

    real_data: bool
    watch_state: str | None
    watch_reason: str | None
    why: str


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# ---- status and the guard ---------------------------------------------------------------------


def check_status(ctx: Context) -> StackFacts:
    r = ctx.report
    response = ctx.api.call("GET", "/status")
    body = _dict(response.json())
    r.require(
        "S2",
        "/status answers 200 with the status shape",
        None
        if response.status == 200 and isinstance(body.get("watchSync"), dict)
        else f"HTTP {response.status} (reason {problem_reason(response)})",
    )
    limit = ctx.settings.status_max_seconds
    r.check(
        "S3",
        f"/status answers within {limit:g} s",
        None
        if response.elapsed <= limit
        else f"took {response.elapsed:.1f} s (raise --status-max-seconds for a cold start)",
        ok=f"{response.elapsed:.2f} s",
    )
    store = _dict(body.get("fhirStore"))
    r.check(
        "S4",
        "FHIR store reachable",
        None if store.get("reachable") is True else f"fhirStore.detail = {store.get('detail')!r}",
    )
    counts = _dict(body.get("observationCounts"))
    r.check(
        "S5",
        "observation counts known",
        None
        if all(isinstance(counts.get(k), int) for k in ("watch", "cuff"))
        else f"observationCounts = {counts} (null means the store could not be asked in time)",
        ok=f"watch {counts.get('watch')}, cuff {counts.get('cuff')}",
    )
    openemr = _dict(body.get("openemr"))
    r.check(
        "S6",
        "OpenEMR reachable",
        None if openemr.get("reachable") is True else f"openemr.detail = {openemr.get('detail')!r}",
    )
    watch = _dict(body.get("watchSync"))
    state, reason = watch.get("state"), watch.get("reason")
    if reason == NOT_DEPLOYED:
        facts = StackFacts(False, state, reason, "health-sync is not deployed: synthetic data")
    else:
        facts = StackFacts(
            True, state, reason, f"health-sync is deployed (state {state!r}): real data"
        )
    return facts


def earlier_patient_is_synthetic(ctx: Context) -> bool | None:
    """A second opinion before anything is written: PID-3.1 of the newest earlier built reading
    in the messages log (``GET /messages``, then its trace's ``messageSent``). True or False
    when one was found, None when there is none to look at. Only built readings count: some T07
    samples keep their own synthetic patient whatever the stack's patient is."""
    response = ctx.api.call("GET", "/messages?kind=cuff-send&count=25")
    entries = _dict(response.json()).get("entries")
    if response.status != 200 or not isinstance(entries, list):
        return None
    for entry in entries:
        if not (
            isinstance(entry, dict)
            and entry.get("kind") == "cuff-send"
            and entry.get("sample") is None
            and entry.get("messageLookup") == "confirmed"
            and isinstance(entry.get("id"), str)
            and entry["id"]
        ):
            continue
        trace = ctx.api.call("GET", f"/trace/cuff-attempts/{entry['id']}")
        sent = _dict(trace.json()).get("messageSent")
        if trace.status != 200 or not isinstance(sent, str):
            continue
        try:
            patient_id = read_sent_reading(sent).patient_id
        except Exception:  # noqa: BLE001, S112 - hostile text: hl7apy may raise anything
            continue
        return patient_id.startswith(SYNTHETIC_PATIENT_PREFIX)
    return None


def guard(ctx: Context, facts: StackFacts) -> bool:
    """True when Part A may write. Records the decision either way."""
    r = ctx.report
    real = facts.real_data
    if not real:
        earlier = earlier_patient_is_synthetic(ctx)
        if earlier is False:
            r.warn(
                "S7",
                "stack data mode",
                "/status says synthetic, but an earlier reading names a non-synthetic "
                "patient: treated as real data",
            )
            real = True
        else:
            r.check(
                "S7",
                "stack data mode",
                None,
                ok=facts.why
                + (
                    "; an earlier reading names a SYN- patient"
                    if earlier
                    else "; no earlier reading to cross-check"
                ),
            )
    else:
        r.check("S7", "stack data mode", None, ok=facts.why)
    return not real or ctx.settings.allow_real_data_writes


# ---- sending ----------------------------------------------------------------------------------


def send(ctx: Context, body: dict) -> Response:
    """``POST /cuff/readings``. A 429 (cuff-sim's rate limit, checked before anything is built
    or sent) is waited out per ``Retry-After`` and retried; nothing was sent, so this is safe."""
    for attempt in range(ctx.settings.rate_limit_retries + 1):
        response = ctx.api.call("POST", "/cuff/readings", body)
        if response.status != 429 or attempt == ctx.settings.rate_limit_retries:
            return response
        try:
            wait = min(max(int(response.headers.get("retry-after", "10")), 1), 60)
        except ValueError:
            wait = 10
        ctx.report.out(f"      cuff-sim rate limit: waiting {wait} s (nothing was sent)")
        ctx.sleep(wait)
    raise AssertionError("unreachable")


def _resources(trace: dict, resource_type: str) -> list[dict]:
    bundle = _dict(trace.get("resources"))
    return [
        e["resource"]
        for e in (bundle.get("entry") if isinstance(bundle.get("entry"), list) else [])
        if isinstance(e, dict)
        and isinstance(e.get("resource"), dict)
        and e["resource"].get("resourceType") == resource_type
    ]


def wait_for_outcome(ctx: Context, attempt_id: str) -> tuple[dict, str | None]:
    """Poll the attempt's trace until the mapper has labelled the stored message (``accepted``
    with its Observation, or ``error``), or the poll timeout passes. Returns the last trace and
    the application outcome seen (None: still pending, not confirmed, or no answer)."""
    deadline = ctx.monotonic() + ctx.settings.poll_timeout
    trace: dict = {}
    while True:
        response = ctx.api.call("GET", f"/trace/cuff-attempts/{attempt_id}")
        outcome = None
        if response.status == 200:
            trace = _dict(response.json())
            application = _dict(_dict(trace.get("attempt")).get("application"))
            outcome = application.get("outcome")
            if outcome == "error" or (outcome == "accepted" and _resources(trace, "Observation")):
                return trace, outcome
        if ctx.monotonic() >= deadline:
            return trace, outcome
        ctx.sleep(ctx.settings.poll_interval)


# ---- Part A -----------------------------------------------------------------------------------


@dataclass
class Sent:
    systolic: int
    diastolic: int
    attempt_id: str
    observed_at: datetime | None
    message_name: str | None


def send_reading(ctx: Context, facts: StackFacts) -> Sent:
    r = ctx.report
    systolic, diastolic = ctx.choose_reading()
    response = send(
        ctx,
        {
            "systolic": systolic,
            "diastolic": diastolic,
            "transport": ctx.settings.transport,
            "sendMalformed": None,
        },
    )
    result = _dict(response.json())
    r.require(
        "A1",
        f"cuff reading {systolic}/{diastolic} mm[Hg] sent over {ctx.settings.transport}",
        None
        if response.status == 200 and isinstance(result.get("attemptId"), str)
        else f"HTTP {response.status} (reason {problem_reason(response)}), "
        f"attemptId {result.get('attemptId')!r}",
        ok=f"attempt {result.get('attemptId')}",
    )
    attempt_id = result["attemptId"]
    r.note("reading", f"{systolic}/{diastolic} mm[Hg] sent at {result.get('sentAt')}")
    r.note("cuff attempt (AuditEvent id)", attempt_id)
    r.note("MSH-10 control ID", str(result.get("controlId")))
    ack = _dict(result.get("ack"))
    r.require(
        "A2",
        "the HL7v2 store's ACK is AA",
        None
        if result.get("result") == "accepted"
        and ack.get("received") is True
        and ack.get("code") == "AA"
        else f"result {result.get('result')!r}, ACK {ack.get('code') or ack.get('why')!r}",
    )
    audit = _dict(result.get("audit"))
    r.check(
        "A3",
        "the send attempt was recorded as an AuditEvent",
        None if audit.get("recorded") is True else f"audit = {audit}",
    )
    try:
        message = read_sent_reading(str(result.get("messageSent") or ""))
    except Exception:  # noqa: BLE001 - hostile text: hl7apy may raise anything
        message = None
    problem = None
    if message is None:
        problem = "messageSent is not a readable HL7v2 message"
    elif not message.message_type.startswith("ORU^R01"):
        problem = f"MSH-9 is {message.message_type!r}, expected ORU^R01"
    elif message.control_id != result.get("controlId"):
        problem = "MSH-10 differs from the controlId cuff-sim reported"
    elif message.values != {SYSTOLIC: str(systolic), DIASTOLIC: str(diastolic)}:
        problem = f"OBX-5 values are {message.values}, expected {systolic}/{diastolic}"
    elif set(message.units.values()) != {fhir.MM_HG}:
        problem = f"OBX-6 units are {message.units}, expected mm[Hg]"
    r.require("A4", "the message sent is the ORU^R01 asked for", problem)
    assert message is not None
    if not facts.real_data:
        # The stack said synthetic before we wrote; the message must agree (module phr refuses
        # a non-SYN- patient with use_synthetic_data). If not, stop before sharing anything.
        r.require(
            "A5",
            "the message names a synthetic (SYN-) patient",
            None
            if message.patient_id.startswith(SYNTHETIC_PATIENT_PREFIX)
            else "PID-3.1 is not a SYN- identifier although /status reported synthetic data; "
            "stopping before the share",
        )
    else:
        r.skip("A5", "the message names a synthetic (SYN-) patient", "real-data stack")
    observed = message.observed_at.get(SYSTOLIC)
    return Sent(systolic, diastolic, attempt_id, observed, result.get("messageName"))


def check_stored(ctx: Context, sent: Sent) -> dict:
    r = ctx.report
    trace, outcome = wait_for_outcome(ctx, sent.attempt_id)
    attempt = _dict(trace.get("attempt"))
    observations = _resources(trace, "Observation")
    r.require(
        "A6",
        "v2-to-fhir mapped the message to an Observation",
        None
        if outcome == "accepted" and observations
        else f"after {ctx.settings.poll_timeout:g} s: application outcome {outcome!r}, "
        f"messageLookup {attempt.get('messageLookup')!r}, "
        f"reason {_dict(attempt.get('application')).get('reason')!r}",
    )
    observation = observations[0]
    observation_id = str(observation.get("id"))
    r.note("FHIR Observation", f"Observation/{observation_id}")
    for n, (what, problem) in enumerate(
        fhir.store_observation_checks(
            observation,
            systolic=sent.systolic,
            diastolic=sent.diastolic,
            observed_at=sent.observed_at,
        )
    ):
        r.check(f"A7{chr(ord('a') + n)}", f"Observation: {what}", problem)
    provenances = _resources(trace, "Provenance")
    name = attempt.get("derivedMessageName") or attempt.get("messageName") or sent.message_name
    r.check(
        "A8",
        "a Provenance links the Observation to the stored message",
        fhir.provenance_problem(provenances[0] if provenances else None, observation_id, name),
    )
    read = ctx.api.call("GET", f"/fhir/Observation/{observation_id}")
    read_body = _dict(read.json())
    r.check(
        "A9",
        "GET /fhir/Observation/<id> returns the same reading",
        (
            f"HTTP {read.status} (reason {problem_reason(read)})"
            if read.status != 200
            else fhir.values_problem(read_body, sent.systolic, sent.diastolic)
            or (None if read_body.get("id") == observation_id else "another id came back")
        ),
    )
    return observation


def share(ctx: Context, sent: Sent, observation: dict) -> None:
    r = ctx.report
    observation_id = str(observation.get("id"))
    effective = fhir.parse_instant(observation.get("effectiveDateTime"))
    first = ctx.api.call("POST", "/share", {"observationId": observation_id})
    body = _dict(first.json())
    problem = None
    if first.status != 200 or body.get("outcome") != "shared":
        problem = (
            f"HTTP {first.status} (reason {problem_reason(first)}), outcome {body.get('outcome')!r}"
        )
        if body.get("outcome") == "already-shared":
            # OpenEMR keys a vitals reading on code + Encounter + effectiveDateTime, and
            # cuff-sim stamps whole seconds: another reading sent in the same second collides.
            problem += (
                "; OpenEMR already holds a blood pressure at this exact second (another "
                "reading sent in the same second?)"
            )
    r.require("A10", "the share to OpenEMR answers outcome shared", problem)
    stored = _dict(_dict(body.get("readBack")).get("observation")).get("stored")
    if isinstance(stored, dict) and stored.get("id"):
        r.note("OpenEMR Observation", str(stored.get("id")))
    for n, (what, problem) in enumerate(
        fhir.openemr_observation_checks(
            stored, systolic=sent.systolic, diastolic=sent.diastolic, effective=effective
        )
    ):
        r.check(f"A11{chr(ord('a') + n)}", what, problem)
    second = ctx.api.call("POST", "/share", {"observationId": observation_id})
    again = _dict(second.json())
    again_stored = _dict(_dict(again.get("readBack")).get("observation")).get("stored")
    problem = None
    if second.status != 200 or again.get("outcome") != "already-shared":
        problem = (
            f"HTTP {second.status} (reason {problem_reason(second)}), "
            f"outcome {again.get('outcome')!r}"
        )
    elif (
        isinstance(stored, dict)
        and isinstance(again_stored, dict)
        and again_stored.get("id") != stored.get("id")
    ):
        problem = "the repeat names another OpenEMR Observation than the first share"
    r.check("A12", "sharing again answers already-shared (no second copy)", problem)


def negatives(ctx: Context) -> None:
    r = ctx.report
    transport = ctx.settings.transport
    # N1: refused by cuff-sim's input check (systolic must be above diastolic), forwarded by the
    # proxy unchanged: 422 with an OperationOutcome; nothing built, sent or recorded.
    bad = send(
        ctx, {"systolic": 80, "diastolic": 120, "transport": transport, "sendMalformed": None}
    )
    body = _dict(bad.json())
    issues = body.get("issue") if isinstance(body.get("issue"), list) else []
    r.check(
        "N1",
        "an invalid reading (80/120) is refused with 422 and nothing is sent",
        None
        if bad.status == 422
        and body.get("resourceType") == "OperationOutcome"
        and any(isinstance(i, dict) and i.get("code") == "invalid" for i in issues)
        and "attemptId" not in body
        else f"HTTP {bad.status}, body type {body.get('resourceType')!r}",
    )
    # N2: a message the HL7v2 store cannot parse: ACK AE, the message is not stored.
    rejected = send(ctx, {"transport": transport, "sendMalformed": REJECTED_SAMPLE})
    result = _dict(rejected.json())
    ack = _dict(result.get("ack"))
    attempt_id = result.get("attemptId")
    ok = (
        rejected.status == 200
        and result.get("result") == "rejected"
        and ack.get("code") == "AE"
        and isinstance(attempt_id, str)
    )
    r.check(
        "N2",
        f"an unparseable message ({REJECTED_SAMPLE}) gets ACK AE",
        None
        if ok
        else f"HTTP {rejected.status}, result {result.get('result')!r}, ACK {ack.get('code')!r}",
    )
    if ok:
        r.note("rejected attempt", str(attempt_id))
        trace = ctx.api.call("GET", f"/trace/cuff-attempts/{attempt_id}")
        body = _dict(trace.json())
        r.check(
            "N3",
            "the rejected message created no Observation",
            None
            if trace.status == 200
            and not _resources(body, "Observation")
            and _dict(body.get("attempt")).get("observation") is None
            else f"HTTP {trace.status}, Observation in the trace: "
            f"{bool(_resources(body, 'Observation'))}",
        )
    # N4: a message that is stored (AA) but cannot be mapped: labelled error, no Observation.
    unmappable = send(ctx, {"transport": transport, "sendMalformed": UNMAPPABLE_SAMPLE})
    result = _dict(unmappable.json())
    ack = _dict(result.get("ack"))
    attempt_id = result.get("attemptId")
    if not r.check(
        "N4",
        f"an unmappable message ({UNMAPPABLE_SAMPLE}) is stored with ACK AA",
        None
        if unmappable.status == 200 and ack.get("code") == "AA" and isinstance(attempt_id, str)
        else f"HTTP {unmappable.status}, ACK {ack.get('code')!r}",
    ):
        return
    r.note("unmappable attempt", str(attempt_id))
    trace, outcome = wait_for_outcome(ctx, str(attempt_id))
    application = _dict(_dict(trace.get("attempt")).get("application"))
    r.check(
        "N5",
        "v2-to-fhir labels it error and creates no Observation",
        None
        if outcome == "error" and not _resources(trace, "Observation")
        else f"application outcome {outcome!r} (reason {application.get('reason')!r}), "
        f"Observation in the trace: {bool(_resources(trace, 'Observation'))}",
        ok=f"reason {application.get('reason')}",
    )


WRITE_CHECKS = (
    ("A1", "cuff reading sent"),
    ("A10", "share to OpenEMR"),
    ("N1", "negative cuff checks"),
)


def run_part_a(ctx: Context, facts: StackFacts, *, may_write: bool) -> None:
    r = ctx.report
    if not may_write:
        reason = (
            "refused: the stack reports real data, and these checks write a synthetic reading "
            "into the patient's record and into OpenEMR; pass --allow-real-data-writes to run"
        )
        for check_id, title in WRITE_CHECKS:
            r.skip(check_id, title, reason)
        return
    try:
        sent = send_reading(ctx, facts)
        observation = check_stored(ctx, sent)
        share(ctx, sent, observation)
    except Stop as stop:
        r.stop("Part A (cuff path)", str(stop))
        if str(stop) == "A5":
            # The stack may hold real data after all: write nothing more.
            r.stop("Part A (negatives)", "A5")
            return
    # The negatives do not depend on the reading above, so they run even after a failure.
    try:
        negatives(ctx)
    except Stop as stop:
        r.stop("Part A (negatives)", str(stop))
