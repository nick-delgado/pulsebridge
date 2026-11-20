"""Service configuration, read from environment variables (set by Terraform on Cloud Run, T22).

Everything is checked at start-up, so a bad value fails the deploy instead of every request.
Nothing here names a deployment: a clone sets its own project, stores, URLs, origin and patient.
The defaults that exist are operating limits (timeouts, the session limits, the token lifetime).

Error messages name the variable, never its value: some values (the patient identifier, the
patient's name) are personal data in a real deployment, and the URLs identify the deployment.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Variable:
    description: str
    default: str | None = None  # None: required


# Environment variable -> what it is (the README table for T22 is this list).
VARIABLES: dict[str, Variable] = {
    "GCP_PROJECT_ID": Variable("Project that holds the Healthcare dataset"),
    "HEALTHCARE_LOCATION": Variable("Region of the dataset, e.g. us-central1"),
    "HEALTHCARE_DATASET_ID": Variable("Healthcare dataset id"),
    "FHIR_STORE_ID": Variable("FHIR R4 store with the health data (Patient, Observation, ...)"),
    "AUDIT_FHIR_STORE_ID": Variable("Audit FHIR store with the send-attempt AuditEvents (D15)"),
    "HL7V2_STORE_ID": Variable("HL7v2 store the cuff messages are stored in"),
    "PATIENT_ID": Variable("The configured patient's identifier (urn:pulsebridge:patient)"),
    "SESSION_SIGNING_KEY_FILE": Variable(
        "Path of the mounted session-signing-key secret (version latest): at least 32 random "
        "bytes, re-read after a short cache, so a new version ends every session"
    ),
    "ALLOWED_ORIGINS": Variable(
        "Comma-separated origins of the page, e.g. https://app.example.test: every POST must "
        "carry one of them in Origin, and the bot check's hostname must be one of their hosts"
    ),
    "CUFF_SIM_URL": Variable("Base URL of the cuff-sim Cloud Run service (https://...)"),
    "HEALTH_SYNC_URL": Variable(
        "Base URL of the health-sync Cloud Run service; empty when it is not deployed", ""
    ),
    "PATIENT_ASSIGNING_AUTHORITY": Variable(
        "PID-3.4 as cuff-sim sends it; with the two names, rebuilds a sent sample's bytes", ""
    ),
    "PATIENT_FAMILY_NAME": Variable("PID-5.1 as cuff-sim sends it (optional, see above)", ""),
    "PATIENT_GIVEN_NAME": Variable("PID-5.2 as cuff-sim sends it (optional, see above)", ""),
    "PATH_PREFIX": Variable(
        "Path every route is served under, e.g. /api behind the Firebase Hosting rewrite", ""
    ),
    "SESSION_TTL_SECONDS": Variable(
        "Lifetime of a session (the token's exp and the cookie's Max-Age), 60 to 86400", "86400"
    ),
    "TOKEN_ISSUER": Variable("JWT iss and aud of the session token", "pulsebridge-fhir-proxy"),
    "TRUSTED_PROXY_HOPS": Variable(
        "How many X-Forwarded-For entries, from the right, trusted proxies appended, 0 to 5"
        " (1: run.app alone; 2 behind Firebase Hosting, which module phr sets)",
        "1",
    ),
    # The bot check (D16): Google Cloud Fraud Defense, formerly reCAPTCHA Enterprise.
    "RECAPTCHA_SITE_KEY": Variable(
        "The score-based web key's ID (public; GET /auth/config hands it to the page)"
    ),
    "RECAPTCHA_ACTION": Variable(
        "The action the page names when it runs the check; an assessment for any other fails",
        "view_demo",
    ),
    "RECAPTCHA_MIN_SCORE": Variable(
        "Lowest score that passes, 0.0 to 1.0 (1.0: very likely a person)", "0.5"
    ),
    "SESSIONS_PER_ADDRESS_PER_HOUR": Variable(
        "POST /auth/session attempts allowed per client address in a sliding hour, 1 to 100",
        "5",
    ),
    "BOT_CHECKS_PER_DAY": Variable(
        "Assessments allowed per UTC day across all instances (the counter bucket), 1 to 100000",
        "300",
    ),
    # The rate limits of the public demo (T46; README "Rate limits"). Per instance, in memory:
    # the real limit is each one times the instance count (fhir-proxy's max_instance_count, 2).
    "REQUESTS_PER_ADDRESS_PER_MINUTE": Variable(
        "Requests per client address a minute, every route together (the public /auth/ routes "
        "included), 60 to 6000, at least READS_PER_SESSION_PER_MINUTE (a token bucket)",
        "300",
    ),
    "READS_PER_SESSION_PER_MINUTE": Variable(
        "GET (and HEAD) requests per demo session a minute, 60 to 1200 (a token bucket keyed on "
        "the session token's jti)",
        "120",
    ),
    "CUFF_SENDS_PER_SESSION_PER_10_MINUTES": Variable(
        "POST /cuff/readings per demo session in any 10 minutes, 1 to 30", "3"
    ),
    "SHARES_PER_SESSION_PER_10_MINUTES": Variable(
        "POST /share per demo session in any 10 minutes, 1 to 20", "2"
    ),
    "SHARES_PER_INSTANCE_PER_HOUR": Variable(
        "POST /share by all visitors together in any hour, per instance, 1 to 600", "30"
    ),
    "DEMO_COUNTER_BUCKET": Variable(
        "Cloud Storage bucket holding one counter object per UTC day (the daily bot-check cap)"
    ),
    "FHIR_TIMEOUT_SECONDS": Variable(
        "Timeout of one Cloud Healthcare API request (FHIR or HL7v2), 1 to 30", "10"
    ),
    "CUFF_FORWARD_TIMEOUT_SECONDS": Variable(
        "Timeout of a forwarded cuff request, 10 to 3000; below Firebase Hosting's 60 s by "
        "default; at least 10 s below REQUEST_TIMEOUT_SECONDS",
        "55",
    ),
    "REQUEST_BUDGET_SECONDS": Variable(
        "Overall time budget of every other request (all upstream calls together), 5 to 3000; "
        "below Firebase Hosting's 60 s by default",
        "50",
    ),
    "PUBLIC_BASE_URL": Variable(
        "The base URL the dashboard reaches the proxy at (https://.../api behind the Firebase "
        "Hosting rewrite); its path must equal PATH_PREFIX. Used for Bundle.entry.fullUrl"
    ),
    "SAMPLES_FORWARD_TIMEOUT_SECONDS": Variable(
        "Timeout of the forwarded sample list, 1 to 60", "15"
    ),
    "HEALTH_SYNC_TIMEOUT_SECONDS": Variable(
        "Timeout of health-sync's /status, 1 to 60 (health-sync budgets 20 s)", "25"
    ),
    "REQUEST_TIMEOUT_SECONDS": Variable(
        "The proxy's own Cloud Run request timeout (T22 sets the same number), 30 to 3600",
        "120",
    ),
    "STATUS_CACHE_SECONDS": Variable(
        "How long the store-derived status (counts, newest write) is reused, 0 to 600", "60"
    ),
    # The share to OpenEMR (T15). Optional as a group: all five or none (OPENEMR_GROUP). The names
    # are the ones module phr passes (infra/modules/phr/fhir_proxy.tf).
    "OPENEMR_SITE_ADDRESS": Variable(
        "OpenEMR's site_addr_oath: https://<the edge VM's internal IP>[:port], no path (module "
        "openemr's site_address). The base of every OpenEMR call; never taken from a request",
        "",
    ),
    "OPENEMR_TOKEN_URL": Variable(
        "OpenEMR's token endpoint, exactly OPENEMR_SITE_ADDRESS + /oauth2/default/token; also "
        "the signed assertion's aud (spike S1)",
        "",
    ),
    "OPENEMR_FHIR_BASE_URL": Variable(
        "OpenEMR's FHIR R4 base, exactly OPENEMR_SITE_ADDRESS + /apis/default/fhir", ""
    ),
    "OPENEMR_SCOPES": Variable(
        "The space-separated system/ scopes the client is registered with (module openemr's "
        "scopes); they must cover what the share does, and the client document must list the "
        "same set",
        "",
    ),
    "OPENEMR_CLIENT_FILE": Variable(
        "Absolute path of the mounted client document (module openemr's client secret, version "
        "latest), read on every share",
        "",
    ),
    "OPENEMR_TIMEOUT_SECONDS": Variable(
        "Timeout of one OpenEMR request, 1 to 30 (each is also capped by the request budget)",
        "10",
    ),
}

OPENEMR_GROUP = (
    "OPENEMR_SITE_ADDRESS",
    "OPENEMR_TOKEN_URL",
    "OPENEMR_FHIR_BASE_URL",
    "OPENEMR_SCOPES",
    "OPENEMR_CLIENT_FILE",
)
# OpenEMR's fixed paths under site_addr_oath (spike S1; module openemr's outputs).
OPENEMR_TOKEN_PATH = "/oauth2/default/token"  # noqa: S105 - a path, not a secret
OPENEMR_FHIR_PATH = "/apis/default/fhir"
# What the share does with each resource type (README "Security"): search and read the Patient
# and create it if absent; the same for the Encounter; create the Observation and read it back.
# OpenEMR 8.5.0 reads SMART v1 suffixes `read` as read + search and `write` as create + update +
# delete, and a SMART v2 suffix as an ordered subset of c r u d s (module openemr, "Scopes").
SHARE_NEEDS = {"Patient": set("crs"), "Encounter": set("crs"), "Observation": set("cr")}
_SCOPE = re.compile(
    r"system/(Patient|Encounter|Observation)\.(read|write|(?=[cruds])c?r?u?d?s?)", re.ASCII
)

# The forward timeout must cover cuff-sim's worst case (services/cuff-sim/README.md, "Time
# limits"): MLLP 5 + 4 x T, HTTPS 8 x T, T = its HTTPS_TIMEOUT_SECONDS. Behind Firebase Hosting
# (60 s) that means T = 6 there (48 s) and 55 s here; the README says how to raise both.
MIN_CUFF_FORWARD_SECONDS = 10
# The forward must end before Cloud Run cuts the proxy's own request off, with room to answer.
REQUEST_TIMEOUT_MARGIN_SECONDS = 10

# Resource-name parts as pulsebridge_gcp checks them; repeated here only so the error can name
# the variable before the client raises its own (which quotes the value).
_PROJECT_OR_LOCATION = re.compile(r"[a-z0-9-]{1,63}", re.ASCII)
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,256}", re.ASCII)
# Patient identifier: T06's rule for PatientInput (printable, no control characters), capped.
_PATIENT_ID = re.compile(r"[\x21-\x7e]{1,64}", re.ASCII)
_HL7_TEXT = re.compile(r"[\x20-\x5b\x5d-\x7e]{1,80}", re.ASCII)
_PREFIX = re.compile(r"(/[a-z0-9][a-z0-9-]{0,30}){0,3}", re.ASCII)
_ISSUER = re.compile(r"[A-Za-z0-9._:-]{1,100}", re.ASCII)
_HOST_NAME = re.compile(
    r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*",
    re.ASCII,
)
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")
# A reCAPTCHA key ID is 40 characters of letters, digits, - and _ today; allow some room.
_SITE_KEY = re.compile(r"[A-Za-z0-9_-]{20,100}", re.ASCII)
# "Actions may only contain alphanumeric characters, slashes, and underscores" (reCAPTCHA,
# "Install score-based keys on websites", actions).
_ACTION = re.compile(r"[A-Za-z0-9/_]{1,100}", re.ASCII)
# Cloud Storage bucket naming rules, the dot-free form (no domain-named buckets here).
_BUCKET = re.compile(r"[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]", re.ASCII)


class ConfigError(ValueError):
    """A variable is missing or unusable. The message names variables, never their values."""


@dataclass(frozen=True)
class SamplePatient:
    """What cuff-sim writes into a sample's PID (T07 ``with_patient``), when configured."""

    assigning_authority: str
    family_name: str
    given_name: str


