"""The web entry point: the one public service (D10). Every route is in ``routes.CORE_ROUTES``.

Run in production with gunicorn; the exact command is in README.md ("Running"): one worker,
eight threads, ``--timeout 0``, and ``fhir_proxy.app:create_app()``.

Order of checks on every request (``before_request``):

0. The request's time budget starts (``fhir_proxy.deadline``): ``CUFF_FORWARD_TIMEOUT_SECONDS``
   for the cuff send, ``REQUEST_BUDGET_SECONDS`` for everything else. No upstream call starts
   after it is spent.
1. The per-address rate limit (T46): ``REQUESTS_PER_ADDRESS_PER_MINUTE`` requests per client
   address, every route together, the public ``/auth/`` routes and requests refused below
   included; otherwise ``429 rate-limited``. First: it is as cheap as a check gets (one
   dictionary entry, no I/O), and a flood is then counted before anything is done for it,
   even before a refusal below is logged.
2. A token in the query string (``access_token=``, RFC 6750 section 2.3, or ``__session=``) is
   refused, whatever else the request carries: query strings end up in request logs.
3. Every request that can change something (any method but ``GET`` and ``HEAD``; in practice
   every ``POST``) must carry an ``Origin`` header naming one of ``ALLOWED_ORIGINS``;
   otherwise ``403 origin-refused``. With the cookie's ``SameSite=Strict`` this is the defence
   against cross-site request forgery (README, "Demo sessions").
4. Every route except the three under ``/auth/`` needs a valid session token in the
   ``__session`` cookie; otherwise ``401 session-required``. That includes unknown paths, so an
   unauthenticated caller cannot list the routes. ``Authorization: Bearer`` is not accepted.
5. The per-session rate limits (T46), keyed on the verified token's ``jti``: reads (every
   ``GET``/``HEAD``), cuff sends (``POST /cuff/readings``) and shares (``POST /share``, then the
   per-instance limit on shares by everyone); otherwise ``429 rate-limited``. After the session
   check, because only a verified token's ``jti`` is a key a caller cannot make up. Then the
   route itself; ``POST /auth/session`` adds its own per-address limit on session attempts.

There is no CORS: the page and the proxy share one origin (Firebase Hosting's ``/api``
rewrite), so no ``Access-Control-*`` header is ever sent and a cross-origin script cannot read
an answer. Every response (``after_request``) gets the security headers, and loses any
``Set-Cookie`` except the one ``POST /auth/session`` or ``/auth/logout`` sets. Every error is an
OperationOutcome with fixed wording; an unexpected exception is logged by class name only and
answered 500.
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
from urllib.parse import urlsplit

from flask import Flask, Response, g, request
from pulsebridge_gcp import HealthcareApiError, HealthcareClient, NotFoundError, default_transport
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from fhir_proxy import capability, deadline, search, traces
from fhir_proxy.bodies import MAX_BODY_BYTES, read_body, too_large
from fhir_proxy.bot_check import BotCheckUnavailable, FraudDefense, token_has_shape
from fhir_proxy.client_address import client_address
from fhir_proxy.config import ServiceConfig
from fhir_proxy.demo_counter import CounterUnavailable, DailyCounter, DemoBusy
from fhir_proxy.errors import FHIR_JSON, Problem, upstream_problem
from fhir_proxy.extensions import EXTENSION_MODULE, ProxyContext, load_extension, sample_urls
from fhir_proxy.flood_log import FloodLogSampler
from fhir_proxy.logs import configure_logging, log
from fhir_proxy.messages import (
    DEFAULT_COUNT,
    KINDS,
    MAX_COUNT,
    OUTCOMES,
    MessagesLog,
    decode_cursor,
)
from fhir_proxy.rate_limit import SlidingWindows, TokenBuckets
from fhir_proxy.routes import CORE_ROUTES, Route
from fhir_proxy.search import parse_instant
from fhir_proxy.session_limit import SessionLimiter
from fhir_proxy.session_token import InvalidToken, TokenSigner
from fhir_proxy.signing_key import KeyUnavailable, SigningKeyFile
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
# {"recaptcha_token": "<token>"}: a token is at most 8 KiB (bot_check.TOKEN_SHAPE).
MAX_SESSION_BODY_BYTES = 10 * 1024
QUERY_TOKEN_NAMES = ("access_token", "token", "id_token", "__session")
# The one cookie: Firebase Hosting strips the cookies of a request it passes to Cloud Run, except
# one named __session, which it also adds to the CDN cache key ("Manage cache behavior",
# firebase.google.com/docs/hosting/manage-cache, "Using cookies").
COOKIE_NAME = "__session"
# RFC 9110 section 15.5.2: a 401 MUST carry WWW-Authenticate. No registered scheme describes a
# cookie session, so this names the cookie in the form of the (expired) HTTP Cookie
# authentication draft (draft-broyer-http-cookie-auth); a client needs nothing from it.
SESSION_CHALLENGE = f'Cookie realm="pulsebridge", cookie-name="{COOKIE_NAME}"'
# Upper bound of one Fraud Defense or Cloud Storage call (each is also capped by the request's
# budget). Fixed, not a variable: neither is user-visible tuning.
GOOGLE_CALL_TIMEOUT_SECONDS = 10.0
SESSION_LOG_WINDOW_SECONDS = 60
DEMO_BUSY_TEXT = "The demo is busy, try again tomorrow."
# The share route's endpoint (fhir_proxy.share.ROUTE; a test checks the two agree). Named here,
# not imported, because the share package is an optional extension.
SHARE_ENDPOINT = "share"
# T46: the windows of the rate limits; the counts are variables (config.py).
MINUTE_SECONDS = 60
TEN_MINUTES_SECONDS = 600
HOUR_SECONDS = 3600
RATE_LOG_WINDOW_SECONDS = 60
# Every 429 the gate answers; the page shows the wait from Retry-After for any of them (T45).
RATE_LIMIT_TEXTS = {
    "address": "Too many requests from this address; wait and try again.",
    "session-reads": "Too many requests in this demo session; wait and try again.",
    "session-cuff-sends": (
        "This demo session has sent as many cuff readings as it may for now; wait and try again."
    ),
    "session-shares": (
        "This demo session has shared as many readings as it may for now; wait and try again."
    ),
    "all-shares": ("The demo has shared as many readings as it may this hour; wait and try again."),
}

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


def session_cookie(token: str, *, path: str, max_age: int) -> str:
    """The ``Set-Cookie`` value of the demo session (the contract in docs/workplan.md, D16).

    - ``Path``: ``PATH_PREFIX`` (``/api``), so the browser sends the cookie to the proxy only,
      never with the page's own files.
    - ``Max-Age``: the token's lifetime; 0 deletes the cookie (RFC 6265 section 5.2.2).
    - ``HttpOnly``: page script cannot read it, so an injected script cannot steal it.
    - ``Secure``: sent over HTTPS only.
    - ``SameSite=Strict``: the browser sends it only on requests the page's own site makes, never
      on a request another site starts (a link, a form, an image). That is half of the defence
      against cross-site request forgery; the ``Origin`` check is the other half.
    - No ``Domain``: a host-only cookie, never shared with other subdomains.
    """
    return (
        f"{COOKIE_NAME}={token}; Path={path}; Max-Age={max_age}; HttpOnly; Secure; SameSite=Strict"
    )


def create_app(
    config: ServiceConfig | None = None,
    transport: object | None = None,
    *,
    id_tokens: IdTokenSource | None = None,
    send: HttpSender | None = None,
    recaptcha_transport: object | None = None,
    storage_transport: object | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    extension_module: str = EXTENSION_MODULE,
    extension_options: dict[str, object] | None = None,
) -> Flask:
    """Build the app. With no arguments, configuration comes from the environment, the
    Healthcare client uses Application Default Credentials and identity tokens come from the
    metadata server (the service's own service account on Cloud Run). ``transport`` is the
    Cloud Healthcare transport (tests pass a fake); ``recaptcha_transport`` and
    ``storage_transport`` carry the Fraud Defense and Cloud Storage calls (tests pass
    ``fhir_proxy.testing``'s fakes). Each is always wrapped so every call honours the request's
    time budget. ``extension_options`` reaches the extension's ``register`` as
    ``proxy.options`` (tests pass the share's fake OpenEMR transport there)."""
    configure_logging()
    config = config or ServiceConfig.from_env()
    clock = clock or (lambda: datetime.now(UTC))
    monotonic = monotonic or time.monotonic
    shared: list[object] = []

    def google() -> object:
        # One authorised session (Application Default Credentials, cloud-platform scope) for
        # every Google API the proxy calls; created only if some transport was not passed in.
        if not shared:
            shared.append(default_transport())
        return shared[0]

    client = HealthcareClient(
        project=config.project_id,
        location=config.location,
        dataset=config.dataset_id,
        transport=deadline.DeadlineTransport(transport or google()),
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
    signing_key = SigningKeyFile(config.session_key_file, monotonic=monotonic)
    signer = TokenSigner(
        issuer=config.token_issuer, ttl_seconds=config.session_ttl_seconds, clock=clock
    )
    session_limiter = SessionLimiter(
        per_address=config.sessions_per_address_per_hour, monotonic=monotonic
    )
    # The bot check's hostname must be the host of one of the page's origins.
    page_hosts = [urlsplit(origin).hostname or "" for origin in config.allowed_origins]
    fraud_defense = FraudDefense(
        deadline.DeadlineTransport(recaptcha_transport or google()),
        project=config.project_id,
        site_key=config.recaptcha_site_key,
        action=config.recaptcha_action,
        min_score=config.recaptcha_min_score,
        hostnames=page_hosts,
        timeout=GOOGLE_CALL_TIMEOUT_SECONDS,
    )
    counter = DailyCounter(
        deadline.DeadlineTransport(storage_transport or google()),
        bucket=config.demo_counter_bucket,
        cap=config.bot_checks_per_day,
        timeout=GOOGLE_CALL_TIMEOUT_SECONDS,
        clock=clock,
    )
    # T46's rate limits (README, "Rate limits"): in memory, per instance.
    per_address = TokenBuckets(
        capacity=config.requests_per_address_per_minute,
        window_seconds=MINUTE_SECONDS,
        monotonic=monotonic,
    )
    session_reads = TokenBuckets(
        capacity=config.reads_per_session_per_minute,
        window_seconds=MINUTE_SECONDS,
        monotonic=monotonic,
    )
    session_cuff_sends = SlidingWindows(
        limit=config.cuff_sends_per_session_per_10_minutes,
        window_seconds=TEN_MINUTES_SECONDS,
        monotonic=monotonic,
    )
    session_shares = SlidingWindows(
        limit=config.shares_per_session_per_10_minutes,
        window_seconds=TEN_MINUTES_SECONDS,
        monotonic=monotonic,
    )
    all_shares = SlidingWindows(
        limit=config.shares_per_instance_per_hour,
        window_seconds=HOUR_SECONDS,
        max_keys=1,
        monotonic=monotonic,
    )
    rate_log = FloodLogSampler(window_seconds=RATE_LOG_WINDOW_SECONDS, monotonic=monotonic)
    refused_log = FloodLogSampler(window_seconds=SESSION_LOG_WINDOW_SECONDS, monotonic=monotonic)
    origin_log = FloodLogSampler(window_seconds=SESSION_LOG_WINDOW_SECONDS, monotonic=monotonic)
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

    context = ProxyContext(
        config=config,
        stores=stores,
        core_routes=CORE_ROUTES,
        clock=clock,
        monotonic=monotonic,
        options=dict(extension_options or {}),
    )
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
    app.extensions["pulsebridge_status"] = status_service
    # Fixed by the core table: an extension can never make a route public.
    public_endpoints = {r.endpoint for r in CORE_ROUTES if r.public}
    cookie_path = prefix or "/"

    # ---- the gate ----------------------------------------------------------------------------

    def _session_required() -> Problem:
        # R4 issue-type "login": "the user needs to log in". One reason for every case
        # (missing, expired, signed with another key, malformed): the page only starts again.
        return Problem(
            401,
            "login",
            "session-required",
            "Start a demo session: the session is missing, expired or not valid.",
            headers={"WWW-Authenticate": SESSION_CHALLENGE},
        )

    def _current_key() -> bytes:
        try:
            return signing_key.current()
        except KeyUnavailable as exc:
            log(logging.ERROR, "session-signing-key-unavailable", problem=str(exc))
            raise Problem(
                503,
                "transient",
                "session-not-configured",
                "Demo sessions are not configured on the server.",
            ) from None

    def _address() -> str:
        return client_address(
            request.headers.get("X-Forwarded-For"), request.remote_addr, config.trusted_proxy_hops
        )

    def _sampled(sampler: FloodLogSampler, event: str, address: str, **fields: object) -> None:
        # Under a flood: one line per address per window, plus a count of the rest.
        log_it, suppressed = sampler.record(address)
        if suppressed:
            log(
                logging.WARNING,
                "session-events-suppressed",
                kind=event,
                count=suppressed,
                window_seconds=SESSION_LOG_WINDOW_SECONDS,
            )
        if log_it:
            log(logging.WARNING, event, client_address=address, **fields)

    def _rate_limited(limit: str, wait: int, address: str) -> Problem:
        # Sampled: under a flood, one line per address per minute plus a count of the rest.
        _sampled(rate_log, "rate-limited", address, limit=limit, retry_after=wait)
        return Problem(
            429,
            "throttled",  # R4 issue-type: "the system is not prepared to handle this request"
            "rate-limited",
            RATE_LIMIT_TEXTS[limit],
            headers={"Retry-After": str(wait)},
        )

    def _session_limits(jti: str, address: str) -> None:
        """The per-session limits of this request, by what it does (T46)."""
        if request.method in ("GET", "HEAD"):
            wait = session_reads.take(jti)
            if wait is not None:
                raise _rate_limited("session-reads", wait, address)
        elif request.endpoint == "cuff_readings":
            wait = session_cuff_sends.take(jti)
            if wait is not None:
                raise _rate_limited("session-cuff-sends", wait, address)
        elif request.endpoint == SHARE_ENDPOINT:
            wait = session_shares.take(jti)
            if wait is not None:
                raise _rate_limited("session-shares", wait, address)
            wait = all_shares.take("all")
            if wait is not None:
                # Refused by the limit on everyone: this session's share did not happen, so it
                # does not count against the session either.
                session_shares.give_back(jti)
                raise _rate_limited("all-shares", wait, address)

    @app.before_request
    def gate() -> Response | None:
        g.started = time.monotonic()
        budget = (
            config.cuff_forward_timeout
            if request.endpoint == "cuff_readings"
            else config.request_budget
        )
        deadline.start(budget)
        address = _address()
        wait = per_address.take(address)
        if wait is not None:
            raise _rate_limited("address", wait, address)
        if any(name in request.args for name in QUERY_TOKEN_NAMES):
            raise Problem(
                400,
                "security",
                "token-in-query",
                "Never send a session token in the URL.",
            )
        if request.method not in ("GET", "HEAD"):
            # The Origin check (CSRF). An exact match with a configured origin, the form a
            # browser sends (RFC 6454 section 7); a missing header is refused too: every
            # browser sends Origin on a POST (Fetch standard, "append a request Origin header").
            origin = request.headers.get("Origin")
            if origin not in config.allowed_origins:
                _sampled(origin_log, "origin-refused", address, origin_present=origin is not None)
                raise Problem(
                    403,
                    "forbidden",
                    "origin-refused",
                    "This request must come from the dashboard's own page.",
                )
        if request.endpoint in public_endpoints:
            return None
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise _session_required()
        key = _current_key()
        try:
            g.session = signer.verify(token, key)
        except InvalidToken as exc:
            log(logging.INFO, "token-refused", reason=str(exc))
            raise _session_required() from None
        _session_limits(g.session["jti"], address)
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        # The only cookie is the session's, set by the two handlers below; any other (from an
        # extension or a forwarded answer) is dropped.
        response.headers.remove("Set-Cookie")
        cookie = g.get("set_cookie")
        if cookie is not None:
            response.headers.add("Set-Cookie", cookie)
        response.headers.add("Vary", "Origin, Cookie")
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

    def auth_config() -> Response:
        # Public by design: the site key is what the page hands to Google's script. Fetched by
        # the page, not built into it, so a clone sets its own key in Terraform only.
        return _json(
            {
                "recaptcha_site_key": config.recaptcha_site_key,
                "recaptcha_action": config.recaptcha_action,
            }
        )

    def _bot_check_unavailable() -> Problem:
        return Problem(
            503,
            "transient",
            "bot-check-unavailable",
            "The bot check is unavailable; try again shortly.",
        )

    def auth_session() -> Response:
        """The order is the cost order: everything that is free comes before the counter, and
        the counter (which caps what is spent) before the assessment (what costs money)."""
        address = _address()
        # 1. The per-address limit, before the body is even read.
        wait = session_limiter.attempt(address)
        if wait is not None:
            _sampled(refused_log, "session-rate-limited", address, retry_after=wait)
            raise Problem(
                429,
                "throttled",
                "rate-limited",
                "Too many attempts; wait and try again.",
                headers={"Retry-After": str(wait)},
            )
        # 2. The body: exactly {"recaptcha_token": "<text>"}, strict JSON.
        invalid = Problem(
            400,
            "invalid",
            "invalid-body",
            'The body must be {"recaptcha_token": "..."}.',
            expression=["recaptcha_token"],
        )
        if not _is_json_request():
            raise invalid
        try:
            body = _strict_json(read_body(MAX_SESSION_BODY_BYTES))
        except (UnicodeDecodeError, ValueError):
            raise invalid from None
        if not isinstance(body, dict) or set(body) != {"recaptcha_token"}:
            raise invalid
        token = body["recaptcha_token"]
        if not isinstance(token, str):
            raise invalid
        failed = Problem(403, "forbidden", "bot-check-failed", "The bot check did not pass.")
        # 3. The cheap shape check: junk costs no assessment and no count.
        if not token_has_shape(token):
            log(logging.WARNING, "bot-check-failed", reason="malformed-token")
            raise failed
        # 4. The signing key: without it no session could be issued, so spend nothing.
        key = _current_key()
        # 5. The daily cap, counted before the assessment is made.
        try:
            taken = counter.take()
        except DemoBusy as busy:
            log(logging.WARNING, "demo-busy", cap=config.bot_checks_per_day)
            raise Problem(
                503,
                "throttled",
                "demo-busy",
                DEMO_BUSY_TEXT,
                headers={"Retry-After": str(busy.retry_after)},
            ) from None
        except CounterUnavailable as exc:
            log(logging.ERROR, "demo-counter-unavailable", problem=str(exc))
            raise _bot_check_unavailable() from None
        # 6. The assessment.
        try:
            verdict = fraud_defense.assess(token)
        except BotCheckUnavailable as exc:
            log(logging.ERROR, "bot-check-unavailable", problem=str(exc))
            raise _bot_check_unavailable() from None
        if not verdict.passed:
            # Logged for tuning RECAPTCHA_MIN_SCORE; the caller learns only bot-check-failed.
            log(
                logging.WARNING,
                "bot-check-failed",
                reason=verdict.reason,
                invalid_reason=verdict.invalid_reason,
                score=verdict.score,
                checks_today=taken.count,
            )
            raise failed
        issued = signer.issue(key)
        g.set_cookie = session_cookie(
            issued.token, path=cookie_path, max_age=config.session_ttl_seconds
        )
        log(logging.INFO, "session-created", score=verdict.score, checks_today=taken.count)
        expires = issued.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return _json({"expires_at": expires})

    def auth_logout() -> Response:
        # No session needed: an expired cookie can be cleared too. The browser deletes a cookie
        # whose Max-Age is 0 when the name, path and domain match the one it holds.
        g.set_cookie = session_cookie("", path=cookie_path, max_age=0)
        return Response(status=204)

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
        "auth_config": auth_config,
        "auth_session": auth_session,
        "auth_logout": auth_logout,
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
            # No automatic OPTIONS answer: there is no CORS, and OPTIONS goes through the gate.
            provide_automatic_options=False,
        )
    # Every core URL must still reach its core endpoint: no extension rule may shadow one.
    adapter = app.url_map.bind("")
    for core in CORE_ROUTES:
        for url in sample_urls(core.path):
            endpoint, _ = adapter.match(f"{prefix}{url}", method=core.method)
            if endpoint != core.endpoint:
                raise RuntimeError("an extension route shadows a core route")
    return app
