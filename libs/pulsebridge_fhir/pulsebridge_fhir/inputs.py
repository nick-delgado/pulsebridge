"""Plain-Python inputs to the builders.

Callers (``health-sync``, ``v2-to-fhir``, the seeder, the share to OpenEMR) convert whatever they
received (a Google Health JSON window, an HL7v2 segment, configuration) into these dataclasses.
Each one validates itself when constructed, so a builder never sees a half-valid input and every
problem surfaces as a ``FhirInputError`` naming the field.
"""

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from pulsebridge_fhir import codes
from pulsebridge_fhir.errors import FhirInputError

# Google documents heart rate as 1 to 300 beats per minute (spike S3); the same range is applied to
# every heart-rate value, so a sensor glitch or a unit mix-up (for example Hz) is rejected.
HEART_RATE_RANGE = (1, 300)
# Plausibility bounds for a cuff reading in mm[Hg]. Wider than the simulator's input bounds (T13),
# narrow enough to catch a kPa value (about 16) or a swapped field.
SYSTOLIC_RANGE = (50, 300)
DIASTOLIC_RANGE = (20, 200)

# FHIR administrative-gender (required binding on Patient.gender).
GENDERS = ("male", "female", "other", "unknown")

# Observation.status values that make sense for a measurement we write (R4 observation-status).
OBSERVATION_STATUSES = ("preliminary", "final", "amended", "corrected")