@dataclass(frozen=True)
class OpenEmrSettings:
    """Where OpenEMR is and how to reach it (T15). The client's key, ID and OpenEMR's TLS
    certificate are NOT here: they are in the client document at ``client_file``, read on every
    share, because a re-registration on the VM replaces them together."""

    site_address: str  # https://<ip>[:port], no trailing slash: OpenEMR's site_addr_oath
    host: str  # the IP address, without brackets
    port: int
    token_url: str
    fhir_base_url: str
    scopes: tuple[str, ...]
    client_file: str
    timeout: float = 10.0

    @property
    def fhir_path(self) -> str:
        return OPENEMR_FHIR_PATH

    @property
    def token_path(self) -> str:
        return OPENEMR_TOKEN_PATH


@dataclass(frozen=True)
class ServiceConfig:
    project_id: str
    location: str
    dataset_id: str
    fhir_store_id: str
    audit_fhir_store_id: str
    hl7v2_store_id: str
    patient_id: str
    session_key_file: str
    allowed_origins: tuple[str, ...]
    cuff_sim_url: str
    recaptcha_site_key: str
    demo_counter_bucket: str
    health_sync_url: str | None = None
    sample_patient: SamplePatient | None = None
    path_prefix: str = ""
    session_ttl_seconds: int = 86400
    token_issuer: str = "pulsebridge-fhir-proxy"  # noqa: S105 - a name, not a secret
    trusted_proxy_hops: int = 1
    recaptcha_action: str = "view_demo"
    recaptcha_min_score: float = 0.5
    sessions_per_address_per_hour: int = 5
    bot_checks_per_day: int = 300
    requests_per_address_per_minute: int = 300
    reads_per_session_per_minute: int = 120
    cuff_sends_per_session_per_10_minutes: int = 3
    shares_per_session_per_10_minutes: int = 2
    shares_per_instance_per_hour: int = 30
    fhir_timeout: float = 10.0
    cuff_forward_timeout: float = 55.0
    request_budget: float = 50.0
    public_base_url: str = "https://proxy.example.test"
    samples_forward_timeout: float = 15.0
    health_sync_timeout: float = 25.0
    request_timeout: float = 120.0
    status_cache_seconds: float = 60.0
    openemr: OpenEmrSettings | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceConfig:
        env = os.environ if env is None else env
        values: dict[str, str] = {}
        missing = []
        for name, var in VARIABLES.items():
            value = (env.get(name) or "").strip()
            if not value:
                if var.default is None:
                    missing.append(name)
                    continue
                value = var.default
            values[name] = value
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")

        for name in ("GCP_PROJECT_ID", "HEALTHCARE_LOCATION"):
            _match(name, values[name], _PROJECT_OR_LOCATION, "lowercase letters, digits and -")
        for name in ("HEALTHCARE_DATASET_ID", "FHIR_STORE_ID", "AUDIT_FHIR_STORE_ID"):
            _match(name, values[name], _SEGMENT, "letters, digits, _, . and -")
        _match("HL7V2_STORE_ID", values["HL7V2_STORE_ID"], _SEGMENT, "letters, digits, _, . and -")
        if values["AUDIT_FHIR_STORE_ID"] == values["FHIR_STORE_ID"]:
            # D15: the audit data has its own store. One id for both is a wiring mistake.
            raise ConfigError("AUDIT_FHIR_STORE_ID must differ from FHIR_STORE_ID")
        _match("PATIENT_ID", values["PATIENT_ID"], _PATIENT_ID, "1 to 64 printable characters")
        if not values["SESSION_SIGNING_KEY_FILE"].startswith("/"):
            raise ConfigError("SESSION_SIGNING_KEY_FILE must be an absolute path")
        _match("RECAPTCHA_SITE_KEY", values["RECAPTCHA_SITE_KEY"], _SITE_KEY, "a reCAPTCHA key ID")
        _match(
            "RECAPTCHA_ACTION",
            values["RECAPTCHA_ACTION"],
            _ACTION,
            "1 to 100 of A-Z a-z 0-9 / _ (reCAPTCHA's rule for action names)",
        )
        _match(
            "DEMO_COUNTER_BUCKET",
            values["DEMO_COUNTER_BUCKET"],
            _BUCKET,
            "a Cloud Storage bucket name (3 to 63 of a-z 0-9 - _ .)",
        )
        _match("PATH_PREFIX", values["PATH_PREFIX"], _PREFIX, "empty or like /api")
        _match("TOKEN_ISSUER", values["TOKEN_ISSUER"], _ISSUER, "1 to 100 of A-Z a-z 0-9 . _ : -")

        sample_names = ("PATIENT_ASSIGNING_AUTHORITY", "PATIENT_FAMILY_NAME", "PATIENT_GIVEN_NAME")
        given = [n for n in sample_names if values[n]]
        sample_patient = None
        if given and len(given) != len(sample_names):
            raise ConfigError(f"set all or none of {', '.join(sample_names)}")
        if given:
            for name in sample_names:
                _match(name, values[name], _HL7_TEXT, "printable ASCII without a backslash")
            sample_patient = SamplePatient(*(values[n] for n in sample_names))

        request_timeout = _number("REQUEST_TIMEOUT_SECONDS", values, 30, 3600)
        forward = _number("CUFF_FORWARD_TIMEOUT_SECONDS", values, MIN_CUFF_FORWARD_SECONDS, 3000)
        budget = _number("REQUEST_BUDGET_SECONDS", values, 5, 3000)
        for name, value in (
            ("CUFF_FORWARD_TIMEOUT_SECONDS", forward),
            ("REQUEST_BUDGET_SECONDS", budget),
        ):
            if value + REQUEST_TIMEOUT_MARGIN_SECONDS > request_timeout:
                raise ConfigError(
                    f"{name} must be at least {REQUEST_TIMEOUT_MARGIN_SECONDS} s below "
                    "REQUEST_TIMEOUT_SECONDS"
                )
        per_address = int(_number("REQUESTS_PER_ADDRESS_PER_MINUTE", values, 60, 6000, whole=True))
        reads = int(_number("READS_PER_SESSION_PER_MINUTE", values, 60, 1200, whole=True))
        if per_address < reads:
            # Otherwise one visitor's own reads would run into its address's limit first.
            raise ConfigError(
                "REQUESTS_PER_ADDRESS_PER_MINUTE must be at least READS_PER_SESSION_PER_MINUTE"
            )
        public_base_url = _public_base_url(values["PUBLIC_BASE_URL"], values["PATH_PREFIX"])
        health_sync_url = values["HEALTH_SYNC_URL"] or None
        return cls(
            project_id=values["GCP_PROJECT_ID"],
            location=values["HEALTHCARE_LOCATION"],
            dataset_id=values["HEALTHCARE_DATASET_ID"],
            fhir_store_id=values["FHIR_STORE_ID"],
            audit_fhir_store_id=values["AUDIT_FHIR_STORE_ID"],
            hl7v2_store_id=values["HL7V2_STORE_ID"],
            patient_id=values["PATIENT_ID"],
            session_key_file=values["SESSION_SIGNING_KEY_FILE"],
            allowed_origins=_origins(values["ALLOWED_ORIGINS"]),
            cuff_sim_url=_service_url("CUFF_SIM_URL", values["CUFF_SIM_URL"]),
            recaptcha_site_key=values["RECAPTCHA_SITE_KEY"],
            demo_counter_bucket=values["DEMO_COUNTER_BUCKET"],
            health_sync_url=(
                None
                if health_sync_url is None
                else _service_url("HEALTH_SYNC_URL", health_sync_url)
            ),
            sample_patient=sample_patient,
            path_prefix=values["PATH_PREFIX"],
            session_ttl_seconds=int(_number("SESSION_TTL_SECONDS", values, 60, 86400, whole=True)),
            token_issuer=values["TOKEN_ISSUER"],
            trusted_proxy_hops=int(_number("TRUSTED_PROXY_HOPS", values, 0, 5, whole=True)),
            recaptcha_action=values["RECAPTCHA_ACTION"],
            recaptcha_min_score=_number("RECAPTCHA_MIN_SCORE", values, 0, 1),
            sessions_per_address_per_hour=int(
                _number("SESSIONS_PER_ADDRESS_PER_HOUR", values, 1, 100, whole=True)
            ),
            bot_checks_per_day=int(_number("BOT_CHECKS_PER_DAY", values, 1, 100000, whole=True)),
            requests_per_address_per_minute=per_address,
            reads_per_session_per_minute=reads,
            cuff_sends_per_session_per_10_minutes=int(
                _number("CUFF_SENDS_PER_SESSION_PER_10_MINUTES", values, 1, 30, whole=True)
            ),
            shares_per_session_per_10_minutes=int(
                _number("SHARES_PER_SESSION_PER_10_MINUTES", values, 1, 20, whole=True)
            ),
            shares_per_instance_per_hour=int(
                _number("SHARES_PER_INSTANCE_PER_HOUR", values, 1, 600, whole=True)
            ),
            fhir_timeout=_number("FHIR_TIMEOUT_SECONDS", values, 1, 30),
            cuff_forward_timeout=forward,
            request_budget=budget,
            public_base_url=public_base_url,
            samples_forward_timeout=_number("SAMPLES_FORWARD_TIMEOUT_SECONDS", values, 1, 60),
            health_sync_timeout=_number("HEALTH_SYNC_TIMEOUT_SECONDS", values, 1, 60),
            request_timeout=request_timeout,
            status_cache_seconds=_number("STATUS_CACHE_SECONDS", values, 0, 600),
            openemr=openemr_settings(values),
        )


