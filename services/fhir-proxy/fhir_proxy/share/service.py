"""``POST <prefix>/share``: share one final reading with OpenEMR, and say exactly what happened.

The contract is the dashboard's (web/README.md, "For T15: the share route"; contract.ts,
parse.ts). In order:

1. **Refusals before OpenEMR is called** are ``Problem`` s (an OperationOutcome with a reason
   code): ``share-not-configured`` 503 (no OpenEMR settings, or the client document is not
   usable), ``invalid-body`` 400, ``not-found`` 404 (unknown, or another patient's: answered
   alike; so is one older than the shown window, T47), ``observation-not-final`` /
   ``observation-not-shareable`` / ``patient-incomplete``
   422, and ``share-busy`` 503 (another share holds this instance's lock until the budget runs
   out). Only these mean "nothing was sent" to the page.
2. **The sequence** (spike S1), each step recorded with ``extensions.trace_step`` as it
   completes: ``token``; ``patient-search`` by the ``PT`` identifier, ``patient-create`` only if
   none (OpenEMR ignores conditional create); ``encounter-search`` for the patient and the
   reading's UTC day, ``encounter-create`` only if none; ``observation-create`` (OpenEMR's
   "already exists" 400 is ``already-shared``); then ``patient-read``, ``encounter-read`` and
   ``observation-read`` by id (D13; by id because S1 found the Observation search returns
   placeholder vitals).
3. **200** ``{"outcome": "shared" | "already-shared", "steps", "readBack"}`` only when OpenEMR
   holds the reading. A failed read-back is a ``failed`` step and ``stored: null``; the
   outcome stays what it was.
4. **Any failure once OpenEMR has been called** (unreachable, refused, timed out, an unexpected
   status, an unreadable answer, the request budget spent, or a bug here) is the 502/504
   envelope ``{"operationOutcome", "steps"}`` with the steps so far. A step that got no answer
   has status 0. A step that was never sent (the budget ran out first) is not in the list,
   because nothing about it happened. Never a bare 5xx after the first call.

Shares are serialised per instance (one lock). Two instances (Cloud Run max 2) can still run
at the same moment; see README "Concurrency".
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

from flask import Response, request
from pulsebridge_fhir import codes
from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.resources import patient_for_sharing
from pulsebridge_gcp import HealthcareApiError, NotFoundError

from fhir_proxy import deadline
from fhir_proxy.bodies import read_body
from fhir_proxy.config import OpenEmrSettings
from fhir_proxy.errors import Problem, upstream_problem
from fhir_proxy.extensions import NOT_SHOWN, trace_step
from fhir_proxy.history import effective_in_window, window_start
from fhir_proxy.logs import log
from fhir_proxy.share import resources
from fhir_proxy.share.document import PROBLEMS, ClientDocument, DocumentProblem, load_document
from fhir_proxy.share.oauth import (
    TokenCache,
    client_assertion,
    oauth_error,
    read_token_answer,
    token_request_form,
)
from fhir_proxy.share.transport import (
    AnswerTooLarge,
    OpenEmrTimeout,
    OpenEmrUnreachable,
    Transport,
)
from fhir_proxy.store import SAFE_ID, Stores, entries

JSON_TYPE = "application/json"
MAX_SHARE_BODY_BYTES = 512
# How long a second share waits for the first on this instance, at most, and how much of the
# request budget must be left for its own sequence after waiting (nine calls).
LOCK_WAIT_SECONDS = 20.0
SEQUENCE_RESERVE_SECONDS = 20.0
BUSY_RETRY_AFTER = "5"
NOT_CONFIGURED = "Sharing with the clinic is not configured on the server"

# Any text from PulseBridge's own identifier systems (urn:pulsebridge:...).
OUR_SYSTEMS = "urn:pulsebridge:"
TOKEN_LABEL = "Access token (client credentials, signed assertion)"  # noqa: S105 - a label


class Stop(Exception):
    """The share stops after OpenEMR was called: answered with the envelope."""

    def __init__(self, status: int, reason: str, text: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.text = text


@dataclass
class Reply:
    status: int
    body: object  # the parsed JSON, or None when the answer was not JSON
    size: int
    duration_ms: int


def _strict_json(data: bytes) -> object:
    """JSON as RFC 8259 defines it: UTF-8, no NaN or Infinity, no repeated keys."""

    def no_constants(name: str) -> object:
        raise ValueError(name)

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict:
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(pairs)

    return json.loads(
        data.decode("utf-8"), parse_constant=no_constants, object_pairs_hook=no_duplicates
    )


def _invalid_body() -> Problem:
    return Problem(
        400,
        "invalid",
        "invalid-body",
        'The body must be {"observationId": "<id>"}.',
        expression=["observationId"],
    )


def _strings(value: object):  # noqa: ANN202 - a generator of str
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _created_id(body: object, *names: str) -> str | None:
    """The id in OpenEMR's (non-FHIR) create answer: ``{"uuid": ...}``, ``{"pid", "uuid"}`` or
    ``{"euuid", "encounter", "pid"}`` (S1); also looked for one level down in ``data``."""
    for holder in (body, body.get("data") if isinstance(body, dict) else None):
        if not isinstance(holder, dict):
            continue
        for name in names:
            value = holder.get(name)
            if isinstance(value, str) and SAFE_ID.fullmatch(value):
                return value
    return None


def _is_bundle(body: object) -> bool:
    return isinstance(body, dict) and body.get("resourceType") == "Bundle"


def _instant(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
    return datetime.max.replace(tzinfo=UTC)


class ShareService:
    def __init__(
        self,
        *,
        settings: OpenEmrSettings | None,
        stores: Stores,
        transport: Transport | None,
        tokens: TokenCache,
        clock: Callable[[], datetime],
        lock: threading.Lock | None = None,
        history_days: int = 7,
    ) -> None:
        self.settings = settings
        self.stores = stores
        self.transport = transport
        self.tokens = tokens
        self.clock = clock
        self.history_days = history_days
        # One share at a time on this instance (see the module docstring).
        self.lock = lock or threading.Lock()

    # ---- refusals before OpenEMR is called -------------------------------------------------

    def _document(self) -> ClientDocument:
        if self.settings is None or self.transport is None:
            raise Problem(503, "transient", "share-not-configured", f"{NOT_CONFIGURED}.")
        try:
            return load_document(self.settings)
        except DocumentProblem as problem:
            log(
                logging.WARNING,
                "openemr-client-document-unusable",
                problem=problem.code,
                field=problem.field,
            )
            raise Problem(
                503,
                "transient",
                "share-not-configured",
                f"{NOT_CONFIGURED}: {PROBLEMS[problem.code]}.",
            ) from None

    def _observation_id(self) -> str:
        if (request.mimetype or "") != JSON_TYPE:
            raise _invalid_body()
        try:
            data = read_body(MAX_SHARE_BODY_BYTES)
        except Problem as problem:
            if problem.status == 413:
                # The page reads only 400/404/422/503 as "nothing was sent"; an oversized body
                # is the same kind of refusal, so it gets the same status.
                raise _invalid_body() from None
            raise
        try:
            body = _strict_json(data)
        except (UnicodeDecodeError, ValueError):
            raise _invalid_body() from None
        if not isinstance(body, dict) or set(body) != {"observationId"}:
            raise _invalid_body()
        value = body["observationId"]
        if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
            raise _invalid_body()
        return value

    def _load(self, observation_id: str) -> tuple[dict, dict]:
        """The Observation (pinned to the configured patient) and the Patient it is about."""
        not_found = Problem(404, "not-found", "not-found", "The Observation was not found.")
        try:
            refs = self.stores.patient_references()
            observation = self.stores.read("Observation", observation_id)
        except NotFoundError:
            raise not_found from None
        except HealthcareApiError as exc:
            raise upstream_problem(exc, "the FHIR store") from None
        if (
            not isinstance(observation, dict)
            or not self.stores.is_ours(observation, refs)
            or not effective_in_window(observation, window_start(self.clock(), self.history_days))
        ):
            # Another patient's, or one older than the shown window (T47), answers exactly like
            # a missing one. Checked before anything else about the reading, so a 422 never
            # tells an old reading's status. The read-back that follows a share is of this
            # reading, so it covers the same window.
            raise not_found
        status = observation.get("status")
        if status != "final":
            named = status if status in resources.OBSERVATION_STATUSES else "not final"
            raise Problem(
                422,
                "business-rule",
                "observation-not-final",
                f"Only final Observations are shared; this one is {named}.",
                expression=["Observation.status"],
            )
        if resources.kind_of(observation) is None:
            raise Problem(
                422,
                "business-rule",
                "observation-not-shareable",
                "Only heart-rate and blood-pressure readings are shared.",
                expression=["Observation.code"],
            )
        if resources.utc_day(observation) is None:
            raise Problem(
                422,
                "business-rule",
                "observation-not-shareable",
                "The reading has no time with a time zone, so no Encounter day can be chosen.",
                expression=["Observation.effective[x]"],
            )
        patient_ref = observation["subject"]["reference"]
        try:
            patient = self.stores.read("Patient", patient_ref.split("/", 1)[1])
        except NotFoundError:
            patient = None
        except HealthcareApiError as exc:
            raise upstream_problem(exc, "the FHIR store") from None
        if not isinstance(patient, dict):
            raise Problem(
                422,
                "required",
                "patient-incomplete",
                "The stored Patient is incomplete: missing the Patient record.",
            )
        return observation, patient

    # ---- the route -------------------------------------------------------------------------

    def handle(self) -> Response:
        document = self._document()
        observation_id = self._observation_id()
        observation, stored_patient = self._load(observation_id)
        try:
            patient = patient_for_sharing(stored_patient)
        except FhirInputError as exc:
            # The library's message names only what is missing, from a fixed set of phrases.
            missing = str(exc).removeprefix("complete patient: ")
            raise Problem(
                422,
                "required",
                "patient-incomplete",
                f"The stored Patient is incomplete: {missing}.",
                expression=["Patient"],
            ) from None
        self._guard(observation, stored_patient, patient)

        # Wait for a share already running here, but only while enough of this request's budget
        # would be left for its own sequence afterwards.
        left = deadline.remaining(LOCK_WAIT_SECONDS + SEQUENCE_RESERVE_SECONDS)
        wait = min(LOCK_WAIT_SECONDS, left - SEQUENCE_RESERVE_SECONDS)
        acquired = self.lock.acquire(timeout=wait) if wait > 0 else self.lock.acquire(False)
        if not acquired:
            raise Problem(
                503,
                "transient",
                "share-busy",
                "Another share is in progress on this server; nothing was sent. Try again in a "
                "few seconds.",
                headers={"Retry-After": BUSY_RETRY_AFTER},
            )
        try:
            return ShareRun(self, document, observation, patient).run()
        finally:
            self.lock.release()

    def _guard(self, observation: dict, stored_patient: dict, patient: dict) -> None:
        """Nothing of our store may reach OpenEMR: checked on the resources as they will be
        sent, before anything is sent. A failure here is a bug (500), not the caller's."""
        internal = [observation.get("id") or "", stored_patient.get("id") or ""]
        device = observation.get("device")
        if isinstance(device, dict) and isinstance(device.get("reference"), str):
            internal.append(device["reference"])
        for identifier in list(observation.get("identifier") or []) + list(
            stored_patient.get("identifier") or []
        ):
            if isinstance(identifier, dict) and identifier.get("system") != codes.PB_PATIENT_ID:
                internal.append(str(identifier.get("value") or ""))
        template = resources.observation_for_openemr(
            observation, "Patient/openemr-patient", "Encounter/openemr-encounter"
        )
        # No PulseBridge identifier system anywhere in the Observation; in the Patient only the
        # PT identifier's own system (the clinic finds the Patient by it).
        observation_leaks = resources.leaks(template, [*internal, OUR_SYSTEMS, "urn:uuid:"])
        without_pt_system = json.loads(json.dumps(patient))
        for identifier in without_pt_system.get("identifier") or []:
            if isinstance(identifier, dict) and identifier.get("system") == codes.PB_PATIENT_ID:
                identifier.pop("system")
        patient_leaks = resources.leaks(without_pt_system, [*internal, OUR_SYSTEMS])
        wrong_refs = set(resources.references(template)) - {"subject", "encounter"}
        if observation_leaks or patient_leaks or wrong_refs or "meta" in patient or "id" in patient:
            log(logging.ERROR, "share-blocked", reason="internal-value")
            # Nothing has been sent: a plain refusal, with no steps (README, "Failures").
            raise Problem(
                500,
                "exception",
                "share-blocked",
                "The share was stopped before anything was sent: what would have been sent "
                "carried a value internal to PulseBridge. This is a fault in the server.",
            )


