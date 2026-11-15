"""``connect-google-health``: authorise the deployer's Google Health account (task T11, D2).

The flow, in the order :func:`run` runs it:

1. Check the configuration (project, secret names, FHIR store, Patient demographics) before
   touching anything, so a typo cannot leave a half-done connect.
2. Read the OAuth client ID and secret from Secret Manager. If either has no usable version (or
   ``--reset-client``), prompt for it without echo and store it as a new version.
3. Make a PKCE verifier and challenge and a random ``state``; build Google's authorization
   address with ``access_type=offline``, ``prompt=consent`` and the scope.
4. Receive the redirect: a one-shot listener on ``127.0.0.1`` (the default), or, with
   ``--paste`` or when the listener cannot start, the redirect address pasted without echo.
5. Exchange the code (with the verifier); require a refresh token and the scope.
6. Store the refresh token as a new version; print only the secret's name and version number.
   Optionally (``--disable-previous``) disable older versions. Nothing is destroyed.
7. Say when the refresh token expires, and how to stop that.
8. Unless ``--skip-patient``: the identity call, then T06's setup Bundle (the full Patient,
   conditional update) through ``pulsebridge_gcp``. A failure here does not undo step 6.

Exit codes: 0 connected (and Patient written unless skipped); 1 not connected (no refresh token
stored); 2 bad arguments or configuration; 3 connected but the Patient write failed (re-run, or
run again later: every step is safe to repeat); 130 interrupted.

Never printed, logged or written to disk: the client secret, the client ID (except inside the
authorization address in ``--paste`` mode, where the user must open it by hand), the
authorization code, the access token, the refresh token, the Google user id and the
demographics.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import sys
import traceback
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TextIO

from pulsebridge_fhir.inputs import GENDERS
from pulsebridge_gcp import HealthcareClient
from pulsebridge_gcp.transport import Transport

from connect_google_health import oauth, pkce
from connect_google_health.loopback import LoopbackListener
from connect_google_health.patient import (
    Demographics,
    SetupError,
    check_demographics,
    fetch_health_user_id,
    setup_bundle,
    write_patient,
)
from connect_google_health.secret_store import (
    DEFAULT_PREFIX,
    SecretStore,
    SecretStoreError,
    check_prefix,
    secret_id,
)

EXIT_OK = 0
EXIT_NOT_CONNECTED = 1
EXIT_USAGE = 2
EXIT_PATIENT_FAILED = 3
EXIT_INTERRUPTED = 130

DEFAULT_TIMEOUT_SECONDS = 300
TESTING_REFRESH_TOKEN_DAYS = 7  # Google Identity, "Refresh token expiration"

# GCP project IDs: 6-30 characters, lower-case letters, digits and hyphens, starting with a
# letter and not ending with a hyphen.
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", re.ASCII)

PUBLISH_HELP = (
    "To stop the weekly reconnect, publish the OAuth app: Google Cloud console > Google Auth "
    'Platform > Audience > "Publish app" (publishing status "In production"). Whether Google '
    "then requires app verification for health scopes was not tested (spike S3)."
)


class UsageError(Exception):
    """Bad arguments or configuration. ``str()`` names the flag or variable, never a value."""


# ---- configuration ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FhirTarget:
    location: str
    dataset: str
    fhir_store: str


@dataclass(frozen=True)
class Config:
    project: str
    prefix: str
    scope: str
    port: int
    paste: bool
    open_browser: bool
    timeout_seconds: int
    reset_client: bool
    disable_previous: bool
    fhir: FhirTarget | None  # None with --skip-patient
    demographics: Demographics | None = field(default=None, repr=False)

    def secret(self, key: str) -> str:
        return secret_id(self.prefix, key)


# flag -> (environment variable, help). The environment names match health-sync's where the
# value is the same (GCP_PROJECT_ID, HEALTHCARE_*, FHIR_STORE_ID, PATIENT_ID, GOOGLE_HEALTH_SCOPE).
ENVIRONMENT = {
    "project": ("GCP_PROJECT_ID", "GCP project that holds the secrets and the FHIR store"),
    "name_prefix": (
        "PULSEBRIDGE_NAME_PREFIX",
        f"identity module name_prefix (default {DEFAULT_PREFIX!r})",
    ),
    "scope": ("GOOGLE_HEALTH_SCOPE", "OAuth scope to request"),
    "location": ("HEALTHCARE_LOCATION", "Healthcare dataset region"),
    "dataset": ("HEALTHCARE_DATASET_ID", "Healthcare dataset id"),
    "fhir_store": ("FHIR_STORE_ID", "the main FHIR store id (not the audit store)"),
    "patient_id": ("PATIENT_ID", "the configured patient id (the same one health-sync uses)"),
    "family_name": ("PATIENT_FAMILY_NAME", "Patient family name"),
    "given_name": ("PATIENT_GIVEN_NAME", "Patient given name"),
    "gender": ("PATIENT_GENDER", f"Patient gender: {', '.join(GENDERS)}"),
    "birth_date": ("PATIENT_BIRTH_DATE", "Patient birth date, YYYY-MM-DD"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connect-google-health",
        description=(
            "Connect your Google Health account to PulseBridge: stores the OAuth client and a "
            "refresh token in Secret Manager and writes your Patient record once. Each value "
            "can also come from the environment variable named in brackets."
        ),
    )
    for name, (env, text) in ENVIRONMENT.items():
        parser.add_argument("--" + name.replace("_", "-"), dest=name, help=f"{text} [{env}]")
    parser.add_argument(
        "--port", type=int, default=0, help="loopback port for the redirect (default: random)"
    )
    parser.add_argument(
        "--paste",
        action="store_true",
        help="do not listen; paste the redirect address instead (browser on another machine)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser; print the local link to open by hand",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait for the redirect (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--reset-client",
        action="store_true",
        help="prompt for the OAuth client ID and secret even if they are stored",
    )
    parser.add_argument(
        "--disable-previous",
        action="store_true",
        help="disable (never destroy) older refresh-token versions after storing the new one",
    )
    parser.add_argument(
        "--skip-patient", action="store_true", help="do not write the Patient record"
    )
    return parser


def _value(args: argparse.Namespace, env: Mapping[str, str], name: str) -> str | None:
    flag_value = getattr(args, name)
    if flag_value not in (None, ""):
        return flag_value
    value = env.get(ENVIRONMENT[name][0], "")
    return value or None


def _required(args: argparse.Namespace, env: Mapping[str, str], name: str) -> str:
    value = _value(args, env, name)
    if value is None:
        raise UsageError(f"--{name.replace('_', '-')} or {ENVIRONMENT[name][0]} is required")
    return value


def load_config(argv: list[str], env: Mapping[str, str]) -> Config:
    args = build_parser().parse_args(argv)
    project = _required(args, env, "project")
    if not _PROJECT_ID.fullmatch(project):
        raise UsageError("--project / GCP_PROJECT_ID is not a valid GCP project ID")
    try:
        prefix = check_prefix(_value(args, env, "name_prefix") or DEFAULT_PREFIX)
    except ValueError as exc:
        raise UsageError(f"--name-prefix / PULSEBRIDGE_NAME_PREFIX: {exc}") from None
    scope = _value(args, env, "scope") or oauth.DEFAULT_SCOPE
    if not re.fullmatch(r"[!-~]+( [!-~]+)*", scope, re.ASCII):
        raise UsageError("--scope / GOOGLE_HEALTH_SCOPE must be space-separated scope strings")
    if not 0 <= args.port <= 65535:
        raise UsageError("--port must be 0 (random) or 1 to 65535")
    if not 10 <= args.timeout <= 3600:
        raise UsageError("--timeout must be 10 to 3600 seconds")

    fhir = demographics = None
    if not args.skip_patient:
        fhir = FhirTarget(
            location=_required(args, env, "location"),
            dataset=_required(args, env, "dataset"),
            fhir_store=_required(args, env, "fhir_store"),
        )
        try:  # the same path checks the client applies, before anything happens
            HealthcareClient(
                project=project,
                location=fhir.location,
                dataset=fhir.dataset,
                transport=None,  # type: ignore[arg-type] - never used here
            ).fhir_store_name(fhir.fhir_store)
        except ValueError:
            raise UsageError("the FHIR location, dataset or store ID is not valid") from None
        birth_text = _required(args, env, "birth_date")
        try:
            birth = date.fromisoformat(birth_text)
        except ValueError:
            raise UsageError("--birth-date / PATIENT_BIRTH_DATE must be YYYY-MM-DD") from None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth_text) or birth > date.today():
            raise UsageError("--birth-date / PATIENT_BIRTH_DATE must be a past YYYY-MM-DD date")
        gender = _required(args, env, "gender")
        if gender not in GENDERS:
            raise UsageError(f"--gender / PATIENT_GENDER must be one of {', '.join(GENDERS)}")
        demographics = Demographics(
            patient_id=_required(args, env, "patient_id"),
            family=_required(args, env, "family_name"),
            given=_required(args, env, "given_name"),
            gender=gender,
            birth_date=birth,
        )
        try:
            check_demographics(demographics)
        except SetupError as exc:
            raise UsageError(str(exc)) from None

    return Config(
        project=project,
        prefix=prefix,
        scope=scope,
        port=args.port,
        paste=args.paste,
        open_browser=not args.no_browser,
        timeout_seconds=args.timeout,
        reset_client=args.reset_client,
        disable_previous=args.disable_previous,
        fhir=fhir,
        demographics=demographics,
    )


# ---- dependencies (real ones in main(), fakes in tests) ---------------------------------------


@dataclass
class Dependencies:
    secret_store: Callable[[str], SecretStore]
    google_transport: Callable[[], Transport]  # plain HTTPS: never the GCP credentials
    healthcare_client: Callable[[str, FhirTarget], HealthcareClient]
    prompt_hidden: Callable[[str], str] = getpass.getpass
    open_browser: Callable[[str], bool] = webbrowser.open
    listener: Callable[..., LoopbackListener] = LoopbackListener
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    random_port: Callable[[], int] = lambda: 49152 + secrets.randbelow(65535 - 49152 + 1)
    token_url: str = oauth.TOKEN_URL
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)


def real_dependencies() -> Dependencies:
    def secret_store(project: str) -> SecretStore:
        from connect_google_health.secret_store import SecretManagerStore

        return SecretManagerStore.from_environment(project)

    def google_transport() -> Transport:
        import requests
        from pulsebridge_gcp import RequestsTransport

        return RequestsTransport(requests.Session())

    def healthcare_client(project: str, target: FhirTarget) -> HealthcareClient:
        from pulsebridge_gcp import default_transport

        return HealthcareClient(
            project=project,
            location=target.location,
            dataset=target.dataset,
            transport=default_transport(),
        )

    return Dependencies(
        secret_store=secret_store,
        google_transport=google_transport,
        healthcare_client=healthcare_client,
    )


# ---- the flow --------------------------------------------------------------------------------


class Abort(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _say(deps: Dependencies, text: str = "") -> None:
    print(text, file=deps.out, flush=True)


def _prompt_credential(deps: Dependencies, label: str) -> str:
    for _attempt in range(3):
        value = deps.prompt_hidden(f"{label} (input hidden): ").strip()
        if oauth.is_credential_shaped(value):
            return value
        _say(deps, f"  That is not a valid {label}: one value, no spaces. Try again.")
    raise Abort(f"No valid {label} entered.", EXIT_NOT_CONNECTED)


def client_credentials(
    config: Config, store: SecretStore, deps: Dependencies
) -> oauth.ClientCredentials:
    values: dict[str, str] = {}
    for key, label in (("client-id", "OAuth client ID"), ("client-secret", "OAuth client secret")):
        name = config.secret(key)
        stored = None if config.reset_client else store.access_latest(name)
        if stored is not None and oauth.is_credential_shaped(stored.strip()):
            values[key] = stored.strip()
            _say(deps, f"  {label}: using the latest version of secret {name}.")
            continue
        if stored is not None:
            _say(deps, f"  {label}: the stored value in {name} is not usable; enter it again.")
        else:
            reason = "--reset-client" if config.reset_client else "no usable version stored"
            _say(deps, f"  {label}: {reason}. Copy it from Google Auth Platform > Clients.")
        value = _prompt_credential(deps, label)
        version = store.add_version(name, value)
        _say(deps, f"  {label}: stored as version {version} of secret {name}.")
        values[key] = value
    return oauth.ClientCredentials(values["client-id"], values["client-secret"])


def receive_code(
    config: Config, auth_url_for: Callable[[str], str], state: str, deps: Dependencies
) -> tuple[oauth.AuthorizationCode, str]:
    """Returns the code and the redirect address it was requested with."""
    if not config.paste:
        try:
            listener = deps.listener(
                expected_state=state,
                port=config.port,
            )
        except OSError:
            _say(deps, "  Could not listen on 127.0.0.1; switching to paste mode.")
        else:
            with listener:
                redirect_uri = oauth.loopback_redirect_uri(listener.port)
                listener.authorization_url = auth_url_for(redirect_uri)
                opened = False
                if config.open_browser:
                    try:
                        opened = bool(deps.open_browser(listener.start_url))
                    except Exception:  # noqa: BLE001 - any browser failure means "open by hand"
                        opened = False
                if opened:
                    _say(
                        deps, "  A browser was opened at this local link (open it by hand if not):"
                    )
                else:
                    _say(deps, "  Open this link in a browser on THIS computer:")
                _say(deps, f"    {listener.start_url}")
                _say(deps, f"  Waiting up to {config.timeout_seconds} s for Google's redirect...")
                return listener.wait(config.timeout_seconds), redirect_uri

    port = config.port or deps.random_port()
    redirect_uri = oauth.loopback_redirect_uri(port)
    _say(deps, "  Open this address in any browser. It contains your OAuth client ID, which")
    _say(deps, "  Google treats as public (it appears in the browser's address bar anyway):")
    _say(deps, f"    {auth_url_for(redirect_uri)}")
    _say(deps, f"  After you approve, the browser fails to load {redirect_uri}/?... That is")
    _say(deps, "  expected. Copy the whole address from the address bar and paste it here.")
    pasted = deps.prompt_hidden("Redirect address (input hidden, it holds a one-time code): ")
    code = oauth.parse_pasted_redirect(pasted, redirect_uri=redirect_uri, expected_state=state)
    return code, redirect_uri


def describe_expiry(tokens: oauth.Tokens, now: datetime) -> list[str]:
    """Honest wording: the script cannot see the OAuth app's publishing status."""
    if tokens.refresh_token_expires_in is not None:
        when = now + timedelta(seconds=tokens.refresh_token_expires_in)
        days = tokens.refresh_token_expires_in / 86400
        lines = [
            f"  Google says this refresh token expires in {days:.1f} days, at "
            f"{when:%Y-%m-%d %H:%M} UTC. Run this script again before then."
        ]
        if round(days) <= TESTING_REFRESH_TOKEN_DAYS:
            lines.append(
                f"  A {TESTING_REFRESH_TOKEN_DAYS}-day limit is what Google applies while the "
                'OAuth app\'s publishing status is "Testing".'
            )
        return [*lines, "  " + PUBLISH_HELP]
    when = now + timedelta(days=TESTING_REFRESH_TOKEN_DAYS)
    return [
        "  Google did not say when this refresh token expires, and this script cannot see your "
        "OAuth app's publishing status.",
        f'  If the status is "Testing", it expires in {TESTING_REFRESH_TOKEN_DAYS} days, around '
        f'{when:%Y-%m-%d %H:%M} UTC. If it is "In production", it lasts until access is '
        "revoked or it goes unused for six months.",
        "  " + PUBLISH_HELP,
    ]


