"""The web entry point: the one public service (D10). Every route is in ``routes.CORE_ROUTES``.

Run in production with gunicorn; the exact command is in README.md ("Running"): one worker,
eight threads, ``--timeout 0``, and ``fhir_proxy.app:create_app()``.

Order of checks on every request (``before_request``):

0. The request's time budget starts (``fhir_proxy.deadline``): ``CUFF_FORWARD_TIMEOUT_SECONDS``
   for the cuff send, ``REQUEST_BUDGET_SECONDS`` for everything else. No upstream call starts
   after it is spent.
1. ``OPTIONS`` is a CORS preflight, answered here without a token: ``204`` for a known route
   from an allowed origin with its method and headers, and the same ``403`` for anything else,
   an unknown path included (so a preflight cannot list the routes either).
2. A token in the query string (``access_token=``, RFC 6750 section 2.3) is refused, whatever
   else the request carries: query strings end up in request logs.
3. Every route except ``POST /auth/login`` needs a valid session token in
   ``Authorization: Bearer <token>``; otherwise ``401`` with ``WWW-Authenticate: Bearer``. That
   includes unknown paths, so an unauthenticated caller cannot list the routes.

Every response (``after_request``) gets the security headers and, for an allowed origin, the
CORS headers. Every error is an OperationOutcome with fixed wording; an unexpected exception is
logged by class name only and answered 500.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.utils import format_datetime

from flask import Flask, Response, g, request
from pulsebridge_gcp import HealthcareApiError, HealthcareClient, NotFoundError, default_transport
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from fhir_proxy import capability, deadline, search, traces
from fhir_proxy.bodies import MAX_BODY_BYTES, read_body, too_large
from fhir_proxy.client_address import client_address
from fhir_proxy.config import ServiceConfig
from fhir_proxy.errors import FHIR_JSON, Problem, upstream_problem
from fhir_proxy.extensions import EXTENSION_MODULE, ProxyContext, load_extension, sample_urls
from fhir_proxy.login_limit import FloodLogSampler, LoginLimiter
from fhir_proxy.logs import configure_logging, log
from fhir_proxy.messages import (
    DEFAULT_COUNT,
    KINDS,
    MAX_COUNT,
    OUTCOMES,
    MessagesLog,
    decode_cursor,
)
from fhir_proxy.password import PasswordFile, PasswordUnavailable
from fhir_proxy.routes import CORE_ROUTES, Route
from fhir_proxy.search import parse_instant
from fhir_proxy.session_token import InvalidToken, TokenSigner, passwords_match
from fhir_proxy.status import StatusService
from fhir_proxy.store import SAFE_ID, Stores, entries, next_link
from fhir_proxy.upstream import (
    GoogleIdTokens,
    HttpSender,
    IdTokenSource,
    Upstream,
    UpstreamError,
    UpstreamTimeout,
    requests_sender,
)

SOFTWARE_VERSION = "0.1.0"
JSON_TYPE = "application/json"
MAX_LOGIN_BYTES = 1024
MAX_PASSWORD_CHARS = 1024
# RFC 9110 section 11.1: the auth scheme name is case-insensitive.
_BEARER = re.compile(r"(?i:bearer) ([A-Za-z0-9._-]{1,2048})", re.ASCII)
ALLOWED_REQUEST_HEADERS = ("authorization", "content-type")
EXPOSED_HEADERS = "Retry-After, WWW-Authenticate"
PREFLIGHT_MAX_AGE = "600"
QUERY_TOKEN_NAMES = ("access_token", "token", "id_token")

SECURITY_HEADERS = {
    # Health data and tokens are never cached, by the browser or by Firebase Hosting's CDN.
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    # Do not guess a content type: a JSON body is never run as script or rendered as HTML.
    "X-Content-Type-Options": "nosniff",
    # An API response is never a page: nothing may load in it or frame it.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
}


def _json(body: object, status: int = 200, content_type: str = JSON_TYPE) -> Response:
    return Response(json.dumps(body), status=status, content_type=content_type)


def _problem_response(problem: Problem) -> Response:
    response = Response(problem.body(), status=problem.status, content_type=FHIR_JSON)
    for name, value in problem.headers.items():
        response.headers[name] = value
    return response


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


def _is_json_request() -> bool:
    return (request.mimetype or "") == JSON_TYPE


def create_app(
    config: ServiceConfig | None = None,
    transport: object | None = None,
    *,
    id_tokens: IdTokenSource | None = None,
    send: HttpSender | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    extension_module: str = EXTENSION_MODULE,
) -> Flask:
    """Build the app. With no arguments, configuration comes from the environment, the
    Healthcare client uses Application Default Credentials and identity tokens come from the
    metadata server (the service's own service account on Cloud Run). ``transport`` is the
    Cloud Healthcare transport (tests pass a fake); it is always wrapped so every call honours
    the request's time budget."""
    configure_logging()
    config = config or ServiceConfig.from_env()
    clock = clock or (lambda: datetime.now(UTC))
    monotonic = monotonic or time.monotonic
    client = HealthcareClient(
        project=config.project_id,
        location=config.location,
        dataset=config.dataset_id,
        transport=deadline.DeadlineTransport(transport or default_transport()),
        timeout=config.fhir_timeout,
    )
    executor = ThreadPoolExecutor(max_workers=16)
    started_on = datetime.now(UTC).date().isoformat()
    stores = Stores(
        client,
        fhir_store=config.fhir_store_id,
        audit_store=config.audit_fhir_store_id,
        hl7v2_store=config.hl7v2_store_id,
        patient_id=config.patient_id,
        monotonic=monotonic,
    )
    passwords = PasswordFile(
        config.password_file, cache_seconds=config.password_cache_seconds, monotonic=monotonic
    )
    signer = TokenSigner(
        issuer=config.token_issuer, ttl_seconds=config.session_ttl_seconds, clock=clock
    )
    limiter = LoginLimiter(
        per_address=config.login_failures_per_address,
        total=config.login_failures_total,
        window_seconds=config.login_window_seconds,
        monotonic=monotonic,
    )
    failed_log = FloodLogSampler(window_seconds=config.login_window_seconds, monotonic=monotonic)
    refused_log = FloodLogSampler(window_seconds=config.login_window_seconds, monotonic=monotonic)
    upstream = Upstream(
        tokens=id_tokens or GoogleIdTokens(),
        send=send or requests_sender(),
        cuff_sim_url=config.cuff_sim_url,
        health_sync_url=config.health_sync_url,
        cuff_timeout=config.cuff_forward_timeout,
        samples_timeout=config.samples_forward_timeout,
        health_sync_timeout=config.health_sync_timeout,
    )
    messages_log = MessagesLog(stores, patient_id=config.patient_id, executor=executor, now=clock)

    context = ProxyContext(config=config, stores=stores, core_routes=CORE_ROUTES)
    load_extension(context, extension_module)
    status_service = StatusService(
        stores,
        upstream.health_sync_caller(),
        cache_seconds=config.status_cache_seconds,
        openemr_probe=context.openemr_probe,
        clock=clock,
        monotonic=monotonic,
    )

    prefix = config.path_prefix
    app = Flask("fhir_proxy", static_folder=None)
    # Not Werkzeug's limit: it lets a chunked body through truncated (fhir_proxy.bodies).
    app.config["MAX_CONTENT_LENGTH"] = None
    app.json.sort_keys = False  # type: ignore[attr-defined]
    routes: list[Route] = list(CORE_ROUTES) + [route for route, _ in context.routes]
    app.extensions["pulsebridge_routes"] = routes
    app.extensions["pulsebridge_stores"] = stores
    app.extensions["pulsebridge_messages_log"] = messages_log
    # Fixed by the core table: an extension can never make a route public (or replace login).
    public_endpoints = {r.endpoint for r in CORE_ROUTES if r.public}
    methods_by_rule = {f"{prefix}{r.path}": r.method for r in routes}

    # ---- the gate ----------------------------------------------------------------------------

    def _unauthorized(error: str | None) -> Problem:
        challenge = 'Bearer realm="pulsebridge"'
        if error is not None:
            challenge += f', error="{error}"'
        return Problem(
            401,
            "login",
            "unauthorized",
            "Log in again: the session token is missing, expired or not valid.",
            headers={"WWW-Authenticate": challenge},
        )

    def _current_password() -> str:
        try:
            return passwords.current()
        except PasswordUnavailable as exc:
            log(logging.ERROR, "reviewer-password-unavailable", problem=str(exc))
            raise Problem(
                503,
                "transient",
                "login-not-configured",
                "Reviewer login is not configured on the server.",
            ) from None

    def _preflight() -> Response:
        rule = request.url_rule
        origin = request.headers.get("Origin")
        wanted_method = request.headers.get("Access-Control-Request-Method", "")
        wanted_headers = {
            h.strip().lower()
            for h in request.headers.get("Access-Control-Request-Headers", "").split(",")
            if h.strip()
        }
        allowed_method = None if rule is None else methods_by_rule.get(rule.rule)
        if (
            allowed_method is None
            or origin not in config.allowed_origins
            or wanted_method != allowed_method
            or not wanted_headers <= set(ALLOWED_REQUEST_HEADERS)
        ):
            g.cors_refused = True
            raise Problem(403, "forbidden", "cors-refused", "This cross-origin request is refused.")
        response = Response(status=204)
        response.headers["Access-Control-Allow-Methods"] = allowed_method
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = PREFLIGHT_MAX_AGE
        return response

    @app.before_request
    def gate() -> Response | None:
        g.started = time.monotonic()
        budget = (
            config.cuff_forward_timeout
            if request.endpoint == "cuff_readings"
            else config.request_budget
        )
        deadline.start(budget)
        if request.method == "OPTIONS":
            return _preflight()
        if any(name in request.args for name in QUERY_TOKEN_NAMES):
            raise Problem(
                400,
                "security",
                "token-in-query",
                "Send the session token in the Authorization header, never in the URL.",
                headers={"WWW-Authenticate": 'Bearer realm="pulsebridge", error="invalid_request"'},
            )
        if request.endpoint in public_endpoints:
            return None
        header = request.headers.get("Authorization")
        if header is None:
            raise _unauthorized(None)
        match = _BEARER.fullmatch(header)
        if match is None:
            raise _unauthorized("invalid_request")
        password = _current_password()
        try:
            signer.verify(match.group(1), password)
        except InvalidToken as exc:
            log(logging.INFO, "token-refused", reason=str(exc))
            raise _unauthorized("invalid_token") from None
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        response.headers.pop("Set-Cookie", None)  # D9: no cookie, ever
        response.headers.add("Vary", "Origin")
        origin = request.headers.get("Origin")
        if origin is not None and origin in config.allowed_origins and not g.get("cors_refused"):
            # Exactly the configured origin, never "*"; no Allow-Credentials: a bearer token is
            # sent by script, not by the browser, so credentials mode is not needed.
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Expose-Headers"] = EXPOSED_HEADERS
        return response

    @app.errorhandler(Problem)
    def problem(error: Problem) -> Response:
        return _problem_response(error)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> Response:
        status = error.code or 500
        if isinstance(error, RequestEntityTooLarge):
            # The same shape cuff-sim answers (code too-long, field body), so the dashboard's
            # CuffSendError reads it as "too-large" whichever side refused.
            return _problem_response(too_large(MAX_BODY_BYTES))
        texts = {
            404: ("not-found", "unknown-route", "There is no such route."),
            405: ("not-supported", "method-not-allowed", "This method is not allowed here."),
        }
        code, reason, text = texts.get(
            status, ("invalid", "bad-request", "The request is invalid.")
        )
        return _problem_response(Problem(status, code, reason, text))

    @app.errorhandler(Exception)
    def bug(error: Exception) -> Response:
        # Never the message or traceback: either could quote a value.
        log(logging.ERROR, "bug", error_type=type(error).__name__, endpoint=request.endpoint)
        return _problem_response(
            Problem(500, "exception", "internal-error", "Internal error; see the logs.")
        )

    # ---- handlers ----------------------------------------------------------------------------

    def login() -> Response:
        address = client_address(
            request.headers.get("X-Forwarded-For"), request.remote_addr, config.trusted_proxy_hops
        )

        def sampled(sampler: FloodLogSampler, event: str, **fields: object) -> None:
            # Under a flood: one line per address per window, plus a count of the rest.
            log_it, suppressed = sampler.record(address)
            if suppressed:
                log(
                    logging.WARNING,
                    "login-events-suppressed",
                    kind=event,
                    count=suppressed,
                    window_seconds=config.login_window_seconds,
                )
            if log_it:
                log(logging.WARNING, event, client_address=address, **fields)

        def throttled(wait: int) -> Problem:
            sampled(refused_log, "login-rate-limited", retry_after=wait)
            return Problem(
                429,
                "throttled",
                "rate-limited",
                "Too many failed logins; wait and try again.",
                headers={"Retry-After": str(wait)},
            )

        # The per-address limit comes first: a blocked address learns nothing more. The slot is
        # reserved now, so concurrent attempts from one address count (login_limit.py).
        slot, wait = limiter.begin(address)
        if slot is None:
            raise throttled(wait)
        wrong = False
        try:
            if not _is_json_request():
                raise Problem(
                    415, "not-supported", "unsupported-media-type", "Send application/json."
                )
            data = read_body(MAX_LOGIN_BYTES)
            try:
                body = _strict_json(data)
            except (UnicodeDecodeError, ValueError):
                body = None
            if (
                not isinstance(body, dict)
                or set(body) != {"password"}
                or not isinstance(body["password"], str)
                or not 1 <= len(body["password"]) <= MAX_PASSWORD_CHARS
            ):
                raise Problem(
                    400,
                    "invalid",
                    "invalid-body",
                    'The body must be {"password": "..."}.',
                    expression=["password"],
                )
            actual = _current_password()
            wrong = not passwords_match(body["password"], actual)
        finally:
            if not wrong:
                limiter.succeeded(slot)  # a correct password or no guess at all: slot given back
        if wrong:
            # The instance-wide total limits failures only: it never stops a correct password.
            total_wait = limiter.failed(slot)
            entries_count = len(
                [p for p in (request.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
            )
            if total_wait is None:
                # Below the total, failures are already bounded (60 a minute): log each one,
                # after any summary of a flood whose window has ended.
                for sampler, kind in (
                    (failed_log, "login-failed"),
                    (refused_log, "login-rate-limited"),
                ):
                    suppressed = sampler.due()
                    if suppressed:
                        log(
                            logging.WARNING,
                            "login-events-suppressed",
                            kind=kind,
                            count=suppressed,
                            window_seconds=config.login_window_seconds,
                        )
                log(
                    logging.WARNING,
                    "login-failed",
                    client_address=address,
                    forwarded_for_entries=entries_count,
                )
            else:
                sampled(
                    failed_log,
                    "login-failed",
                    forwarded_for_entries=entries_count,
                    over_total=True,
                )
                raise Problem(
                    429,
                    "throttled",
                    "rate-limited",
                    "Too many failed logins; wait and try again.",
                    headers={"Retry-After": str(total_wait)},
                )
            raise Problem(
                401,
                "login",
                "wrong-password",
                "Wrong password.",
                headers={"WWW-Authenticate": 'Bearer realm="pulsebridge"'},
            )
        issued = signer.issue(actual)
        log(logging.INFO, "login-succeeded")
        expires = issued.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Field names as docs/spikes/s6.md wrote them (the dashboard reads expires_at).
        return _json({"token": issued.token, "expires_at": expires})

    def status() -> Response:
        return _json(status_service.summary())

    def metadata() -> Response:
        statement = capability.capability_statement(
            routes,
            prefix=prefix,
            ttl_hours=config.session_ttl_seconds / 3600,
            software_version=SOFTWARE_VERSION,
            date=started_on,
        )
        return _json(statement, content_type=FHIR_JSON)

    def search_observations() -> Response:
        query = search.parse_observation_query(list(request.args.items(multi=True)))
        try:
            refs = stores.patient_references()
            bundle = stores.search_observations(query.store_params(",".join(refs))) if refs else {}
        except HealthcareApiError as exc:
            raise upstream_problem(exc, "the FHIR store") from None
        found = [
            r
            for r in entries(bundle, "Observation")
            if stores.is_ours(r, refs) and search.matches_dates(r, query)
        ]
        path = f"{prefix}/fhir/Observation"
        links = [
            {"relation": "self", "url": search.own_link(path, query.own_params(query.page_token))}
        ]
        token = search.page_token_from(next_link(bundle))
        if next_link(bundle) is not None and token is None:
            log(logging.WARNING, "paging-link-unrecognised")
        if token is not None:
            links.append(
                {"relation": "next", "url": search.own_link(path, query.own_params(token))}
            )
        result = {
            "resourceType": "Bundle",
            "type": "searchset",
            "link": links,
            # R4: fullUrl SHALL be present for a search result. It names the resource at the
            # proxy (PUBLIC_BASE_URL, from configuration, never from Host), where the read
            # route answers it; the store's own URL would name the project and dataset.
            "entry": [
                {
                    "fullUrl": f"{config.public_base_url}/fhir/Observation/{r['id']}",
                    "resource": r,
                    "search": {"mode": "match"},
                }
                for r in found
                if isinstance(r.get("id"), str)
            ],
        }
        return _json(result, content_type=FHIR_JSON)

    def read_observation(observation_id: str) -> Response:
        if request.args:
            # Like the search: nothing that is not implemented is passed on or ignored
            # (_format, _elements, _summary, anything else).
            raise Problem(
                400,
                "not-supported",
                "unsupported-parameter",
                "The Observation read takes no parameters.",
            )
        observation_id = _checked_id(observation_id)
        try:
            refs = stores.patient_references()
            resource = stores.read("Observation", observation_id)
        except NotFoundError:
            resource = None
        except HealthcareApiError as exc:
            raise upstream_problem(exc, "the FHIR store") from None
        if resource is None or not stores.is_ours(resource, refs):
            # Another patient's Observation answers exactly like a missing one.
            raise Problem(404, "not-found", "not-found", "The Observation was not found.")
        response = _json(resource, content_type=FHIR_JSON)
        # R4 http.html "read": the server SHOULD return an ETag with the versionId (a weak
        # validator) and Last-Modified from meta.lastUpdated.
        meta = resource.get("meta") if isinstance(resource.get("meta"), dict) else {}
        version = meta.get("versionId")
        if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,64}", version):
            response.headers["ETag"] = f'W/"{version}"'
        updated = (
            parse_instant(meta.get("lastUpdated"))
            if isinstance(meta.get("lastUpdated"), str)
            else None
        )
        if updated is not None:
            response.headers["Last-Modified"] = format_datetime(
                updated.astimezone(UTC), usegmt=True
            )
        return response

    def _checked_id(value: str) -> str:
        if not SAFE_ID.fullmatch(value):
            raise Problem(400, "invalid", "invalid-id", "That is not a valid id.")
        return value

    def heart_rate_trace(observation_id: str) -> Response:
        observation_id = _checked_id(observation_id)
        try:
            return _json(traces.heart_rate_trace(stores, observation_id))
        except HealthcareApiError as exc:
            raise upstream_problem(exc, "the FHIR store") from None

    def cuff_attempt_trace(attempt_id: str) -> Response:
        attempt_id = _checked_id(attempt_id)
        try:
            return _json(
                traces.cuff_attempt_trace(
                    stores,
                    messages_log,
                    attempt_id,
                    patient_id=config.patient_id,
                    sample_patient=config.sample_patient,
                )
            )
        except HealthcareApiError as exc:
            raise upstream_problem(exc, "the stores") from None

    def messages() -> Response:
        allowed = {"kind", "outcome", "count", "cursor"}
        args = list(request.args.items(multi=True))
        names = [n for n, _ in args]
        if set(names) - allowed or len(names) != len(set(names)):
            raise Problem(
                400,
                "not-supported",
                "unsupported-parameter",
                "Supported parameters: kind, outcome, count, cursor (each once).",
            )
        given = dict(args)
        kind = given.get("kind")
        outcome = given.get("outcome")
        if kind is not None and kind not in KINDS:
            raise Problem(400, "invalid", "unsupported-value", "Unknown kind.", expression=["kind"])
        if outcome is not None and outcome not in OUTCOMES:
            raise Problem(
                400, "invalid", "unsupported-value", "Unknown outcome.", expression=["outcome"]
            )
        count = DEFAULT_COUNT
        if "count" in given:
            text = given["count"]
            if not re.fullmatch(r"[0-9]{1,3}", text) or int(text) < 1:
                raise Problem(
                    400,
                    "invalid",
                    "unsupported-value",
                    "count must be a whole number from 1.",
                    expression=["count"],
                )
            count = min(int(text), MAX_COUNT)
        cursor = decode_cursor(given["cursor"], clock()) if "cursor" in given else None
        try:
            page = messages_log.page(kind=kind, outcome=outcome, count=count, cursor=cursor)
        except HealthcareApiError as exc:
            raise upstream_problem(exc, "the stores") from None
        return _json(page)

    def _cuff_problem(exc: UpstreamError) -> Problem:
        if isinstance(exc, UpstreamTimeout) or exc.reason == "cut-off":
            return Problem(
                504,
                "timeout",
                "cuff-sim-timeout",
                "The cuff simulator did not answer in time; the reading may have been sent.",
            )
        return Problem(
            502,
            "transient",
            "cuff-sim-unreachable",
            "The cuff simulator could not be reached; nothing was sent.",
        )

    def _forwarded(call: Callable[[], object], what: str) -> Response:
        started = time.monotonic()
        try:
            result = call()
        except UpstreamError as exc:
            log(
                logging.WARNING,
                "cuff-forward-failed",
                route=what,
                reason=exc.reason,
                upstream_status=exc.status,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise _cuff_problem(exc) from None
        log(
            logging.INFO,
            "cuff-forwarded",
            route=what,
            http_status=result.status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        response = Response(result.body, status=result.status, content_type=result.content_type)
        if result.retry_after is not None:
            response.headers["Retry-After"] = result.retry_after
        return response

    def cuff_samples() -> Response:
        return _forwarded(upstream.cuff_samples, "samples")

    def cuff_readings() -> Response:
        body = read_body(MAX_BODY_BYTES)  # read completely, or 413: never forward a part
        content_type = request.headers.get("Content-Type")
        return _forwarded(lambda: upstream.cuff_reading(body, content_type), "readings")

    handlers: dict[str, Callable] = {
        "login": login,
        "status": status,
        "metadata": metadata,
        "search_observations": search_observations,
        "read_observation": read_observation,
        "heart_rate_trace": heart_rate_trace,
        "cuff_attempt_trace": cuff_attempt_trace,
        "messages": messages,
        "cuff_samples": cuff_samples,
        "cuff_readings": cuff_readings,
    }
    for route, handler in context.routes:
        handlers[route.endpoint] = handler
    for route in routes:
        app.add_url_rule(
            f"{prefix}{route.path}",
            endpoint=route.endpoint,
            view_func=handlers[route.endpoint],
            methods=[route.method],
        )
    # Every core URL must still reach its core endpoint: no extension rule may shadow one.
    adapter = app.url_map.bind("")
    for core in CORE_ROUTES:
        for url in sample_urls(core.path):
            endpoint, _ = adapter.match(f"{prefix}{url}", method=core.method)
            if endpoint != core.endpoint:
                raise RuntimeError("an extension route shadows a core route")
    return app