def _match(name: str, text: str, pattern: re.Pattern[str], rule: str) -> None:
    if not pattern.fullmatch(text):
        raise ConfigError(f"{name} must be {rule}")


def _number(
    name: str, values: Mapping[str, str], low: float, high: float, *, whole: bool = False
) -> float:
    text = values[name]
    # Plain decimal only: float() would also accept "inf", "nan", "1e9" and "1_0".
    pattern = r"[0-9]{1,6}" if whole else r"[0-9]{1,6}(\.[0-9]{1,3})?"
    if not re.fullmatch(pattern, text, re.ASCII) or not low <= float(text) <= high:
        kind = "a whole number" if whole else "a number"
        raise ConfigError(f"{name} must be {kind} from {low:g} to {high:g}")
    return float(text)


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _origin_like(name: str, text: str) -> tuple[str, str, int | None]:
    """Scheme, host and port of an https URL (or http on loopback, for local development)."""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ConfigError(f"{name} must be an https URL") from None
    host = (parts.hostname or "").lower()
    netloc_host = f"[{host}]" if ":" in host else host
    ok_scheme = parts.scheme == "https" or (parts.scheme == "http" and _is_loopback(netloc_host))
    host_ok = bool(_HOST_NAME.fullmatch(host)) or _is_ip(host)
    clean = not (parts.username or parts.password or parts.query or parts.fragment)
    path_ok = parts.path == ""
    if not (ok_scheme and host_ok and clean and path_ok) or "*" in text:
        raise ConfigError(f"{name} must be an https URL with no path, query or credentials")
    return parts.scheme, netloc_host, port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _public_base_url(text: str, prefix: str) -> str:
    """``https://host[:port]`` plus exactly ``PATH_PREFIX`` (no trailing slash): the base of
    ``Bundle.entry.fullUrl``. Taken from configuration, never from ``Host`` or forwarded headers,
    which the caller controls."""
    try:
        parts = urlsplit(text.rstrip("/"))
    except ValueError:
        raise ConfigError("PUBLIC_BASE_URL must be an https URL") from None
    if parts.path != prefix:
        raise ConfigError("PUBLIC_BASE_URL's path must equal PATH_PREFIX")
    origin = _service_url("PUBLIC_BASE_URL", f"{parts.scheme}://{parts.netloc}")
    if parts.query or parts.fragment:
        raise ConfigError("PUBLIC_BASE_URL must be an https URL with no query")
    return origin + prefix


