"""The web entry point: a Flask app with two routes.

    POST /cuff/readings   send one reading (or a named malformed sample); returns the result
    GET  /cuff/samples    the malformed samples "send malformed" may use, per transport

Run in production with gunicorn (see README.md):

    gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 0 "cuff_sim.app:create_app()"

**No authentication code here, on purpose.** The Cloud Run service does not allow unauthenticated
calls, and only ``fhir-proxy``'s service account holds ``roles/run.invoker`` on it (D10, enforced
in T22). Cloud Run checks the proxy's OIDC token before a request reaches this code, and the
proxy has already checked the reviewer's login. There is no health or info route: nothing here
would tell an unauthenticated caller anything, and Cloud Run needs none.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from flask import Flask, Response, request
from pulsebridge_fhir import FhirInputError, NotAcknowledged, SendAttempt, now_ms
from pulsebridge_fhir import build_send_attempt_audit_event as _build_audit_event
from pulsebridge_gcp import HealthcareClient, default_transport
from pulsebridge_hl7v2 import HL7ValidationError

from cuff_sim.config import SERVICE_NAME, ConfigError, ServiceConfig
from cuff_sim.message import build_reading, malformed_samples
from cuff_sim.outcome import AckReceived
from cuff_sim.ratelimit import TokenBucket
from cuff_sim.request import RequestError, parse_request
from cuff_sim.service import CuffSimulator, SendResult

READINGS_PATH = "/cuff/readings"
SAMPLES_PATH = "/cuff/samples"

FHIR_JSON = "application/fhir+json"

logger = logging.getLogger("cuff_sim")


class _JsonFormatter(logging.Formatter):
    """One JSON object per line. Cloud Logging reads ``severity`` and ``message`` from it and
    keeps the other keys as structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def _configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(h.formatter, _JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _log(level: int, event: str, **fields: object) -> None:
    logger.log(level, "cuff-sim: %s", event, extra={"fields": {"event": event, **fields}})


def _send_log_fields(result: SendResult) -> dict:
    """What is logged for a send: codes, sizes and ids. Never values, patient data, message or
    ACK text, MSA-3 reasons or server messages (README, "Logging")."""
    outcome = result.outcome
    fields = {
        "transport": result.transport,
        "result": result.result,
        "sample": result.outgoing.sample_name,
        "framing": result.outgoing.framing,
        "control_id": result.outgoing.control_id,
        "ack_code": None,
        "no_ack_reason": None,
        "http_status": None,
        "message_name_source": result.message_name_source,
        "attempt_id": result.audit.attempt_id,
        "audit_recorded": result.audit.recorded,
        "audit_failure": result.audit.failure,
        "audit_http_status": result.audit.http_status,
        "duration_ms": result.duration_ms,
        **result.log_fields,
    }
    if isinstance(outcome, AckReceived):
        fields["ack_code"] = str(outcome.ack.code)
    else:
        fields["no_ack_reason"] = outcome.why
        fields["http_status"] = outcome.http_status
    return fields


def _operation_outcome(status: int, issues: list[tuple[str, str]], code: str) -> Response:
    """A FHIR R4 OperationOutcome (what the dashboard's client expects on an error)."""
    body = {
        "resourceType": "OperationOutcome",
        "issue": [
            {"severity": "error", "code": code, "diagnostics": text, "expression": [name]}
            for name, text in issues
        ],
    }
    return Response(json.dumps(body), status=status, content_type=FHIR_JSON)


class _NoTransport:
    """A transport for start-up checks: any request through it is a bug."""

    def request(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("start-up checks must not send requests")


def check_configuration(config: ServiceConfig) -> None:
    """Fail at start-up, so a bad deploy fails visibly, if the configuration cannot produce a
    valid message or AuditEvent. Builds both offline, from a fixed synthetic reading; nothing is
    sent. Messages name the variables, never their values."""
    names = HealthcareClient(
        project=config.project_id,
        location=config.location,
        dataset=config.dataset_id,
        transport=_NoTransport(),
    )
    try:
        store_name = names.hl7v2_store_name(config.hl7v2_store_id)
        names.fhir_store_name(config.audit_fhir_store_id)
    except ValueError as exc:
        raise ConfigError(
            "GCP_PROJECT_ID, HEALTHCARE_LOCATION, HEALTHCARE_DATASET_ID, HL7V2_STORE_ID and "
            "AUDIT_FHIR_STORE_ID do not form valid resource names"
        ) from exc
    try:
        build_reading(
            config,
            systolic=120,
            diastolic=80,
            control_id="STARTUPCHECK",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    except (ValueError, HL7ValidationError) as exc:
        raise ConfigError(
            "PATIENT_ID, PATIENT_ASSIGNING_AUTHORITY, PATIENT_FAMILY_NAME, PATIENT_GIVEN_NAME, "
            "SENDING_APPLICATION and SENDING_FACILITY must be printable ASCII without a "
            "backslash, and must form a valid HL7v2 message"
        ) from exc
    try:
        sent = now_ms()
        _build_audit_event(
            SendAttempt(
                sent_at=sent,
                transport="mllp",
                control_id="STARTUPCHECK",
                result=NotAcknowledged(why="timeout"),
                patient_id=config.patient_id,
                agent=SERVICE_NAME,
                destination=store_name,
            ),
            recorded=sent,
        )
    except FhirInputError as exc:
        raise ConfigError(
            "PATIENT_ID or the HL7v2 store name (GCP_PROJECT_ID: 6 to 30 characters, starting "
            "with a letter) cannot be written into an AuditEvent"
        ) from exc


def create_app(
    config: ServiceConfig | None = None,
    client: HealthcareClient | None = None,
    *,
    bucket: TokenBucket | None = None,
) -> Flask:
    """Build the app. With no arguments, configuration comes from the environment and the client
    uses Application Default Credentials (the service's own service account on Cloud Run)."""
    _configure_logging()
    config = config or ServiceConfig.from_env()
    check_configuration(config)
    if client is None:
        client = HealthcareClient(
            project=config.project_id,
            location=config.location,
            dataset=config.dataset_id,
            transport=default_transport(),
            timeout=config.https_timeout,
        )
    simulator = CuffSimulator(config, client)
    bucket = bucket or TokenBucket(
        per_minute=config.rate_limit_per_minute, burst=config.rate_limit_burst
    )
    app = Flask("cuff_sim")

    @app.get(SAMPLES_PATH)
    def samples() -> Response:
        body = [
            {
                "name": s.name,
                "label": s.description,
                "class": s.sample_class,
                "transports": list(s.transports),
            }
            for s in malformed_samples()
        ]
        return Response(json.dumps(body), status=200, content_type="application/json")

    @app.post(READINGS_PATH)
    def readings() -> Response:
        try:
            return _readings()
        except Exception as exc:  # noqa: BLE001 - a bug must not print content in a traceback
            _log(logging.ERROR, "bug", error_type=type(exc).__name__)
            return _operation_outcome(500, [("body", "internal error; see the logs")], "exception")

    def _readings() -> Response:
        try:
            send_request = parse_request(request.get_json(silent=True))
        except RequestError as exc:
            _log(
                logging.WARNING,
                "request-refused",
                http_status=exc.status,
                fields=sorted({name for name, _ in exc.issues}),
            )
            return _operation_outcome(exc.status, exc.issues, "invalid")

        # Rate limit after input checks (a typo does not use up a send) and before anything is
        # built or sent. Malformed samples count like readings.
        wait = bucket.take()
        if wait is not None:
            _log(logging.WARNING, "rate-limited", http_status=429, retry_after=wait)
            response = _operation_outcome(
                429, [("body", "too many sends; try again later")], "throttled"
            )
            response.headers["Retry-After"] = str(wait)
            return response

        try:
            outgoing = simulator.prepare(send_request)
        except HL7ValidationError as exc:
            # Inputs and configuration were checked, so this is a bug, not the caller's fault.
            # Nothing was sent and no attempt is recorded. The problems are counted, not logged:
            # T07's sentences can quote field values.
            _log(logging.ERROR, "validation-failed", problems=len(exc.problems))
            return _operation_outcome(
                500,
                [("body", "the built message failed HL7v2 validation; nothing was sent")],
                "structure",
            )

        result = simulator.send(outgoing, send_request.transport)
        fields = _send_log_fields(result)
        level = logging.INFO if result.audit.recorded else logging.WARNING
        _log(level, "cuff-send", **fields)
        if not result.audit.recorded:
            _log(
                logging.ERROR,
                "audit-write-failed",
                failure=result.audit.failure,
                http_status=result.audit.http_status,
                attempt_id=result.audit.attempt_id,
            )
        return Response(json.dumps(result.to_json()), status=200, content_type="application/json")

    return app
