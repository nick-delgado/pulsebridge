"""The command: ``python -m pulsebridge_smoke`` (``make smoke`` runs it from the venv).

Configuration comes from flags or environment variables, never from the repository:

- the front door: ``--base-url`` or ``PULSEBRIDGE_BASE_URL`` (``<dashboard_url>/api``);
- the password: ``--password-source env`` reads ``PULSEBRIDGE_REVIEWER_PASSWORD``;
  ``--password-source secret-manager`` reads ``<SECRET_NAME_PREFIX>-reviewer-password`` in
  ``CLOUDSDK_CORE_PROJECT`` with ``gcloud``; ``auto`` (the default) takes the variable when it is
  set, else Secret Manager;
- Part B's logs: ``--sync-runs-from gcloud`` (with ``PULSEBRIDGE_HEALTH_SYNC_SERVICE`` and
  ``CLOUDSDK_CORE_PROJECT``; the default when the service variable is set) or
  ``--sync-runs-from <file>``.

**Why the password is read from Secret Manager by default.** The proxy's password lives only
in Secret Manager (``<prefix>-reviewer-password``). Reading it here, with the caller's own
``gcloud`` credentials, keeps it in this process's memory only: it is never on a command line,
in the environment of other processes, in shell history, or in a file. An environment variable
is accepted too (CI, or a runner without ``gcloud``), but then it is in the environment of every
child of that shell. Either way it is registered with the redactor before it is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
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
    LoginFailed,
    Transport,
    TransportError,
    UrllibTransport,
    check_base_url,
)
from pulsebridge_smoke.redact import Redactor
from pulsebridge_smoke.report import EXIT_USAGE, Report, Status, Stop

PASSWORD_ENV = "PULSEBRIDGE_REVIEWER_PASSWORD"  # noqa: S105 - a variable name, not a value
PREFIX = re.compile(r"[a-z]([a-z0-9-]{0,10}[a-z0-9])?")  # module identity's name_prefix rule


class UsageError(Exception):
    pass


def secret_manager_password(
    project: str, prefix: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> str:
    """``gcloud secrets versions access latest`` for ``<prefix>-reviewer-password``, with the
    caller's own credentials (``roles/secretmanager.secretAccessor`` on that secret, or Owner).
    The value is returned to the caller only; stdout is never printed."""
    if not PROJECT_ID.fullmatch(project):
        raise UsageError("CLOUDSDK_CORE_PROJECT (or --project) is not a valid project id")
    if not PREFIX.fullmatch(prefix):
        raise UsageError("SECRET_NAME_PREFIX (or --secret-prefix) is not a valid name prefix")
    try:
        done = run(
            [
                "gcloud",
                "secrets",
                "versions",
                "access",
                "latest",
                f"--secret={prefix}-reviewer-password",
                f"--project={project}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageError(f"gcloud did not run ({type(exc).__name__})") from None
    if done.returncode != 0:
        # stderr of a failed access holds no secret (there was no value to print).
        last = (done.stderr or "").strip().splitlines()[-1:] or ["no message"]
        raise UsageError(f"could not read {prefix}-reviewer-password: {last[0]}")
    password = (done.stdout or "").rstrip("\r\n")  # the proxy drops a trailing newline too
    if not password:
        raise UsageError(f"{prefix}-reviewer-password has an empty latest version")
    return password


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pulsebridge_smoke",
        description="End-to-end smoke test of a deployed PulseBridge stack (T29). "
        "See tests/e2e/README.md.",
    )
    p.add_argument("--base-url", help="front door, <dashboard_url>/api (PULSEBRIDGE_BASE_URL)")
    p.add_argument(
        "--password-source",
        choices=("auto", "env", "secret-manager"),
        default="auto",
        help=f"auto: {PASSWORD_ENV} if set, else Secret Manager",
    )
    p.add_argument("--project", help="project for gcloud (default CLOUDSDK_CORE_PROJECT)")
    p.add_argument("--secret-prefix", help="secret name prefix (default SECRET_NAME_PREFIX)")
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


def password_from(
    args: argparse.Namespace,
    env: Mapping[str, str],
    secret_reader: Callable[[str, str], str],
) -> str:
    source = args.password_source
    if source == "auto":
        source = "env" if env.get(PASSWORD_ENV) else "secret-manager"
    if source == "env":
        if not env.get(PASSWORD_ENV):
            raise UsageError(f"{PASSWORD_ENV} is not set")
        return env[PASSWORD_ENV]
    project = args.project or env.get("CLOUDSDK_CORE_PROJECT", "")
    prefix = args.secret_prefix or env.get("SECRET_NAME_PREFIX", "")
    if not project or not prefix:
        raise UsageError(
            f"no password: set {PASSWORD_ENV}, or CLOUDSDK_CORE_PROJECT and SECRET_NAME_PREFIX "
            "to read it from Secret Manager"
        )
    return secret_reader(project, prefix)


def run_smoke(ctx: Context, password: str, source: SyncRunSource | None) -> Report:
    """Everything after configuration: login, status, Part B, Part A."""
    r = ctx.report
    try:
        ctx.api.login(password)
    except LoginFailed as exc:
        r.record("S1", "reviewer login", Status.FAIL, str(exc))
        r.stop("the smoke test", "S1")
        return r
    r.record("S1", "reviewer login", Status.PASS)
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
    secret_reader: Callable[[str, str], str] = secret_manager_password,
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
        base_url = check_base_url(args.base_url or env.get("PULSEBRIDGE_BASE_URL", ""))
        source = sync_source(args, env)
        password = password_from(args, env, secret_reader)
    except (UsageError, ValueError) as exc:
        emit(f"smoke: {exc}")
        return EXIT_USAGE
    redact.add(password)
    settings = Settings(
        status_max_seconds=args.status_max_seconds,
        poll_timeout=args.poll_timeout,
        poll_interval=args.poll_interval,
        allow_real_data_writes=args.allow_real_data_writes,
        transport=args.transport,
    )
    report = Report(out=emit)
    api = ApiClient(
        transport or UrllibTransport(base_url), redact, log=emit if args.verbose else None
    )
    ctx = Context(api, report, settings)
    if sleep is not None:
        ctx.sleep = sleep
    emit(f"PulseBridge smoke test against {base_url}")
    try:
        run_smoke(ctx, password, source)
    except TransportError as exc:
        report.record("X1", "the front door answers", Status.FAIL, str(exc))
    for line in report.summary():
        emit(line)
    if args.report_json:
        args.report_json.write_text(redact(json.dumps(report.as_json(), indent=2)) + "\n")
    return report.exit_code(fail_on_skip=args.fail_on_skip)