def run(config: Config, deps: Dependencies) -> int:
    _say(deps, f"Connecting Google Health for project {config.project}.")
    store = deps.secret_store(config.project)

    _say(deps, "[1/4] OAuth client")
    try:
        client = client_credentials(config, store, deps)
    except SecretStoreError as exc:
        raise Abort(str(exc), EXIT_NOT_CONNECTED) from None

    _say(deps, "[2/4] Consent")
    verifier = pkce.new_code_verifier()
    state = pkce.new_state()
    challenge = pkce.code_challenge_s256(verifier)

    def auth_url_for(redirect_uri: str) -> str:
        return oauth.authorization_url(
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            scope=config.scope,
            state=state,
            code_challenge=challenge,
        )

    google = deps.google_transport()
    try:
        code, redirect_uri = receive_code(config, auth_url_for, state, deps)
        tokens = oauth.exchange_code(
            google,
            client,
            code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            token_url=deps.token_url,
        )
    except oauth.OAuthError as exc:
        raise Abort(str(exc), EXIT_NOT_CONNECTED) from None
    missing = [
        s for s in config.scope.split() if tokens.scopes is not None and s not in tokens.scopes
    ]
    if tokens.scopes is None:
        _say(deps, "  Note: Google did not list the granted scopes; continuing.")
    if missing:
        raise Abort(
            "Access was granted without the requested scope (its box on the consent screen was "
            "left unticked), so health-sync could read nothing. Nothing was stored. Run the "
            "script again and tick every box.",
            EXIT_NOT_CONNECTED,
        )

    _say(deps, "[3/4] Refresh token")
    name = config.secret("refresh-token")
    try:
        version = store.add_version(name, tokens.refresh_token)
    except SecretStoreError as exc:
        raise Abort(str(exc), EXIT_NOT_CONNECTED) from None
    _say(deps, f"  Connected. Stored the refresh token as version {version} of secret {name}.")
    _say(deps, "  health-sync reads the latest version on its next run; no redeploy is needed.")
    if config.disable_previous:
        try:
            count = store.disable_versions_before(name, version)
            _say(deps, f"  Disabled {count} older version(s) of {name} (none destroyed).")
        except SecretStoreError as exc:
            _say(deps, f"  Warning: {exc} The new version is stored and in use.")
    for line in describe_expiry(tokens, deps.now()):
        _say(deps, line)

    _say(deps, "[4/4] Patient record")
    if config.fhir is None or config.demographics is None:
        _say(deps, "  Skipped (--skip-patient).")
        return EXIT_OK
    try:
        user_id = fetch_health_user_id(google, tokens.access_token)
        bundle = setup_bundle(config.demographics, user_id)
        outcome = write_patient(
            deps.healthcare_client(config.project, config.fhir), config.fhir.fhir_store, bundle
        )
    except SetupError as exc:
        raise Abort(
            f"{exc} The refresh token above IS stored and health-sync can use it; only the "
            "Patient record was not written. Run the script again (safe: the Patient write is a "
            "conditional update and a new refresh-token version is simply added), or check the "
            "FHIR configuration and your roles/healthcare.fhirResourceEditor on the store.",
            EXIT_PATIENT_FAILED,
        ) from None
    _say(deps, f"  Patient record {outcome} (conditional update; safe to repeat).")
    _say(deps, "Done.")
    return EXIT_OK