class ShareRun:
    """One share: the sequence against OpenEMR, and the steps it records."""

    def __init__(
        self, service: ShareService, document: ClientDocument, observation: dict, patient: dict
    ) -> None:
        self.service = service
        self.settings: OpenEmrSettings = service.settings  # type: ignore[assignment]
        self.transport: Transport = service.transport  # type: ignore[assignment]
        self.document = document
        self.observation = observation
        self.patient = patient
        self.steps: list[dict] = []
        self.called = False  # set just before the first request to OpenEMR is handed over
        self.pending = "token"
        self.access_token: str | None = None
        # This run's signed assertions and access token. OpenEMR may echo one back (an OAuth
        # error description, a FHIR error message); every recorded string is scrubbed of them.
        self.secrets: list[str] = []
        # A request handed to the transport whose step is not recorded yet. If the proxy fails
        # before recording it, the step is still listed (sent; outcome not known).
        self.in_flight: dict | None = None
        self.started = time.monotonic()

    # ---- plumbing ----------------------------------------------------------------------------

    def _fhir(self, path: str) -> str:
        return f"{self.settings.fhir_path}{path}"

    def _scrub(self, value: object) -> object:
        """Every string the page will see, at any depth: OpenEMR's own address removed (bundle
        fullUrls, links: the page is never told a host), and this run's assertion and access
        token replaced wherever OpenEMR echoed them. Redaction by key name alone would miss an
        echo inside a message."""
        site = self.settings.site_address
        if isinstance(value, str):
            text = value.replace(site, "")
            for secret in self.secrets:
                if secret:
                    text = text.replace(secret, NOT_SHOWN)
            return text
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value

    def record(
        self,
        step: str,
        label: str,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        request_body: object = None,
        reply: Reply | None,
        result: str,
    ) -> None:
        self.steps.append(
            trace_step(
                step,
                self._scrub(label),  # type: ignore[arg-type]
                method=method,
                path=path,
                query=query,
                request_body=self._scrub(request_body),
                status=0 if reply is None else reply.status,
                response_body=None if reply is None else self._scrub(reply.body),
                result=result,
                duration_ms=0 if reply is None else reply.duration_ms,
            )
        )
        if self.in_flight is not None and self.in_flight["step"] == step:
            self.in_flight = None

    def flush_in_flight(self) -> None:
        """List a request that was sent but whose step was never recorded (the proxy failed in
        between): status 0, ``failed``, and a label saying its outcome is not known. Without it
        the page would conclude that request was never sent."""
        pending, self.in_flight = self.in_flight, None
        if pending is None:
            return
        label = (
            f"{pending['label']}: sent, but the proxy failed before it could read the answer; "
            "whether it took effect is not known"
        )
        for query in (pending["query"], None):
            try:
                self.record(
                    pending["step"],
                    label,
                    pending["method"],
                    pending["path"],
                    query=query,
                    request_body=pending["body"],
                    reply=None,
                    result="failed",
                )
                return
            except ValueError:
                continue

    def call(
        self,
        step: str,
        label: str,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | None = None,
        json_body: dict | None = None,
        form: dict[str, str] | None = None,
    ) -> Reply:
        """Send one request. Transport failures are recorded as a failed step and raised as
        :class:`Stop`. ``deadline.capped`` raises ``BudgetExhausted`` BEFORE anything is sent."""
        self.pending = step
        timeout = deadline.capped(self.settings.timeout)
        headers = {"Accept": "application/fhir+json, application/json"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/fhir+json"
        elif form is not None:
            data = urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if step != "token":
            headers["Authorization"] = f"Bearer {self.access_token}"
        trace_query = None if query is None else dict(query)
        trace_body = json_body if json_body is not None else form
        began = time.monotonic()
        self.called = True
        self.in_flight = {
            "step": step,
            "label": label,
            "method": method,
            "path": path,
            "query": trace_query,
            "body": trace_body,
        }
        try:
            answer = self.transport.request(
                method,
                path,
                query=query,
                headers=headers,
                body=data,
                timeout=timeout,
                context=self.document.ssl_context(),
            )
        except OpenEmrTimeout:
            self.record(
                step,
                f"{label}: no answer in time",
                method,
                path,
                query=trace_query,
                request_body=trace_body,
                reply=None,
                result="failed",
            )
            raise Stop(
                504,
                "openemr-timeout",
                f"OpenEMR did not answer the {step} step in time; it is not known whether that "
                "step took effect.",
            ) from None
        except OpenEmrUnreachable as exc:
            self.record(
                step,
                f"{label}: no answer ({exc.reason})",
                method,
                path,
                query=trace_query,
                request_body=trace_body,
                reply=None,
                result="failed",
            )
            raise Stop(
                502,
                "openemr-unreachable",
                f"OpenEMR could not be reached at the {step} step ({exc.reason}).",
            ) from None
        except AnswerTooLarge as exc:
            # Status 0: the answer was not read, so nothing about it (not even its status) is a
            # claim about what OpenEMR did.
            reply = Reply(0, None, 0, int((time.monotonic() - began) * 1000))
            self.record(
                step,
                f"{label}: answer too large to read (HTTP {exc.status}); whether it took effect "
                "is not known",
                method,
                path,
                query=trace_query,
                request_body=trace_body,
                reply=reply,
                result="failed",
            )
            raise Stop(
                502,
                "openemr-refused",
                f"OpenEMR's answer to the {step} step was too large to read; it is not known "
                "whether that step took effect.",
            ) from None
        try:
            body: object = json.loads(answer.body.decode("utf-8")) if answer.body else None
        except (UnicodeDecodeError, ValueError, RecursionError):
            # RecursionError: a body nested deeper than the parser's stack ("[[[[...").
            body = None  # an answer not understood; never passed on
        reply = Reply(answer.status, body, len(answer.body), int((time.monotonic() - began) * 1000))
        if answer.status == 401 and step != "token":
            # The token was refused (revoked, or expired early): never reuse it.
            self.service.tokens.forget(self.document.client_id)
        return reply

    def refused(self, step: str, reply: Reply, what: str = "refused") -> Stop:
        return Stop(
            502,
            "openemr-refused",
            f"OpenEMR {what} the {step} step (HTTP {reply.status}); the steps show what was "
            "done before it.",
        )

    # ---- the sequence ------------------------------------------------------------------------

    def run(self) -> Response:
        try:
            outcome, read_back = self._sequence()
        except Stop as stop:
            self.flush_in_flight()
            return self._envelope(stop)
        except deadline.BudgetExhausted:
            self.flush_in_flight()
            if not self.called:
                raise Problem(
                    503,
                    "transient",
                    "share-busy",
                    "The server ran out of time before calling OpenEMR; nothing was sent. Try "
                    "again in a few seconds.",
                    headers={"Retry-After": BUSY_RETRY_AFTER},
                ) from None
            return self._envelope(
                Stop(
                    504,
                    "openemr-timeout",
                    f"The request's time budget ran out before the {self.pending} step could be "
                    "sent; nothing after the steps shown was sent.",
                )
            )
        except Exception as exc:
            log(logging.ERROR, "bug", error_type=type(exc).__name__, endpoint="share")
            if not self.called:
                raise
            self.flush_in_flight()
            # After OpenEMR was called a bare 500 would hide what was already done there.
            return self._envelope(
                Stop(
                    502,
                    "share-internal-error",
                    "The share stopped on an internal error after OpenEMR was called; the "
                    "steps show what was done.",
                )
            )
        log(
            logging.INFO,
            "share-finished",
            outcome=outcome,
            steps=len(self.steps),
            duration_ms=int((time.monotonic() - self.started) * 1000),
        )
        return Response(
            json.dumps({"outcome": outcome, "steps": self.steps, "readBack": read_back}),
            status=200,
            content_type=JSON_TYPE,
        )

    def _envelope(self, stop: Stop) -> Response:
        last = self.steps[-1] if self.steps else None
        log(
            logging.WARNING,
            "share-failed",
            reason=stop.reason,
            step=self.pending,
            http_status=None if last is None else last["response"]["status"],
            steps=len(self.steps),
        )
        code = "timeout" if stop.status == 504 else "exception"
        outcome = Problem(stop.status, code, stop.reason, stop.text).outcome()
        return Response(
            json.dumps({"operationOutcome": outcome, "steps": self.steps}),
            status=stop.status,
            content_type=JSON_TYPE,
        )

    def _sequence(self) -> tuple[str, dict]:
        self._token()
        patient_id, patient_sent = self._patient()
        day = resources.utc_day(self.observation)
        assert day is not None  # checked before the lock  # noqa: S101
        encounter_id, encounter_sent = self._encounter(patient_id, day)
        observation_sent = resources.observation_for_openemr(
            self.observation, f"Patient/{patient_id}", f"Encounter/{encounter_id}"
        )
        outcome, observation_id = self._observation(observation_sent)
        stored_patient = self._read("patient-read", "Patient", patient_id)
        stored_encounter = self._read("encounter-read", "Encounter", encounter_id)
        stored_observation = None
        if observation_id is not None:
            stored_observation = self._read(
                "observation-read",
                "Observation",
                observation_id,
                expect={
                    "subject": f"Patient/{patient_id}",
                    "encounter": f"Encounter/{encounter_id}",
                    "code": resources.sent_code(self.observation),
                },
            )
        return outcome, {
            "patient": {"sent": patient_sent, "stored": stored_patient},
            "encounter": {"sent": encounter_sent, "stored": stored_encounter},
            "observation": {"sent": observation_sent, "stored": stored_observation},
        }

    def _token(self) -> None:
        path = self.settings.token_path
        cached = self.service.tokens.get(self.document)
        if cached is not None:
            self.access_token, age = cached
            self.secrets.append(self.access_token)
            # No request was made: result "reused", no bodies, no status.
            self.steps.append(
                trace_step(
                    "token",
                    f"Access token reused from this proxy's memory (obtained {int(age)} s ago, "
                    "still valid): no token request was sent",
                    method="POST",
                    path=path,
                    request_body=None,
                    status=0,
                    response_body=None,
                    result="reused",
                    duration_ms=0,
                )
            )
            return
        for attempt in (1, 2):
            assertion = client_assertion(self.document, self.service.clock())
            self.secrets.append(assertion)
            form = token_request_form(self.document, assertion)
            reply = self.call("token", TOKEN_LABEL, "POST", path, form=form)
            answer = read_token_answer(reply.body) if reply.status == 200 else None
            if answer is not None:
                self.secrets.append(answer.access_token)
                self.record(
                    "token", TOKEN_LABEL, "POST", path, request_body=form, reply=reply, result="ok"
                )
                self.service.tokens.put(self.document, answer)
                self.access_token = answer.access_token
                return
            if oauth_error(reply.body) == "invalid_client" and attempt == 1:
                # A re-registration may just have replaced the document: read it once more.
                self.record(
                    "token",
                    f"{TOKEN_LABEL}: refused (invalid_client); the client document is read "
                    "again and the request sent once more",
                    "POST",
                    path,
                    request_body=form,
                    reply=reply,
                    result="failed",
                )
                self.service.tokens.forget(self.document.client_id)
                try:
                    self.document = load_document(self.settings)
                except DocumentProblem:
                    raise Stop(
                        502,
                        "openemr-refused",
                        "OpenEMR refused the client (invalid_client), and the client document "
                        "read again is not usable.",
                    ) from None
                continue
            self.record(
                "token",
                f"{TOKEN_LABEL}: refused",
                "POST",
                path,
                request_body=form,
                reply=reply,
                result="failed",
            )
            error = oauth_error(reply.body)
            raise Stop(
                502,
                "openemr-refused",
                f"OpenEMR refused the access token request (HTTP {reply.status}"
                + (f", {error}" if error else "")
                + ").",
            )

    def _not_understood(self, step: str, reply: Reply) -> Stop:
        return Stop(
            502,
            "openemr-refused",
            f"OpenEMR's answer to the {step} step (HTTP {reply.status}) could not be understood; "
            "it is not known whether that step took effect.",
        )

    def _create(
        self, step: str, label: str, path: str, sent: dict, *id_names: str
    ) -> tuple[str | None, Reply]:
        """POST one resource. Success is ONLY a 201 whose JSON object names a valid id
        (OpenEMR's non-FHIR create answers, S1). Any other 2xx is "answered but not
        understood": recorded as failed and stopped, because whether it was created is not
        known. Returns (id, reply) for a 201 with an id; (None, reply) for a non-2xx answer, for
        the caller to judge."""
        reply = self.call(step, label, "POST", path, json_body=sent)
        created = _created_id(reply.body, *id_names) if reply.status == 201 else None
        if created is not None:
            self.record(step, label, "POST", path, request_body=sent, reply=reply, result="ok")
            return created, reply
        if 200 <= reply.status < 300:
            self.record(
                step,
                f"{label}: OpenEMR answered HTTP {reply.status} but its answer could not be "
                "understood; whether it was created is not known",
                "POST",
                path,
                request_body=sent,
                reply=reply,
                result="failed",
            )
            raise self._not_understood(step, reply)
        return None, reply

    def _patient(self) -> tuple[str, dict | None]:
        """Search by the PT identifier's value (S1: OpenEMR matches the value exactly and
        ignores the system), create only if nothing is found."""
        value = self.patient["identifier"][0]["value"]
        path = self._fhir("/Patient")
        label = "Search Patient by identifier"
        query = [("identifier", value)]
        reply = self.call("patient-search", label, "GET", path, query=query)
        if reply.status != 200 or not _is_bundle(reply.body):
            self.record(
                "patient-search",
                label,
                "GET",
                path,
                query=dict(query),
                reply=reply,
                result="failed",
            )
            raise self.refused("patient-search", reply)
        found = entries(reply.body, "Patient")
        matching = sorted(
            r["id"]
            for r in found
            if isinstance(r.get("id"), str)
            and SAFE_ID.fullmatch(r["id"])
            and any(
                isinstance(i, dict) and i.get("value") == value for i in r.get("identifier") or []
            )
        )
        if found and not matching:
            # Never create on a guess: a second Patient at the clinic is worse than a stop.
            self.record(
                "patient-search",
                f"{label}: the Patients found do not carry it, or have no usable id",
                "GET",
                path,
                query=dict(query),
                reply=reply,
                result="failed",
            )
            raise self.refused("patient-search", reply, "answered unexpectedly at")
        if matching:
            if len(matching) > 1:
                label += f" ({len(matching)} found, a duplicate: the first by id is used)"
            self.record(
                "patient-search", label, "GET", path, query=dict(query), reply=reply, result="found"
            )
            return matching[0], None
        self.record(
            "patient-search", label, "GET", path, query=dict(query), reply=reply, result="not-found"
        )
        created, reply = self._create(
            "patient-create", "Create Patient", path, self.patient, "uuid"
        )
        if created is None:
            self.record(
                "patient-create",
                "Create Patient",
                "POST",
                path,
                request_body=self.patient,
                reply=reply,
                result="failed",
            )
            raise self.refused("patient-create", reply)
        return created, self.patient

    def _encounter(self, patient_id: str, day: str) -> tuple[str, dict | None]:
        path = self._fhir("/Encounter")
        label = "Search Encounter for the reading's UTC day"
        query = [("patient", patient_id), ("date", day)]
        reply = self.call("encounter-search", label, "GET", path, query=query)
        if reply.status != 200 or not _is_bundle(reply.body):
            self.record(
                "encounter-search",
                label,
                "GET",
                path,
                query=dict(query),
                reply=reply,
                result="failed",
            )
            raise self.refused("encounter-search", reply)
        ours = f"Patient/{patient_id}"
        candidates = []
        for resource in entries(reply.body, "Encounter"):
            rid = resource.get("id")
            if not (isinstance(rid, str) and SAFE_ID.fullmatch(rid)):
                continue
            subject = resource.get("subject")
            # Only an Encounter that names exactly our Patient is reused; one with no subject
            # is not known to be ours.
            if not (isinstance(subject, dict) and subject.get("reference") == ours):
                continue
            period = resource.get("period") if isinstance(resource.get("period"), dict) else {}
            candidates.append((_instant(period.get("start")), rid))
        if candidates:
            candidates.sort()
            if len(candidates) > 1:
                label += f" ({len(candidates)} found: the oldest is reused)"
            self.record(
                "encounter-search",
                label,
                "GET",
                path,
                query=dict(query),
                reply=reply,
                result="found",
            )
            return candidates[0][1], None
        self.record(
            "encounter-search",
            label,
            "GET",
            path,
            query=dict(query),
            reply=reply,
            result="not-found",
        )
        sent = resources.encounter_for_openemr(ours, day)
        label = "Create Encounter for the reading's UTC day"
        created, reply = self._create("encounter-create", label, path, sent, "euuid", "uuid")
        if created is None:
            self.record(
                "encounter-create",
                label,
                "POST",
                path,
                request_body=sent,
                reply=reply,
                result="failed",
            )
            raise self.refused("encounter-create", reply)
        return created, sent

    def _observation(self, sent: dict) -> tuple[str, str | None]:
        """POST the reading. Returns the outcome and OpenEMR's id for it (None when OpenEMR said
        it already holds the reading but did not name exactly one Observation)."""
        path = self._fhir("/Observation")
        label = f"Create Observation ({resources.kind_of(self.observation)})"
        created, reply = self._create("observation-create", label, path, sent, "uuid")
        if created is not None:
            return "shared", created
        # OpenEMR 8.5.0's refusal of a repeated reading (FhirObservationVitalsService), with
        # the code that was sent:
        #   A "<code>" reading already exists for this encounter at this effectiveDateTime
        #   [ (Observation/<uuid>)]; POST does not overwrite a stored reading -- ...
        # The id is there only when OpenEMR could map it.
        phrase = re.compile(
            rf'A "{re.escape(resources.sent_code(self.observation))}" reading already exists '
            r"for this encounter at this effectiveDateTime"
            r"(?: \(Observation/([A-Za-z0-9.-]{1,64})\))?;"
        )
        matches = [m for t in _strings(reply.body) for m in [phrase.match(t)] if m]
        if reply.status == 400 and matches:
            # Exactly one distinct id named, and a valid FHIR id: only then is it read back.
            named = sorted({m.group(1) for m in matches if m.group(1)})
            if len(named) == 1 and SAFE_ID.fullmatch(named[0]):
                self.record(
                    "observation-create",
                    f"{label}: OpenEMR already holds this reading (Observation/{named[0]}); "
                    "nothing new was written",
                    "POST",
                    path,
                    request_body=sent,
                    reply=reply,
                    result="already-shared",
                )
                return "already-shared", named[0]
            self.record(
                "observation-create",
                f"{label}: OpenEMR says it already holds this reading but did not name exactly "
                "one existing Observation, so none is read back",
                "POST",
                path,
                request_body=sent,
                reply=reply,
                result="already-shared",
            )
            return "already-shared", None
        self.record(
            "observation-create",
            label,
            "POST",
            path,
            request_body=sent,
            reply=reply,
            result="failed",
        )
        raise self.refused("observation-create", reply)

    def _read(
        self, step: str, resource_type: str, resource_id: str, expect: dict | None = None
    ) -> dict | None:
        """A read back by id (D13). Its failure is a failed step and ``stored: null``: the
        reading is already in OpenEMR, so the share's outcome does not change. ``expect``: for
        the Observation, the Patient, Encounter and code it must have, so a wrong record (an id
        OpenEMR named that is not this reading) is never shown as ours."""
        path = self._fhir(f"/{resource_type}/{resource_id}")
        label = f"Read back the {resource_type}"
        try:
            reply = self.call(step, label, "GET", path)
        except Stop:
            return None  # recorded by call()
        except deadline.BudgetExhausted:
            self.steps.append(
                trace_step(
                    step,
                    f"{label}: not sent, the request's time budget was spent",
                    method="GET",
                    path=path,
                    request_body=None,
                    status=0,
                    response_body=None,
                    result="failed",
                    duration_ms=0,
                )
            )
            return None
        except Exception as exc:  # noqa: BLE001 - a read-back never undoes a share
            log(logging.ERROR, "bug", error_type=type(exc).__name__, endpoint="share-read")
            self.flush_in_flight()
            return None
        body = reply.body
        if not (
            reply.status == 200
            and isinstance(body, dict)
            and body.get("resourceType") == resource_type
            and body.get("id") == resource_id
        ):
            self.record(step, label, "GET", path, reply=reply, result="failed")
            return None
        if expect is not None and not _matches(body, expect):
            self.record(
                step,
                f"{label}: OpenEMR answered with an Observation that is not this reading "
                "(another Patient, Encounter or code), so it is not shown",
                "GET",
                path,
                reply=reply,
                result="failed",
            )
            return None
        self.record(step, label, "GET", path, reply=reply, result="ok")
        stored = self._scrub(body)
        return stored if isinstance(stored, dict) else None


def _matches(observation: dict, expect: dict) -> bool:
    """Whether a read-back Observation is the reading we shared: our OpenEMR Patient, our
    Encounter and the LOINC code that was sent."""

    def reference(name: str) -> object:
        value = observation.get(name)
        return value.get("reference") if isinstance(value, dict) else None

    codes_found = {
        c.get("code")
        for c in (observation.get("code") or {}).get("coding") or []
        if isinstance(c, dict) and c.get("system") == resources.LOINC
    }
    return (
        reference("subject") == expect["subject"]
        and reference("encounter") == expect["encounter"]
        and expect["code"] in codes_found
    )
