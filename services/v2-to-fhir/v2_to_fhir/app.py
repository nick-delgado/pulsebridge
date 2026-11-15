"""The web entry point: a Flask app with one route for Pub/Sub push.

Run in production with gunicorn (see README.md):

    gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 0 \
        "v2_to_fhir.app:create_app()"

Authentication happens before a request reaches this code: the Cloud Run service does not allow
unauthenticated calls, so Cloud Run checks the OIDC token Pub/Sub sends and the caller's
``roles/run.invoker`` (only the ``pubsub-push`` identity has it, T04/T20). This app checks only
the shape of the body and that the message belongs to its own HL7v2 store.
"""

from __future__ import annotations

import json
import logging
import sys

from flask import Flask, Response, request
from pulsebridge_gcp import HealthcareClient, default_transport

from v2_to_fhir.config import ConfigError, ServiceConfig
from v2_to_fhir.handler import Client, PushResult, handle_push

PUSH_PATH = "/pubsub/hl7v2"  # the push endpoint path T05's usage example points at

logger = logging.getLogger("v2_to_fhir")


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


def _log(result: PushResult) -> None:
    fields = {
        "event": result.event,
        "http_status": result.status,
        "application_outcome": result.outcome.value if result.outcome else None,
        "reason": result.reason.value if result.reason else None,
        **result.details,
    }
    level = logging.WARNING if result.warning or result.status >= 400 else logging.INFO
    logger.log(level, "hl7v2 notification: %s", result.event, extra={"fields": fields})


class _NoTransport:
    """A transport for building names only; any request through it is a bug."""

    def request(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("name check must not send requests")


def check_resource_names(config: ServiceConfig) -> None:
    """Fail at start-up, so a bad deploy fails visibly, if a configured id cannot form a valid
    resource name. Otherwise the service would start and answer every push with 500 until each
    notification was dead-lettered."""
    try:
        names = HealthcareClient(
            project=config.project_id,
            location=config.location,
            dataset=config.dataset_id,
            transport=_NoTransport(),
        )
        names.fhir_store_name(config.fhir_store_id)
        names.hl7v2_store_name(config.hl7v2_store_id)
    except ValueError as exc:
        raise ConfigError(f"configuration does not form valid resource names: {exc}") from exc


def create_app(config: ServiceConfig | None = None, client: Client | None = None) -> Flask:
    """Build the app. With no arguments, configuration comes from the environment and the client
    uses Application Default Credentials (the service's own service account on Cloud Run)."""
    _configure_logging()
    config = config or ServiceConfig.from_env()
    check_resource_names(config)
    if client is None:
        # One authorised session for the process, shared by gunicorn's threads (README,
        # "Running").
        client = HealthcareClient(
            project=config.project_id,
            location=config.location,
            dataset=config.dataset_id,
            transport=default_transport(),
        )
    app = Flask("v2_to_fhir")

    @app.post(PUSH_PATH)
    def pubsub_push() -> Response:
        envelope = request.get_json(silent=True)  # None for a body that is not JSON
        try:
            result = handle_push(envelope, client=client, config=config)
        except Exception as exc:  # noqa: BLE001 - a bug here must not take the worker down
            # Content errors never get here (the mapping catches them). This is a bug in the
            # handler: answer 500 so Pub/Sub retries and, after the attempt limit,
            # dead-letters the notification instead of stalling the subscription.
            logger.error(
                "handler failed: %s", type(exc).__name__, extra={"fields": {"event": "bug"}}
            )
            return Response(status=500)
        _log(result)
        return Response(status=result.status)

    return app
