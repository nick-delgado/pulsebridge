"""Google Health API heart-rate roll-ups to FHIR.

Input is the JSON shape recorded in spike S3 (``docs/spikes/s3.md``) for
``POST v4/users/me/dataTypes/heart-rate/dataPoints:rollUp``, one item per window::

    {"startTime": "2026-01-01T14:05:00Z", "endTime": "2026-01-01T14:10:00Z",
     "heartRate": {"beatsPerMinuteMin": 70, "beatsPerMinuteAvg": 72.4, "beatsPerMinuteMax": 75}}

These are plain dicts (``json.loads`` output), not Google client objects, so the mapping can be
tested without the API.
"""

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo

from pulsebridge_fhir.bundle import build_ingest_bundle
from pulsebridge_fhir.errors import FhirInputError
from pulsebridge_fhir.inputs import DeviceInput, HeartRateWindow, PatientInput, SourceInfo
from pulsebridge_fhir.resources import (
    BuiltResource,
    build_device,
    build_heart_rate_observation,
    build_patient,
    build_provenance,
)

# Patterns use [0-9], not \d: in Python \d also matches non-ASCII digits such as Arabic-Indic.
# Google's UTC times: whole seconds, "Z" suffix (spike S3, physicalTime and roll-up bounds).
_UTC_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
# Google's offsets: signed seconds with an "s" suffix, for example "-18000s" (spike S3).
_GOOGLE_OFFSET = re.compile(r"^([+-]?)([0-9]+)s$")
# A heart rate sent as a string: plain ASCII digits with an optional decimal part. No sign, no
# exponent, no "_" separators, no other scripts' digits (Python's int() and float() accept all of
# those, so the pattern is checked first).
_DECIMAL = re.compile(r"^[0-9]+(\.[0-9]+)?$")

ROLLUP_RECORD_TYPE = "google-health-heart-rate-rollup"
WHERE = "heart-rate window"


