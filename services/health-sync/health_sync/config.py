"""Service configuration, read from environment variables (set by Terraform on Cloud Run, T20).

Nothing here names a deployment: a clone sets its own values. Every check runs at start-up, so a
bad deploy fails visibly instead of failing every scheduled run later. Error messages name the
variable, never its value (the secret paths are not secret, but the rule is simpler kept whole).

The three Google Health secrets (decision D2) are **files**, not values: Cloud Run mounts each
Secret Manager secret as a volume at the path given here, and reads the latest version every time
the file is read. See :mod:`health_sync.credentials` and README "Secrets".
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The one scope health-sync needs (spike S3: it covers heart rate; the API has no heart-rate-only
# scope). A default, not an identity: every deployment uses the same Google scope string.
DEFAULT_SCOPE = (
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
)

# Required variables -> what they are.
REQUIRED = {
    "GCP_PROJECT_ID": "Project that holds the Healthcare dataset",
    "HEALTHCARE_LOCATION": "Region of the dataset, e.g. us-central1",
    "HEALTHCARE_DATASET_ID": "Healthcare dataset id",
    "FHIR_STORE_ID": "FHIR R4 store id (the main store with health data)",
    "PATIENT_ID": "The configured patient id (the same one the setup step wrote, D11)",
    "TIME_ZONE": "The user's IANA time zone, e.g. America/New_York (not a fixed offset)",
    "GOOGLE_HEALTH_CLIENT_ID_FILE": "Path of the mounted OAuth client ID secret",
    "GOOGLE_HEALTH_CLIENT_SECRET_FILE": "Path of the mounted OAuth client secret",
    "GOOGLE_HEALTH_REFRESH_TOKEN_FILE": "Path of the mounted refresh token secret",
}

# Optional variables -> (default, what they are). Integers unless noted.
OPTIONAL = {
    "GOOGLE_HEALTH_SCOPE": (DEFAULT_SCOPE, "OAuth scope the refresh token must carry"),
    "LOOKBACK_MINUTES": ("180", "How far back each sync re-requests (overlap with earlier runs)"),
    "FINAL_AFTER_MINUTES": ("60", "A window is written 'final' once it ended this long ago"),
    "SYNC_INTERVAL_MINUTES": ("15", "How often Cloud Scheduler runs the sync (T20's schedule)"),
    "BATCH_WINDOWS": ("100", "Roll-up windows per transaction Bundle (2 entries each, plus 2)"),
    "HTTP_TIMEOUT_SECONDS": ("30", "Timeout of one Google Health or OAuth request"),
    "FHIR_TIMEOUT_SECONDS": ("60", "Timeout of one executeBundle request"),
    "RUN_DEADLINE_SECONDS": ("150", "A sync run gives up retrying after this long"),
    "MAX_ATTEMPTS": ("4", "Attempts per request for 429, 5xx and network errors"),
    "STATUS_CACHE_SECONDS": ("60", "How long the status route reuses its last answer (0: never)"),
}

WINDOW = timedelta(minutes=5)  # decision D8: five-minute roll-ups
# The API's limit for one heart-rate roll-up request (discovery document, RollUpDataPointsRequest
# .range, and spike S3): 14 days.
MAX_ROLLUP_RANGE = timedelta(days=14)
MAX_BATCH_WINDOWS = 1000  # 2,002 entries; Google: transactions over 1,000 entries may time out

# Fixed-offset zone names that would hide a daylight-saving change. IANA "Etc/GMT+5" is a real
# zone, but it is also a fixed offset, so it is refused for the same reason.
_FIXED_OFFSET = re.compile(r"^(?:Etc/)?(?:GMT|UTC)?[+-][0-9]{1,2}(?::?[0-9]{2})?$", re.IGNORECASE)


class ConfigError(ValueError):
    """Configuration is missing or invalid; the message names variables, never values."""


@dataclass(frozen=True)
class SecretPaths:
    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True)
class ServiceConfig:
    project_id: str
    location: str
    dataset_id: str
    fhir_store_id: str
    patient_id: str
    time_zone: ZoneInfo
    secrets: SecretPaths
    scope: str = DEFAULT_SCOPE
    lookback: timedelta = timedelta(minutes=180)
    final_after: timedelta = timedelta(minutes=60)
    sync_interval: timedelta = timedelta(minutes=15)
    batch_windows: int = 100
    http_timeout: float = 30.0
    fhir_timeout: float = 60.0
    run_deadline: float = 150.0
    max_attempts: int = 4
    status_cache_seconds: float = 60.0

    def __post_init__(self) -> None:
        check_timing(self)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceConfig:
        env = os.environ if env is None else env
        values = {name: (env.get(name) or "").strip() for name in REQUIRED}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        for name in (n for n in REQUIRED if n.endswith("_FILE")):
            if not values[name].startswith("/"):
                raise ConfigError(f"{name} must be an absolute path")
        optional = {
            name: (env.get(name) or "").strip() or default
            for name, (default, _) in OPTIONAL.items()
        }
        return cls(
            project_id=values["GCP_PROJECT_ID"],
            location=values["HEALTHCARE_LOCATION"],
            dataset_id=values["HEALTHCARE_DATASET_ID"],
            fhir_store_id=values["FHIR_STORE_ID"],
            patient_id=values["PATIENT_ID"],
            time_zone=parse_time_zone(values["TIME_ZONE"]),
            secrets=SecretPaths(
                client_id=values["GOOGLE_HEALTH_CLIENT_ID_FILE"],
                client_secret=values["GOOGLE_HEALTH_CLIENT_SECRET_FILE"],
                refresh_token=values["GOOGLE_HEALTH_REFRESH_TOKEN_FILE"],
            ),
            scope=optional["GOOGLE_HEALTH_SCOPE"],
            lookback=timedelta(minutes=_int(optional, "LOOKBACK_MINUTES", 15, 14 * 24 * 60)),
            final_after=timedelta(minutes=_int(optional, "FINAL_AFTER_MINUTES", 0, 14 * 24 * 60)),
            sync_interval=timedelta(minutes=_int(optional, "SYNC_INTERVAL_MINUTES", 1, 24 * 60)),
            batch_windows=_int(optional, "BATCH_WINDOWS", 1, MAX_BATCH_WINDOWS),
            http_timeout=float(_int(optional, "HTTP_TIMEOUT_SECONDS", 1, 300)),
            fhir_timeout=float(_int(optional, "FHIR_TIMEOUT_SECONDS", 1, 600)),
            run_deadline=float(_int(optional, "RUN_DEADLINE_SECONDS", 10, 3600)),
            max_attempts=_int(optional, "MAX_ATTEMPTS", 1, 10),
            status_cache_seconds=float(_int(optional, "STATUS_CACHE_SECONDS", 0, 3600)),
        )


def _int(values: Mapping[str, str], name: str, low: int, high: int) -> int:
    text = values[name]
    if not re.fullmatch(r"[0-9]{1,7}", text):
        raise ConfigError(f"{name} must be a whole number")
    number = int(text)
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


def parse_time_zone(name: str) -> ZoneInfo:
    """An IANA zone such as ``America/New_York``. Fixed offsets are refused: a roll-up item has no
    ``utcOffset`` (spike S3), so the zone is what puts the right offset on each side of a
    daylight-saving change (T06 README, "Times and time zones")."""
    if _FIXED_OFFSET.fullmatch(name):
        raise ConfigError(
            "TIME_ZONE must be an IANA zone name such as America/New_York, not a fixed UTC offset"
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("TIME_ZONE is not a known IANA time zone") from exc


def check_timing(config: ServiceConfig) -> None:
    """The overlap rule that makes ``preliminary`` safe (README, "Overlap and status").

    A window written ``preliminary`` must be re-requested later, or it stays preliminary forever.
    It is re-requested as long as it is inside the look-back of a later run. So the look-back must
    cover at least the "still changing" period plus one run interval:
    ``LOOKBACK >= FINAL_AFTER + SYNC_INTERVAL``. Ranges are also whole windows.
    """
    for name, value in (
        ("LOOKBACK_MINUTES", config.lookback),
        ("FINAL_AFTER_MINUTES", config.final_after),
        ("SYNC_INTERVAL_MINUTES", config.sync_interval),
    ):
        if value % WINDOW:
            raise ConfigError(f"{name} must be a multiple of 5 (the roll-up window)")
    if config.lookback > MAX_ROLLUP_RANGE:
        raise ConfigError("LOOKBACK_MINUTES must not exceed 14 days (one roll-up request)")
    if config.lookback < config.final_after + config.sync_interval:
        raise ConfigError(
            "LOOKBACK_MINUTES must be at least FINAL_AFTER_MINUTES + SYNC_INTERVAL_MINUTES, "
            "or a preliminary window could fall out of every later run's range"
        )
    if not 1 <= config.batch_windows <= MAX_BATCH_WINDOWS:
        raise ConfigError(f"BATCH_WINDOWS must be between 1 and {MAX_BATCH_WINDOWS}")
