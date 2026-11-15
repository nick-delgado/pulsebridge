"""The web entry point: a Flask app with two routes.

- ``POST /sync``: one sync run. Cloud Scheduler calls it every 15 minutes with an OIDC token.
- ``GET /status``: whether the Google Health connection works now (``fhir-proxy``, T14, calls it
  for the dashboard's status strip).

Run in production with gunicorn (README, "Running"):

    gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 0 \
        "health_sync.app:create_app()"

Authentication happens before a request reaches this code: the Cloud Run service does not allow
unauthenticated calls, so Cloud Run checks the caller's OIDC token and ``roles/run.invoker``
(``scheduler-invoker`` for the sync; ``fhir-proxy`` for the status, T20/T22).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from flask import Flask, Response, jsonify
from pulsebridge_fhir import FhirInputError, PatientInput
from pulsebridge_gcp import HealthcareClient, RequestsTransport, default_transport

from health_sync.config import ConfigError, ServiceConfig
from health_sync.credentials import SecretsMissing, load_credentials
from health_sync.logs import configure_logging
from health_sync.status import CONNECTED, RECONNECT_NEEDED, ConnectionStatus, check_connection
from health_sync.sync import (
    Outcome,
    RunReport,
    SyncDependencies,
    run_sync,
    split_range,
    sync_range,
)

SYNC_PATH = "/sync"
STATUS_PATH = "/status"

logger = logging.getLogger("health_sync")


class _NoTransport:
    """A transport for building names only; any request through it is a bug."""

    def request(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("start-up checks must not send requests")


def check_startup(config: ServiceConfig) -> None:
    """Fail at start-up, so a bad deploy fails visibly, if the configuration cannot work.

    - the FHIR store name must form a valid resource name;
    - the patient id must be one T06 accepts.

    The secret files are checked too, but a missing one only logs a warning naming it: the
    service must start so ``/status`` can report ``not-configured`` (README, "Secrets")."""
    try:
        HealthcareClient(
            project=config.project_id,
            location=config.location,
            dataset=config.dataset_id,
            transport=_NoTransport(),
        ).fhir_store_name(config.fhir_store_id)
    except ValueError as exc:
        raise ConfigError("configuration does not form a valid FHIR store name") from exc
    try:
        PatientInput(config.patient_id)
    except FhirInputError as exc:
        raise ConfigError("PATIENT_ID is not a valid patient identifier") from exc
    try:
        load_credentials(config.secrets)
    except SecretsMissing as exc:
        logger.warning(
            "google health secrets not available at start-up",
            extra={"fields": {"event": "secrets-missing", "secrets": exc.names}},
        )


def default_dependencies(config: ServiceConfig) -> SyncDependencies:
    import requests

    return SyncDependencies(
        # A plain session for Google Health and OAuth: the user's access token is added by
        # TokenSource. Never the service account's AuthorizedSession, whose token must not be
        # sent to a non-GCP API.
        google_transport=RequestsTransport(requests.Session()),
        fhir=HealthcareClient(
            project=config.project_id,
            location=config.location,
            dataset=config.dataset_id,
            transport=default_transport(),  # the service's own service account (ADC)
            timeout=config.fhir_timeout,
        ),
    )


class StatusCache:
    """Reuses the last definite status for a short time, so a busy dashboard does not ask
    Google's token endpoint on every page load. In memory only; it is lost on restart, which is
    fine because the answer can always be recomputed."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._value: tuple[float, ConnectionStatus] | None = None

    def get(self) -> ConnectionStatus | None:
        with self._lock:
            if self._value and self._clock() - self._value[0] < self._seconds:
                return self._value[1]
            return None

    def put(self, status: ConnectionStatus) -> None:
        if status.cacheable and self._seconds > 0:
            with self._lock:
                self._value = (self._clock(), status)

    def clear(self) -> None:
        with self._lock:
            self._value = None


def log_report(event: str, report: RunReport) -> None:
    level = logging.INFO
    if report.outcome in (Outcome.RECONNECT_NEEDED, Outcome.NOT_CONFIGURED):
        level = logging.WARNING
    elif report.http_status >= 500:
        level = logging.ERROR
    logger.log(
        level,
        "%s: %s",
        event,
        report.outcome.value,
        extra={"fields": {"event": event, "http_status": report.http_status, **report.as_dict()}},
    )


def create_app(config: ServiceConfig | None = None, deps: SyncDependencies | None = None) -> Flask:
    """Build the app. With no arguments, configuration comes from the environment, the FHIR
    client uses Application Default Credentials and Google calls use a plain HTTPS session."""
    configure_logging()
    config = config or ServiceConfig.from_env()
    check_startup(config)
    deps = deps or default_dependencies(config)
    cache = StatusCache(config.status_cache_seconds, deps.monotonic)
    app = Flask("health_sync")

    @app.post(SYNC_PATH)
    def sync() -> tuple[Response, int]:
        try:
            whole = sync_range(deps.now(), config.lookback)
            report = run_sync(
                config, deps, ranges=split_range(whole), deadline_seconds=config.run_deadline
            )
        except Exception as exc:  # noqa: BLE001 - a bug must answer, not crash the worker
            logger.error("sync failed: %s", type(exc).__name__, extra={"fields": {"event": "bug"}})
            return jsonify({"outcome": "bug"}), 500
        log_report("sync-run", report)
        if report.outcome == Outcome.RECONNECT_NEEDED:
            cache.put(ConnectionStatus(RECONNECT_NEEDED, report.reason, deps.now()))
        elif report.outcome == Outcome.SYNCED and report.token_refreshes:
            # The token refresh just worked, so the connection is good: say so at once rather
            # than serving a cached "reconnect-needed" after the user has reconnected.
            cache.put(ConnectionStatus(CONNECTED, None, deps.now()))
        return jsonify(report.as_dict()), report.http_status

    @app.get(STATUS_PATH)
    def status() -> tuple[Response, int]:
        result = cache.get()
        if result is None:
            try:
                result = check_connection(
                    config,
                    deps.google_transport,
                    now=deps.now,
                    monotonic=deps.monotonic,
                    sleep=deps.sleep,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "status check failed: %s",
                    type(exc).__name__,
                    extra={"fields": {"event": "bug"}},
                )
                return jsonify({"googleHealth": "unknown", "reason": "bug"}), 500
            cache.put(result)
            logger.info(
                "status check: %s",
                result.state,
                extra={
                    "fields": {
                        "event": "status-check",
                        "google_health": result.state,
                        "reason": result.reason,
                    }
                },
            )
        return jsonify(result.as_dict()), result.http_status

    return app
