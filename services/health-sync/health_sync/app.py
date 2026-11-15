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
from health_sync.status import (
    CONNECTED,
    RECONNECT_NEEDED,
    UNKNOWN,
    ConnectionStatus,
    check_connection,
)
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
# How long /status reuses an answer: a definite one for STATUS_CACHE_SECONDS (60 by default),
# "unknown" for this long, so a failing token endpoint is not asked on every page load either.
UNKNOWN_CACHE_SECONDS = 15.0

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
            extra={
                "fields": {"event": "secrets-missing", "secrets": exc.names, "reason": exc.reason}
            },
        )


def default_dependencies(config: ServiceConfig) -> SyncDependencies:
    import requests

    # The service's own service account (Application Default Credentials), one authorised
    # session for the process. A HealthcareClient is cheap to build, so each FHIR request gets
    # its own with a timeout cut to the time the run has left (sync.py, "Time").
    fhir_transport = default_transport()

    def fhir(timeout: float) -> HealthcareClient:
        return HealthcareClient(
            project=config.project_id,
            location=config.location,
            dataset=config.dataset_id,
            transport=fhir_transport,
            timeout=timeout,
        )

    return SyncDependencies(
        # A plain session for Google Health and OAuth: the user's access token is added by
        # TokenSource. Never the service account's AuthorizedSession, whose token must not be
        # sent to a non-GCP API.
        google_transport=RequestsTransport(requests.Session()),
        fhir=fhir,
    )


class StatusCache:
    """Reuses the last status for a short time, so a busy dashboard does not ask Google's token
    endpoint on every page load. In memory only; lost on restart, which is fine because the
    answer can always be recomputed."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._value: tuple[float, float, ConnectionStatus] | None = None

    def get(self) -> ConnectionStatus | None:
        with self._lock:
            if self._value and self._clock() - self._value[0] < self._value[1]:
                return self._value[2]
            return None

    def put(self, status: ConnectionStatus) -> None:
        ttl = (
            min(self._seconds, UNKNOWN_CACHE_SECONDS) if status.state == UNKNOWN else self._seconds
        )
        if ttl > 0:
            with self._lock:
                self._value = (self._clock(), ttl, status)


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
    # One run at a time per instance. Scheduler can start a second run while the first is
    # still going (a slow run past its attempt deadline, or a manual call); the second one
    # answers at once instead of writing the same windows in parallel. With Cloud Run's
    # max_instance_count = 1 (README, T20) this covers the whole service.
    sync_lock = threading.Lock()
    app = Flask("health_sync")
    app.extensions["health_sync_lock"] = sync_lock

    @app.post(SYNC_PATH)
    def sync() -> tuple[Response, int]:
        if not sync_lock.acquire(blocking=False):
            logger.info(
                "sync skipped: another run is in progress",
                extra={
                    "fields": {
                        "event": "sync-run",
                        "outcome": "already-running",
                        "http_status": 200,
                    }
                },
            )
            return jsonify({"outcome": Outcome.ALREADY_RUNNING.value}), 200
        try:
            whole = sync_range(deps.now(), config.lookback)
            report = run_sync(
                config, deps, ranges=split_range(whole), deadline_seconds=config.run_deadline
            )
        except Exception as exc:  # noqa: BLE001 - a bug must answer, not crash the worker
            logger.error("sync failed: %s", type(exc).__name__, extra={"fields": {"event": "bug"}})
            return jsonify({"outcome": "bug"}), 500
        finally:
            sync_lock.release()
        log_report("sync-run", report)
        stranded = report.stranded_preliminary
        notes = (["refresh-token-rotated"] if report.refresh_token_rotated else []) + (
            ["backfill-needed"] if stranded else []
        )
        if report.outcome == Outcome.RECONNECT_NEEDED:
            cache.put(
                ConnectionStatus(
                    RECONNECT_NEEDED, report.reason, deps.now(), tuple(notes), stranded
                )
            )
        elif report.outcome == Outcome.SYNCED and report.token_refreshes:
            # The token refresh just worked: say so at once rather than serving a cached
            # "reconnect-needed" after the user has reconnected.
            cache.put(ConnectionStatus(CONNECTED, None, deps.now(), tuple(notes), stranded))
        return jsonify(report.as_dict()), report.http_status

    @app.get(STATUS_PATH)
    def status() -> tuple[Response, int]:
        result = cache.get()
        if result is None:
            try:
                result = check_connection(
                    config,
                    deps.google_transport,
                    fhir=deps.fhir,
                    range_start=sync_range(deps.now(), config.lookback).start,
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
                        "notes": list(result.notes),
                    }
                },
            )
        return jsonify(result.as_dict()), result.http_status

    return app
