"""The command: ``python -m pulsebridge_smoke`` (``make smoke`` runs it from the venv).

Configuration comes from flags or environment variables, never from the repository:

- the front door: ``--base-url`` or ``PULSEBRIDGE_BASE_URL`` (``<dashboard_url>/api``);
- the session: the signing key ``<SECRET_NAME_PREFIX>-session-signing-key`` in
  ``CLOUDSDK_CORE_PROJECT``, read with the caller's own ``gcloud``; the token is signed here
  (:mod:`pulsebridge_smoke.session`) for the proxy's ``TOKEN_ISSUER``, which is required:
  ``--token-issuer`` or ``PULSEBRIDGE_TOKEN_ISSUER`` (the stack's ``token_issuer`` output);
- Part B's logs: ``--sync-runs-from gcloud`` (with ``PULSEBRIDGE_HEALTH_SYNC_SERVICE`` and
  ``CLOUDSDK_CORE_PROJECT``; the default when the service variable is set) or
  ``--sync-runs-from <file>``.

**Why the session is signed here (D16).** The proxy has no password and no bot-check bypass: a
visitor's session comes from a browser bot check a command line cannot pass. The proxy signs a
passed visitor's token with the key in Secret Manager; the smoke test, run by an operator who
may read that key, signs its own the same way. The key is read with the caller's own ``gcloud``
credentials and kept in this process's memory only: never on a command line, in the
environment, in shell history or in a file. It and the token are registered with the redactor
before they are used. The session routes are still checked: ``GET /auth/config`` answers, and
``POST /auth/session`` refuses a junk token with ``403 bot-check-failed`` (which costs the stack
one assessment of its daily cap).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from pulsebridge_smoke.cuff_path import Context, Settings, check_status, guard, run_part_a
from pulsebridge_smoke.first_sync import (
    PROJECT_ID,
    FileSyncRuns,
    GcloudSyncRuns,
    SyncRunSource,
    run_part_b,
)
from pulsebridge_smoke.http import (
    ApiClient,
    Transport,
    TransportError,
    UrllibTransport,
    check_base_url,
    origin_of,
    problem_reason,
)
from pulsebridge_smoke.redact import Redactor
from pulsebridge_smoke.report import EXIT_USAGE, Report, Status, Stop
from pulsebridge_smoke.session import (
    SigningKeyError,
    key_text_forms,
    secret_manager_signing_key,
    session_token,
)

PREFIX = re.compile(r"[a-z]([a-z0-9-]{0,10}[a-z0-9])?")  # module identity's name_prefix rule


class UsageError(Exception):
    pass


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pulsebridge_smoke",
        description="End-to-end smoke test of a deployed PulseBridge stack (T29). "
        "See tests/e2e/README.md.",
    )
    p.add_argument("--base-url", help="front door, <dashboard_url>/api (PULSEBRIDGE_BASE_URL)")
    p.add_argument("--project", help="project for gcloud (default CLOUDSDK_CORE_PROJECT)")
    p.add_argument("--secret-prefix", help="secret name prefix (default SECRET_NAME_PREFIX)")
    p.add_argument(
        "--token-issuer",
        help="the proxy's TOKEN_ISSUER, required (PULSEBRIDGE_TOKEN_ISSUER; "
        "terraform output -raw token_issuer)",
    )
    p.add_argument(
        "--allow-real-data-writes",
        action="store_true",
        help="run Part A's writes even when the stack reports real data (see the README)",
    )
    p.add_argument(
        "--sync-runs-from",
        help="Part B's run reports: 'gcloud' or a JSON file "
        "(default gcloud when PULSEBRIDGE_HEALTH_SYNC_SERVICE is set)",
    )
    p.add_argument("--health-sync-service", help="default PULSEBRIDGE_HEALTH_SYNC_SERVICE")
    p.add_argument("--log-freshness", default="12h", help="how far back Part B reads (12h)")
    p.add_argument("--transport", choices=("mllp", "https"), default="mllp")
    p.add_argument("--status-max-seconds", type=float, default=5.0)
    p.add_argument("--poll-timeout", type=float, default=180.0)
    p.add_argument("--poll-interval", type=float, default=5.0)
    p.add_argument("--fail-on-skip", action="store_true", help="exit 3 when a check was skipped")
    p.add_argument("--report-json", type=Path, help="also write the results as JSON (no secrets)")
    p.add_argument("--verbose", action="store_true", help="one line per request: path and status")
    return p


def sync_source(args: argparse.Namespace, env: Mapping[str, str]) -> SyncRunSource | None:
    service = args.health_sync_service or env.get("PULSEBRIDGE_HEALTH_SYNC_SERVICE", "")
    choice = args.sync_runs_from or ("gcloud" if service else "")
    if not choice:
        return None
    if choice == "gcloud":
        project = args.project or env.get("CLOUDSDK_CORE_PROJECT", "")
        if not service or not project:
            raise UsageError(
                "--sync-runs-from gcloud needs PULSEBRIDGE_HEALTH_SYNC_SERVICE and "
                "CLOUDSDK_CORE_PROJECT (or the flags)"
            )
        try:
            return GcloudSyncRuns(project, service, freshness=args.log_freshness)
        except ValueError as exc:
            raise UsageError(str(exc)) from None
    return FileSyncRuns(Path(choice))


def front_door(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    raw = args.base_url or env.get("PULSEBRIDGE_BASE_URL", "")
    if not raw:
        raise UsageError(
            "no front door: set PULSEBRIDGE_BASE_URL (or --base-url) to <dashboard_url>/api"
        )
    try:
        return check_base_url(raw)
    except ValueError as exc:
        raise UsageError(f"PULSEBRIDGE_BASE_URL: {exc}") from None


def signing_settings(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[str, str, str]:
    """The project, the secret prefix and the token issuer, checked."""
    project = args.project or env.get("CLOUDSDK_CORE_PROJECT", "")
    prefix = args.secret_prefix or env.get("SECRET_NAME_PREFIX", "")
    if not project or not prefix:
        raise UsageError(
            "no session: set CLOUDSDK_CORE_PROJECT and SECRET_NAME_PREFIX (or --project and "
            "--secret-prefix) so the signing key can be read from Secret Manager"
        )
    if not PROJECT_ID.fullmatch(project):
        raise UsageError("CLOUDSDK_CORE_PROJECT (or --project) is not a valid project id")
    if not PREFIX.fullmatch(prefix):
        raise UsageError("SECRET_NAME_PREFIX (or --secret-prefix) is not a valid name prefix")
    # Given, never derived: module phr builds it from module identity's name_prefix, which is
    # not SECRET_NAME_PREFIX (pulsebridge_smoke.session, "The issuer").
    issuer = args.token_issuer or env.get("PULSEBRIDGE_TOKEN_ISSUER", "")
    if not issuer:
        raise UsageError(
            "no token issuer: set PULSEBRIDGE_TOKEN_ISSUER (or --token-issuer) to the proxy's "
            "TOKEN_ISSUER, the stack's token_issuer output: "
            'PULSEBRIDGE_TOKEN_ISSUER="$(terraform -chdir=infra/envs/<env> output -raw '
            'token_issuer)"'
        )
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", issuer):  # fhir_proxy.config's TOKEN_ISSUER
        raise UsageError("PULSEBRIDGE_TOKEN_ISSUER (or --token-issuer) is not a valid TOKEN_ISSUER")
    return project, prefix, issuer


# A token-shaped value no browser made: the proxy must refuse it after an assessment.
JUNK_TOKEN_PREFIX = "pulsebridge-smoke-not-a-recaptcha-token-"  # noqa: S105 - not a secret


def check_session_routes(ctx: Context) -> None:
    """S1a to S1c: the session routes and the gate answer as the contract says (D16)."""
    r, api = ctx.report, ctx.api
    config = api.call("GET", "/auth/config", with_session=False)
    body = config.json()
    fields = ("recaptcha_site_key", "recaptcha_action")
    if (
        config.status == 200
        and isinstance(body, dict)
        and all(isinstance(body.get(f), str) and body[f] for f in fields)
    ):
        r.record("S1a", "GET /auth/config gives the bot check's site key and action", Status.PASS)
    else:
        r.record(
            "S1a",
            "GET /auth/config gives the bot check's site key and action",
            Status.FAIL,
            f"HTTP {config.status} (reason {problem_reason(config)}); expected 200 with "
            "recaptcha_site_key and recaptcha_action",
        )
    junk = JUNK_TOKEN_PREFIX + secrets.token_urlsafe(16)
    refused = api.call("POST", "/auth/session", {"recaptcha_token": junk}, with_session=False)
    reason = problem_reason(refused)
    name = "POST /auth/session refuses a token no browser made (403 bot-check-failed)"
    if refused.status == 403 and reason == "bot-check-failed":
        r.record("S1b", name, Status.PASS)
    elif (refused.status, reason) in ((429, "rate-limited"), (503, "demo-busy")):
        r.record("S1b", name, Status.SKIP, f"skipped: the proxy answered {reason} first")
    else:
        r.record("S1b", name, Status.FAIL, f"HTTP {refused.status} (reason {reason}); expected 403")
    gate = api.call("GET", "/fhir/metadata", with_session=False)
    name = "without the cookie, GET /fhir/metadata is 401 session-required"
    if gate.status == 401 and problem_reason(gate) == "session-required":
        r.record("S1c", name, Status.PASS)
    else:
        r.record(
            "S1c",
            name,
            Status.FAIL,
            f"HTTP {gate.status} (reason {problem_reason(gate)}); expected 401",
        )


def run_smoke(ctx: Context, token: str, source: SyncRunSource | None) -> Report:
    """Everything after configuration: the session routes, the session, status, Part B,
    Part A."""
    r = ctx.report
    check_session_routes(ctx)
    ctx.api.use_session(token)
    metadata = ctx.api.call("GET", "/fhir/metadata")
    if metadata.status != 200:
        r.record(
            "S1",
            "the locally signed session is accepted (GET /fhir/metadata)",
            Status.FAIL,
            f"HTTP {metadata.status} (reason {problem_reason(metadata)}); expected 200. Check "
            "that SECRET_NAME_PREFIX and PULSEBRIDGE_TOKEN_ISSUER match the stack",
        )
        r.stop("the smoke test", "S1")
        return r
    r.record("S1", "the locally signed session is accepted (GET /fhir/metadata)", Status.PASS)
    try:
        facts = check_status(ctx)
        may_write = guard(ctx, facts)
    except Stop as stop:
        r.stop("the smoke test", str(stop))
        return r
    r.out("Part B: health-sync first-sync checks")
    run_part_b(
        r,
        real_data=facts.real_data,
        watch_state=facts.watch_state,
        watch_reason=facts.watch_reason,
        source=source,
    )
    r.out("Part A: cuff reading through MLLP, the FHIR store and OpenEMR")
    run_part_a(ctx, facts, may_write=may_write)
    return r


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    key_reader: Callable[[str, str], bytes] = secret_manager_signing_key,
    clock: Callable[[], datetime] | None = None,
    out: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    env = os.environ if env is None else env
    args = parser().parse_args(argv)
    redact = Redactor()
    write = out or (lambda line: print(line, flush=True))

    def emit(line: str) -> None:
        write(redact(line))

    try:
        base_url = front_door(args, env)
        source = sync_source(args, env)
        project, prefix, issuer = signing_settings(args, env)
        key = key_reader(project, prefix)
        for form in key_text_forms(key):
            redact.add(form)
        token = session_token(key, issuer, now=clock() if clock else None)
        redact.add(token)
    except (UsageError, SigningKeyError, ValueError) as exc:
        emit(f"smoke: {exc}")
        return EXIT_USAGE
    settings = Settings(
        status_max_seconds=args.status_max_seconds,
        poll_timeout=args.poll_timeout,
        poll_interval=args.poll_interval,
        allow_real_data_writes=args.allow_real_data_writes,
        transport=args.transport,
    )
    report = Report(out=emit, redact=redact)
    api = ApiClient(
        transport or UrllibTransport(base_url),
        redact,
        log=emit if args.verbose else None,
        origin=origin_of(base_url),
    )
    ctx = Context(api, report, settings)

    def new_session() -> None:
        # Another token with the same claims and a new random jti: the proxy keys its
        # per-session limits on the jti (T46), so this session starts with a full budget.
        fresh = session_token(key, issuer, now=clock() if clock else None)
        redact.add(fresh)
        api.use_session(fresh)

    ctx.new_session = new_session
    if sleep is not None:
        ctx.sleep = sleep
    emit(f"PulseBridge smoke test against {base_url}")
    try:
        run_smoke(ctx, token, source)
    except TransportError as exc:
        report.record("X1", "the front door answers", Status.FAIL, str(exc))
    except Exception as exc:  # noqa: BLE001 - last resort: a FAIL with exit 1, never a traceback
        # Only the exception's type: its text could quote a server answer.
        report.record(
            "X2",
            "the smoke test ran to the end",
            Status.FAIL,
            f"stopped by an unexpected {type(exc).__name__}; a server answer probably had an "
            "unexpected shape (rerun with --verbose to see which request was last)",
        )
    for line in report.summary():
        emit(line)
    if args.report_json:
        args.report_json.write_text(redact(json.dumps(report.as_json(), indent=2)) + "\n")
    return report.exit_code(fail_on_skip=args.fail_on_skip)