_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _require_text(where: str, name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FhirInputError(f"{where}: {name} is missing or empty")
    if value != value.strip():
        raise FhirInputError(f"{where}: {name} has leading or trailing whitespace")
    return value


def _require_aware(where: str, name: str, value: object) -> datetime:
    if value is None:
        raise FhirInputError(f"{where}: {name} is missing")
    if not isinstance(value, datetime):
        raise FhirInputError(f"{where}: {name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        # A FHIR dateTime with a time part must carry a time-zone offset (R4 datatypes.html).
        raise FhirInputError(f"{where}: {name} has no time zone; FHIR needs an offset")
    return value


def _require_number(where: str, name: str, value: object, low: float, high: float) -> int | float:
    # bool is a subclass of int in Python; True must not pass as 1 beat per minute.
    if value is None:
        raise FhirInputError(f"{where}: {name} is missing")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FhirInputError(f"{where}: {name} must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise FhirInputError(f"{where}: {name} must be a finite number")
    if not low <= value <= high:
        raise FhirInputError(f"{where}: {name} ({value}) is outside {low} to {high}")
    return value


@dataclass(frozen=True)
class PatientInput:
    """The person the data is about.

    ``patient_id`` is PulseBridge's identifier for the person. It becomes the ``PT``-typed
    identifier (the conditional-update key, and what OpenEMR searches on), so it must stay the same
    for the life of the deployment. It comes from configuration (or PID-3 on the HL7v2 path),
    never from code. Name, gender and birth date are required because OpenEMR rejects a Patient
    without them (spike S1).
    """

    patient_id: str
    family: str
    given: tuple[str, ...]
    gender: str
    birth_date: date
    google_health_user_id: str | None = None

    def __post_init__(self) -> None:
        where = "patient"
        _require_text(where, "patient_id", self.patient_id)
        _require_text(where, "family", self.family)
        if isinstance(self.given, str) or not self.given:
            raise FhirInputError(f"{where}: given must be a non-empty tuple of names")
        for name in self.given:
            _require_text(where, "given name", name)
        if self.gender not in GENDERS:
            raise FhirInputError(
                f"{where}: gender {self.gender!r} is not one of {', '.join(GENDERS)}"
            )
        if not isinstance(self.birth_date, date) or isinstance(self.birth_date, datetime):
            raise FhirInputError(f"{where}: birth_date must be a date")
        if self.google_health_user_id is not None:
            _require_text(where, "google_health_user_id", self.google_health_user_id)


@dataclass(frozen=True)
class DeviceInput:
    """The device that took the measurement.

    Google supplies only ``dataSource.device.displayName`` (for example ``"Pixel Watch 4"``) and no
    serial number, so the Device identifier is derived from the patient and this name (spike S3).
    On the HL7v2 path the name comes from the sending application.
    """

    display_name: str
    manufacturer: str | None = None

    def __post_init__(self) -> None:
        _require_text("device", "display_name", self.display_name)
        if self.manufacturer is not None:
            _require_text("device", "manufacturer", self.manufacturer)


@dataclass(frozen=True)
class HeartRateWindow:
    """One heart-rate roll-up window: minimum, average and maximum beats per minute.

    ``start`` and ``end`` are the window bounds as instants (Google sends UTC). Build one from the
    Google Health API JSON with :func:`pulsebridge_fhir.google_health.window_from_rollup`.
    """

    start: datetime
    end: datetime
    bpm_min: int | float
    bpm_avg: int | float
    bpm_max: int | float

    def __post_init__(self) -> None:
        where = "heart-rate window"
        _require_aware(where, "start", self.start)
        _require_aware(where, "end", self.end)
        if self.end <= self.start:
            raise FhirInputError(f"{where}: end ({self.end}) is not after start ({self.start})")
        low, high = HEART_RATE_RANGE
        _require_number(where, "minimum", self.bpm_min, low, high)
        _require_number(where, "average", self.bpm_avg, low, high)
        _require_number(where, "maximum", self.bpm_max, low, high)
        if not self.bpm_min <= self.bpm_avg <= self.bpm_max:
            raise FhirInputError(
                f"{where}: expected minimum <= average <= maximum, got "
                f"{self.bpm_min} / {self.bpm_avg} / {self.bpm_max}"
            )

    @property
    def start_utc(self) -> str:
        """Window start in UTC, ``YYYY-MM-DDTHH:MM:SSZ``: the time part of the idempotency key."""
        return self.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def end_utc(self) -> str:
        return self.end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class BloodPressureReading:
    """One cuff reading: systolic and diastolic pressure at one time.

    ``source_key`` identifies the reading at its source and is the idempotency key, so a
    redelivered message maps to the same Observation. On the HL7v2 path use the sending
    application and the message control ID (MSH-3 and MSH-10). ``unit`` must be the UCUM code
    ``mm[Hg]``; ``v2-to-fhir`` passes OBX-6 here so a wrong unit is rejected, not relabelled.
    """

    effective: datetime
    systolic: int | float
    diastolic: int | float
    source_key: str
    unit: str = codes.UCUM_MM_HG

    def __post_init__(self) -> None:
        where = "blood-pressure reading"
        _require_aware(where, "effective", self.effective)
        _require_text(where, "source_key", self.source_key)
        if self.unit != codes.UCUM_MM_HG:
            raise FhirInputError(
                f"{where}: unit {self.unit!r} is not the UCUM code {codes.UCUM_MM_HG!r}"
            )
        _require_number(where, "systolic", self.systolic, *SYSTOLIC_RANGE)
        _require_number(where, "diastolic", self.diastolic, *DIASTOLIC_RANGE)
        if self.systolic <= self.diastolic:
            raise FhirInputError(
                f"{where}: systolic ({self.systolic}) must be greater than "
                f"diastolic ({self.diastolic})"
            )


@dataclass(frozen=True)
class SourceInfo:
    """Where a reading came from, recorded in its Provenance.

    - ``platform``: the upstream platform, for example Google's ``dataSource.platform``
      (``"FITBIT"``) or ``"HL7V2_STORE"`` for the cuff path. Upper-case token.
    - ``recording_method``: for example Google's ``dataSource.recordingMethod``
      (``"PASSIVELY_MEASURED"``). Optional; upper-case token.
    - ``record_type`` and ``record_id``: what the source record is and its id, for example
      ``"hl7v2-message"`` and the HL7v2 store message name, or ``"google-health-heart-rate-rollup"``
      and the window bounds. Together they let the trace panel find the original.
    """

    platform: str
    record_type: str
    record_id: str
    recording_method: str | None = None

    def __post_init__(self) -> None:
        where = "source"
        for name in ("platform", "recording_method"):
            value = getattr(self, name)
            if value is None and name == "recording_method":
                continue
            _require_text(where, name, value)
            if not _TOKEN.match(value):
                raise FhirInputError(f"{where}: {name} {value!r} must be an upper-case token")
        _require_text(where, "record_type", self.record_type)
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", self.record_type):
            raise FhirInputError(
                f"{where}: record_type {self.record_type!r} must be lower-case words joined by '-'"
            )
        _require_text(where, "record_id", self.record_id)