def parse_google_utc_offset(value: str) -> timezone:
    """``"-18000s"`` to a fixed-offset ``timezone`` (UTC-05:00).

    A fixed offset is correct only for a time range that does not cross a daylight-saving change.
    For a sync or backfill, prefer the user's IANA zone, ``zoneinfo.ZoneInfo("America/New_York")``,
    from configuration. FHIR and ISO 8601 offsets are whole minutes, from -14:00 to +14:00, so
    anything else is rejected rather than rounded.
    """
    match = _GOOGLE_OFFSET.fullmatch(value) if isinstance(value, str) else None
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
    if not isinstance(value, str) or not _UTC_TIME.fullmatch(value):
        raise FhirInputError(
            f"{WHERE}: {name} {value!r} is not a UTC time in whole seconds "
            "like 2026-01-01T14:05:00Z"
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
    # string (proto3 JSON writes int64 as a string). Accept a plain decimal string too.
    if isinstance(value, str):
        if len(value) > 32 or not _DECIMAL.fullmatch(value):
            shown = repr(value) if len(value) <= 32 else f"{value[:12]!r}… ({len(value)} chars)"
            raise FhirInputError(
                f"{WHERE}: {name} {shown} is not a number (expected ASCII digits, e.g. '72')"
            )
        value = float(value) if "." in value else int(value)
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


@dataclass(frozen=True, kw_only=True)
class GoogleDataSource:
    """Google's ``dataSource`` for the data: platform and recording method (spike S3).

    The roll-up response carries no ``dataSource``, so there are no defaults: the caller states
    what it knows (from a listed data point, or configuration) or passes ``None``, and the
    Provenance then asserts nothing about it.
    """

    platform: str | None
    recording_method: str | None


def rollup_source(window: HeartRateWindow, data_source: GoogleDataSource) -> SourceInfo:
    """The Provenance source record for one window. Google gives windows no id, so the id is the
    window's UTC bounds, which is how ``health-sync`` would ask for the same window again."""
    return SourceInfo(
        record_type=ROLLUP_RECORD_TYPE,
        record_id=f"{window.start_utc}/{window.end_utc}",
        platform=data_source.platform,
        recording_method=data_source.recording_method,
    )


def window_status(window: HeartRateWindow, complete_before: datetime | None) -> str:
    """``final`` if the window ended at or before ``complete_before``, else ``preliminary``.

    R4 observation-status: ``preliminary`` is "an initial or interim observation: data may be
    incomplete or unverified"; ``final`` is "complete and there are no further actions needed".
    ``None`` means the caller declares every window complete.
    """
    if complete_before is None:
        return "final"
    if not isinstance(complete_before, datetime) or complete_before.utcoffset() is None:
        raise FhirInputError("complete_before must be a datetime with a time zone, or None")
    return "final" if window.end <= complete_before else "preliminary"


def _amended_starts(
    windows: list[HeartRateWindow],
    amended: Collection[datetime],
    complete_before: datetime | None,
) -> set[datetime]:
    """Validate ``amended`` against the batch; return the window starts as UTC instants.

    R4 observation-status ``amended``: "Subsequent to being Final, the observation has been
    modified". So a start named here must be a window of this batch (otherwise the caller meant
    a different batch, and silently writing ``final`` would hide that), and that window must be
    complete now (``final`` by ``complete_before``): a window still filling has not been final,
    and ``amended`` would also claim it is complete.
    """
    if isinstance(amended, str | bytes | datetime) or not isinstance(amended, Collection):
        raise FhirInputError("amended must be a collection of window start times (datetimes)")
    by_start = {window.start: window for window in windows}
    starts: set[datetime] = set()
    for start in amended:
        if not isinstance(start, datetime) or start.utcoffset() is None:
            raise FhirInputError(f"amended: {start!r} is not a datetime with a time zone")
        utc = start.astimezone(UTC)
        window = by_start.get(utc)
        if window is None:
            raise FhirInputError(
                f"amended: no window in this batch starts at {utc.isoformat()}; "
                "amended names windows of the batch being written"
            )
        if window_status(window, complete_before) != "final":
            raise FhirInputError(
                f"amended: the window starting at {utc.isoformat()} is not complete yet "
                "(it would be preliminary); R4 'amended' means modified after being final"
            )
        starts.add(utc)
    return starts


def heart_rate_resources(
    windows: Iterable[HeartRateWindow],
    *,
    patient: BuiltResource,
    device: BuiltResource,
    data_source: GoogleDataSource,
    tz: tzinfo,
    recorded: datetime,
    complete_before: datetime | None,
    amended: Collection[datetime] = (),
    assembler: str = "PulseBridge health-sync",
) -> list[BuiltResource]:
    """An Observation and its Provenance for each window, in input order.

    Status per window: ``preliminary`` if it ends after ``complete_before``, otherwise ``final``,
    or ``amended`` if its start is in ``amended`` (see :func:`heart_rate_transaction`).
    """
    windows = list(windows)
    amended_starts = _amended_starts(windows, amended, complete_before)
    out: list[BuiltResource] = []
    for window in windows:
        status = window_status(window, complete_before)
        if window.start in amended_starts:
            status = "amended"  # checked above: the window is complete, so it would be final
        observation = build_heart_rate_observation(
            window,
            patient=patient,
            device=device,
            tz=tz,
            status=status,
        )
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
    complete_before: datetime | None,
    amended: Collection[datetime] = (),
    assembler: str = "PulseBridge health-sync",
) -> dict:
    """``health-sync`` (T10): Google roll-up JSON items to one ingest Bundle.

    - Patient and Device: conditional create. They are never overwritten; the setup step owns the
      Patient record (see ``patient_setup_transaction``). ``patient`` needs only ``patient_id``.
    - An Observation (conditional update) and its Provenance (update by id) per window, so sending
      the Bundle twice creates no duplicates.
    - ``tz``: the user's IANA zone, ``zoneinfo.ZoneInfo(...)`` from configuration. Roll-up items
      carry no ``utcOffset``, so a fixed offset would put local times an hour off for windows on
      the far side of a daylight-saving change.
    - ``complete_before``: windows ending after this instant are written ``preliminary``. T10
      passes the start of the overlap it will re-request next run, so those windows are rewritten
      as ``final`` once they are complete. ``None`` writes every window ``final``.
    - ``amended`` (T33): start times (time-zone aware; compared as UTC instants) of windows to
      write with status ``amended`` instead of ``final``: windows the store already held as
      ``final`` whose values have changed since (R4: "Subsequent to being Final, the observation
      has been modified"). Each must be the start of a window in this batch, and that window
      must be complete (``final`` by ``complete_before``); anything else raises
      ``FhirInputError`` rather than being ignored. The Observation's key is unchanged, so the
      conditional update replaces the stored one.
    """
    built_patient = build_patient(patient)
    built_device = build_device(device, patient=built_patient)
    windows = [window_from_rollup(point) for point in rollup_points]
    return build_ingest_bundle(
        patient=built_patient,
        devices=[built_device],
        records=heart_rate_resources(
            windows,
            patient=built_patient,
            device=built_device,
            data_source=data_source,
            tz=tz,
            recorded=recorded,
            complete_before=complete_before,
            amended=amended,
            assembler=assembler,
        ),
    )
