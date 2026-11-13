"""Google Health API heart-rate roll-ups to FHIR.

Input is the JSON shape recorded in spike S3 (``docs/spikes/s3.md``) for
``POST v4/users/me/dataTypes/heart-rate/dataPoints:rollUp``, one item per window::

    {"startTime": "2026-01-01T14:05:00Z", "endTime": "2026-01-01T14:10:00Z",
     "heartRate": {"beatsPerMinuteMin": 70, "beatsPerMinuteAvg": 72.4, "beatsPerMinuteMax": 75}}

These are plain dicts (``json.loads`` output), not Google client objects, so the mapping can be
tested without the API.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo

from pulsebridge_fhir.bundle import build_transaction_bundle
from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.inputs import DeviceInput, HeartRateWindow, PatientInput, SourceInfo
from pulsebridge_fhir.resources import (
    BuiltResource,
    build_device,
    build_heart_rate_observation,
    build_patient,
    build_provenance,
)

# Google's UTC times: whole seconds, "Z" suffix (spike S3, physicalTime and roll-up bounds).
_UTC_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
# Google's offsets: signed seconds with an "s" suffix, for example "-18000s" (spike S3).
_GOOGLE_OFFSET = re.compile(r"^([+-]?)(\d+)s$")

ROLLUP_RECORD_TYPE = "google-health-heart-rate-rollup"
WHERE = "heart-rate window"


def parse_google_utc_offset(value: str) -> timezone:
    """``"-18000s"`` to a fixed-offset ``timezone`` (UTC-05:00).

    FHIR and ISO 8601 offsets are whole minutes, from -14:00 to +14:00, so anything else is
    rejected rather than rounded.
    """
    match = _GOOGLE_OFFSET.match(value) if isinstance(value, str) else None
    if not match:
        raise FhirInputError(f"utcOffset {value!r} is not Google's '<seconds>s' format")
    seconds = int(match.group(2)) * (-1 if match.group(1) == "-" else 1)
    if seconds % 60:
        raise FhirInputError(f"utcOffset {value!r} is not a whole number of minutes")
    if abs(seconds) > 14 * 3600:
        raise FhirInputError(f"utcOffset {value!r} is outside -14:00 to +14:00")
    return timezone(timedelta(seconds=seconds))


def _parse_utc(point: Mapping, name: str) -> datetime:
    value = point.get(name)
    if value is None or value == "":
        raise FhirInputError(f"{WHERE}: {name} is missing")
    if not isinstance(value, str) or not _UTC_TIME.match(value):
        raise FhirInputError(
            f"{WHERE}: {name} {value!r} is not a UTC time like 2026-01-01T14:05:00Z"
        )
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError as exc:  # matches the pattern but is not a real date, e.g. month 13
        raise FhirInputError(f"{WHERE}: {name} {value!r} is not a valid time") from exc


def _parse_bpm(heart_rate: Mapping, name: str) -> int | float:
    value = heart_rate.get(name)
    if value is None:
        raise FhirInputError(f"{WHERE}: {name} is missing")
    # Roll-ups send JSON numbers (spike S3), but Google's raw points send beatsPerMinute as a
    # string (proto3 JSON writes int64 as a string). Accept a numeric string too, nothing else.
    if isinstance(value, str):
        try:
            value = float(value) if any(c in value for c in ".eE") else int(value)
        except ValueError as exc:
            raise FhirInputError(f"{WHERE}: {name} {value!r} is not a number") from exc
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FhirInputError(f"{WHERE}: {name} must be a number, got {type(value).__name__}")
    return value


def window_from_rollup(point: Mapping) -> HeartRateWindow:
    """One roll-up item to a validated :class:`HeartRateWindow`."""
    if not isinstance(point, Mapping):
        raise FhirInputError(f"{WHERE}: expected a JSON object, got {type(point).__name__}")
    heart_rate = point.get("heartRate")
    if not isinstance(heart_rate, Mapping):
        raise FhirInputError(f"{WHERE}: heartRate is missing")
    return HeartRateWindow(
        start=_parse_utc(point, "startTime"),
        end=_parse_utc(point, "endTime"),
        bpm_min=_parse_bpm(heart_rate, "beatsPerMinuteMin"),
        bpm_avg=_parse_bpm(heart_rate, "beatsPerMinuteAvg"),
        bpm_max=_parse_bpm(heart_rate, "beatsPerMinuteMax"),
    )


@dataclass(frozen=True)
class GoogleDataSource:
    """Google's ``dataSource`` for the data: platform and recording method (spike S3).

    The roll-up response itself carries no ``dataSource``; the caller supplies it (from a listed
    data point or from configuration).
    """

    platform: str = "FITBIT"
    recording_method: str | None = "PASSIVELY_MEASURED"


def rollup_source(window: HeartRateWindow, data_source: GoogleDataSource) -> SourceInfo:
    """The Provenance source record for one window. Google gives windows no id, so the id is the
    window's UTC bounds, which is how ``health-sync`` would ask for the same window again."""
    return SourceInfo(
        platform=data_source.platform,
        recording_method=data_source.recording_method,
        record_type=ROLLUP_RECORD_TYPE,
        record_id=f"{window.start_utc}/{window.end_utc}",
    )


def heart_rate_resources(
    windows: Iterable[HeartRateWindow],
    *,
    patient: BuiltResource,
    device: BuiltResource,
    data_source: GoogleDataSource,
    tz: tzinfo,
    recorded: datetime,
    assembler: str = "PulseBridge health-sync",
) -> list[BuiltResource]:
    """An Observation and its Provenance for each window, in input order."""
    out: list[BuiltResource] = []
    for window in windows:
        observation = build_heart_rate_observation(window, patient=patient, device=device, tz=tz)
        out.append(observation)
        out.append(
            build_provenance(
                target=observation,
                device=device,
                source=rollup_source(window, data_source),
                recorded=recorded,
                assembler=assembler,
            )
        )
    return out


def heart_rate_transaction(
    rollup_points: Iterable[Mapping],
    *,
    patient: PatientInput,
    device: DeviceInput,
    data_source: GoogleDataSource,
    tz: tzinfo,
    recorded: datetime,
    assembler: str = "PulseBridge health-sync",
) -> dict:
    """Google roll-up JSON items to one transaction Bundle.

    The Bundle holds the Patient, the Device, and an Observation plus Provenance per window, all
    written with ``PUT`` so that sending it twice changes nothing. Patient and Device are included
    every time so the Bundle is self-contained: the Observations refer to them by ``urn:uuid``.

    ``tz`` is the user's time zone (a ``zoneinfo.ZoneInfo`` follows daylight saving per window) or
    the fixed offset from :func:`parse_google_utc_offset`.
    """
    built_patient = build_patient(patient)
    built_device = build_device(device, patient=built_patient)
    windows = [window_from_rollup(point) for point in rollup_points]
    return build_transaction_bundle(
        [
            built_patient,
            built_device,
            *heart_rate_resources(
                windows,
                patient=built_patient,
                device=built_device,
                data_source=data_source,
                tz=tz,
                recorded=recorded,
                assembler=assembler,
            ),
        ]
    )