def _service_url(name: str, text: str) -> str:
    scheme, host, port = _origin_like(name, text.rstrip("/"))
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


def _origins(text: str) -> tuple[str, ...]:
    """Exact origins, as a browser sends them in ``Origin`` (RFC 6454): scheme, host, port."""
    origins = []
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        scheme, host, port = _origin_like("ALLOWED_ORIGINS", item)
        origin = f"{scheme}://{host}" + (f":{port}" if port is not None else "")
        if origin != item:
            # The comparison with the Origin header is exact, so the configured form must be
            # the canonical one (lowercase host, no trailing slash, no default port).
            raise ConfigError("ALLOWED_ORIGINS entries must be written exactly as an Origin header")
        origins.append(origin)
    if not origins:
        raise ConfigError("ALLOWED_ORIGINS must name at least one origin")
    return tuple(origins)


def openemr_settings(values: Mapping[str, str]) -> OpenEmrSettings | None:
    """The OpenEMR group (T15): all five variables, or none (then sharing answers 503
    ``share-not-configured`` and the status strip says ``not-configured``). Checked here, at
    start-up, so a wrong address fails the deploy; errors name variables, never values."""
    given = [n for n in OPENEMR_GROUP if values.get(n)]
    if not given:
        return None
    if len(given) != len(OPENEMR_GROUP):
        missing = [n for n in OPENEMR_GROUP if n not in given]
        raise ConfigError(f"set all or none of the OpenEMR variables; missing {', '.join(missing)}")

    site = values["OPENEMR_SITE_ADDRESS"].rstrip("/")
    try:
        parts = urlsplit(site)
        port = parts.port
    except ValueError:
        raise ConfigError("OPENEMR_SITE_ADDRESS must be https://<IP address>[:port]") from None
    host = parts.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    # The edge VM's internal address (D14): a private IP literal, the form module openemr's
    # certificate names (subject alternative name IP:<internal_ip>). A host name would need DNS
    # the VPC does not have, and a public address would mean OpenEMR is exposed.
    if (
        parts.scheme != "https"
        or address is None
        or not address.is_private
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
        or "*" in site
    ):
        raise ConfigError(
            "OPENEMR_SITE_ADDRESS must be https://<private IP address>[:port] with no path, "
            "query or credentials"
        )
    netloc = f"[{host}]" if address.version == 6 else host
    site = f"https://{netloc}" + (f":{port}" if port is not None else "")
    for name, path in (
        ("OPENEMR_TOKEN_URL", OPENEMR_TOKEN_PATH),
        ("OPENEMR_FHIR_BASE_URL", OPENEMR_FHIR_PATH),
    ):
        if values[name].rstrip("/") != site + path:
            raise ConfigError(f"{name} must be OPENEMR_SITE_ADDRESS followed by {path}")

    scopes = values["OPENEMR_SCOPES"].split()
    if (
        not scopes
        or len(scopes) != len(set(scopes))
        or not all(_SCOPE.fullmatch(s) for s in scopes)
        or len(values["OPENEMR_SCOPES"]) > 512
    ):
        raise ConfigError(
            "OPENEMR_SCOPES must be distinct system/Patient, system/Encounter or "
            "system/Observation scopes (.read, .write or an ordered cruds subset)"
        )
    granted = scope_permissions(scopes)
    lacking = sorted(t for t, need in SHARE_NEEDS.items() if not need <= granted.get(t, set()))
    if lacking:
        raise ConfigError(
            f"OPENEMR_SCOPES does not allow what the share does on {', '.join(lacking)}"
        )

    if not values["OPENEMR_CLIENT_FILE"].startswith("/"):
        raise ConfigError("OPENEMR_CLIENT_FILE must be an absolute path")
    return OpenEmrSettings(
        site_address=site,
        host=host,
        port=port if port is not None else 443,
        token_url=site + OPENEMR_TOKEN_PATH,
        fhir_base_url=site + OPENEMR_FHIR_PATH,
        scopes=tuple(scopes),
        client_file=values["OPENEMR_CLIENT_FILE"],
        timeout=_number("OPENEMR_TIMEOUT_SECONDS", values, 1, 30),
    )


def scope_permissions(scopes: list[str] | tuple[str, ...]) -> dict[str, set[str]]:
    """Resource type -> the letters of c r u d s the scopes grant (OpenEMR 8.5.0's reading:
    ``read`` = r s, ``write`` = c u d; SMART v2 letters as written)."""
    granted: dict[str, set[str]] = {}
    for scope in scopes:
        match = _SCOPE.fullmatch(scope)
        if match is None:
            continue
        resource, suffix = match.groups()
        letters = {"read": set("rs"), "write": set("cud")}.get(suffix, set(suffix))
        granted.setdefault(resource, set()).update(letters)
    return granted