# google.auth's errors when Application Default Credentials are missing or expired. Matched by
# name so this module does not import google.auth.
_CREDENTIAL_ERRORS = frozenset(
    {"DefaultCredentialsError", "RefreshError", "ReauthFailError", "TransportError"}
)


def _unexpected(exc: BaseException) -> str:
    """Where it failed, without the exception's message (which could quote anything)."""
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" at {os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else ""
    hint = ""
    if type(exc).__name__ in _CREDENTIAL_ERRORS:
        hint = (
            " Your Google Cloud credentials could not be used: run "
            "`gcloud auth application-default login` and try again."
        )
    return f"Unexpected error: {type(exc).__name__}{where}. Nothing more is shown on purpose.{hint}"


def main_with_config(config: Config, deps: Dependencies) -> int:
    """:func:`run`, with every failure turned into a message and an exit code."""
    try:
        return run(config, deps)
    except Abort as exc:
        print(f"connect-google-health: {exc}", file=deps.err)
        return exc.exit_code
    except KeyboardInterrupt:
        print("connect-google-health: interrupted.", file=deps.err)
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - last line of defence: never print a message
        print(f"connect-google-health: {_unexpected(exc)}", file=deps.err)
        return EXIT_NOT_CONNECTED


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    try:
        config = load_config(
            sys.argv[1:] if argv is None else argv, os.environ if env is None else env
        )
    except UsageError as exc:
        print(f"connect-google-health: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:  # argparse: --help (0) or a bad flag (2); it printed already
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    return main_with_config(config, real_dependencies())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
