"""Configuration from environment variables (set by Terraform on the Cloud Run job, T20/T25).

The same rules as health-sync: everything is checked before any work starts, and an error names
the variable, never its value. Nothing names a deployment.

The FHIR store variables are needed only for a real run: ``--dry-run`` makes no requests at all,
so it works on a laptop with no GCP configuration.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pulsebridge_fhir import FhirInputError, PatientInput

from pulsebridge_seed.synthetic import PATIENT_ID_PREFIX

# Needed for every run, dry or not.
ALWAYS = {
    "PATIENT_ID": "The synthetic patient's identifier; must start with SYN-",
    "TIME_ZONE": "IANA time zone of the synthetic person, e.g. America/New_York",
}
# Needed to write.
FHIR = {
    "GCP_PROJECT_ID": "Project that holds the Healthcare dataset",
    "HEALTHCARE_LOCATION": "Region of the dataset, e.g. us-central1",
    "HEALTHCARE_DATASET_ID": "Healthcare dataset id",
    "FHIR_STORE_ID": "FHIR R4 store id (the main store with health data)",
}
# Optional -> (default, meaning).
OPTIONAL = {
    "SYNTHETIC_SEED": ("1", "Seed of the generator; another seed is another synthetic person"),
    "FINAL_AFTER_MINUTES": ("60", "A window is written 'final' once it ended this long ago"),
    "BATCH_WINDOWS": ("100", "Windows per transaction Bundle (2 entries each, plus 2)"),
    "FHIR_TIMEOUT_SECONDS": ("60", "Timeout of one FHIR request"),
    "MAX_ATTEMPTS": ("4", "Attempts per FHIR request for 429, 5xx, 409 and network errors"),
}

MAX_BATCH_WINDOWS = 1000  # 2,002 entries; Google: transactions over 1,000 entries may time out
MAX_SEED = 2**63 - 1
_FIXED_OFFSET = re.compile(r"^(?:Etc/)?(?:GMT|UTC)?[+-][0-9]{1,2}(?::?[0-9]{2})?$", re.IGNORECASE)


class ConfigError(ValueError):
    """Configuration is missing or invalid; the message names variables, never values."""


@dataclass(frozen=True)
class FhirTarget:
    project_id: str
    location: str
    dataset_id: str
    fhir_store_id: str


@dataclass(frozen=True)
class SeedConfig:
    patient_id: str
    time_zone: ZoneInfo
    fhir: FhirTarget | None  # None only for a dry run
    seed: int = 1
    final_after: timedelta = timedelta(minutes=60)
    batch_windows: int = 100
    fhir_timeout: float = 60.0
    max_attempts: int = 4

    def __post_init__(self) -> None:
        # Checked here too, not only in from_env: a SeedConfig built any other way (a test, a
        # future caller) still cannot target a non-synthetic patient identifier.
        parse_patient_id(self.patient_id)
        if not 1 <= self.batch_windows <= MAX_BATCH_WINDOWS:
            raise ConfigError(f"BATCH_WINDOWS must be between 1 and {MAX_BATCH_WINDOWS}")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        need_fhir: bool = True,
        seed_override: str | None = None,
    ) -> SeedConfig:
        env = os.environ if env is None else env
        names = dict(ALWAYS) | (FHIR if need_fhir else {})
        values = {name: (env.get(name) or "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        optional = {
            name: (env.get(name) or "").strip() or default
            for name, (default, _) in OPTIONAL.items()
        }
        if seed_override is not None:
            optional["SYNTHETIC_SEED"] = seed_override.strip()
        fhir = None
        if need_fhir:
            fhir = FhirTarget(
                values["GCP_PROJECT_ID"],
                values["HEALTHCARE_LOCATION"],
                values["HEALTHCARE_DATASET_ID"],
                values["FHIR_STORE_ID"],
            )
        final_after = _int(optional, "FINAL_AFTER_MINUTES", 0, 14 * 24 * 60)
        if final_after % 5:
            raise ConfigError("FINAL_AFTER_MINUTES must be a multiple of 5 (the roll-up window)")
        return cls(
            patient_id=parse_patient_id(values["PATIENT_ID"]),
            time_zone=parse_time_zone(values["TIME_ZONE"]),
            fhir=fhir,
            seed=_int(optional, "SYNTHETIC_SEED", 0, MAX_SEED),
            final_after=timedelta(minutes=final_after),
            batch_windows=_int(optional, "BATCH_WINDOWS", 1, MAX_BATCH_WINDOWS),
            fhir_timeout=float(_int(optional, "FHIR_TIMEOUT_SECONDS", 1, 600)),
            max_attempts=_int(optional, "MAX_ATTEMPTS", 1, 10),
        )


def _int(values: Mapping[str, str], name: str, low: int, high: int) -> int:
    text = values[name]
    if not re.fullmatch("[0-9]+", text):
        raise ConfigError(f"{name} must be a whole number")
    # A long string of digits is out of range, not "not a number"; it is never converted.
    number = int(text) if len(text) <= len(str(high)) else high + 1
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


def parse_patient_id(value: str) -> str:
    """The synthetic patient's identifier. It must start with ``SYN-`` so that it can never be
    the identifier of a deployment's real patient (README, "Safety"), and must be a valid T06
    patient identifier."""
    if not value.startswith(PATIENT_ID_PREFIX) or len(value) == len(PATIENT_ID_PREFIX):
        raise ConfigError(
            f"PATIENT_ID must start with {PATIENT_ID_PREFIX} (the seeder writes synthetic "
            "patients only)"
        )
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        raise ConfigError("PATIENT_ID may hold only letters, digits, '.', '_' and '-' (1 to 64)")
    try:
        PatientInput(value)
    except FhirInputError:
        raise ConfigError("PATIENT_ID is not a valid patient identifier") from None
    return value


def parse_time_zone(name: str) -> ZoneInfo:
    """An IANA zone, as health-sync requires: a fixed offset would put local times an hour off
    on the far side of a daylight-saving change (T06 README, "Times and time zones")."""
    if _FIXED_OFFSET.fullmatch(name):
        raise ConfigError(
            "TIME_ZONE must be an IANA zone name such as America/New_York, not a fixed UTC offset"
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("TIME_ZONE is not a known IANA time zone") from exc
