"""One sync run: Google Health roll-ups in, transaction Bundles out. Used by the scheduled route
(``POST /sync``) and by the backfill command; the two differ only in the range they pass.

Flow for each requested range (at most 14 days):

1. Read the three secrets from their mounted files (fresh every run).
2. ``dataPoints:rollUp`` for the range, every page.
3. Keep the windows that carry heart-rate values; drop empty items (no data) and any window
   T06 refuses (out of range, min > avg), counted but never logged by value. Sort oldest first,
   because Google sends newest first and batches should be stable runs of time.
4. One raw ``dataPoints`` call (``pageSize=1``) for the device name, platform and recording
   method, which roll-ups do not carry.
5. Map each batch with T06's ``heart_rate_transaction`` and write it with ``executeBundle``.

Why this is safe to repeat or overlap: every write is keyed. The Observation is a conditional
update on UUIDv5(patient, LOINC 8867-4, window start in UTC), the Provenance is ``PUT`` by an id
derived from that key, and the Patient and Device are conditional creates. Writing the same
window twice lands on the same resources; the store's version history records each rewrite.

Ranges are aligned to five-minute boundaries in UTC. The API starts its first window at the
start of the requested range ("Every bucket starts sequentially from the beginning of your
range", Google Health API guide "Aggregate data"), so an unaligned start would shift every window
and give every window a new key: the overlap would then duplicate instead of rewrite.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pulsebridge_fhir import (
    DeviceInput,
    FhirInputError,
    GoogleDataSource,
    PatientInput,
    heart_rate_transaction,
    truncate_to_ms,
    window_from_rollup,
)
from pulsebridge_gcp import (
    ClientError,
    ConflictError,
    HealthcareApiError,
    MalformedResponseError,
    RetryableError,
    operation_outcome_summary,
)
from pulsebridge_gcp.transport import Transport

from health_sync.config import MAX_ROLLUP_RANGE, WINDOW, ServiceConfig
from health_sync.credentials import SecretsMissing, load_credentials
from health_sync.google_health import (
    DataSourceInfo,
    GoogleHealthClient,
    GoogleHealthError,
    GoogleHealthRejected,
    GoogleHealthUnavailable,
    HttpSender,
    ReconnectNeeded,
    TokenSource,
)
from health_sync.retry import Deadline, RetryPolicy

logger = logging.getLogger("health_sync")

ASSEMBLER = "PulseBridge health-sync"
# Used only when Google reports no displayName for the device. T06's Device needs a name; this
# one claims nothing about the hardware. See README, "Device and data source".
UNNAMED_DEVICE = "Unnamed Google Health device"


class Outcome(StrEnum):
    SYNCED = "synced"
    RECONNECT_NEEDED = "reconnect-needed"
    NOT_CONFIGURED = "not-configured"
    GOOGLE_UNAVAILABLE = "google-unavailable"
    GOOGLE_ERROR = "google-error"
    FHIR_UNAVAILABLE = "fhir-unavailable"
    FHIR_REJECTED = "fhir-rejected"
    MAPPING_FAILED = "mapping-failed"


# The HTTP status the scheduled route answers with (README, "What Scheduler sees"). 200 for the
# states a retry cannot change and the status route already reports; 503 for transient failures;
# 500 for refusals that need a person.
HTTP_STATUS = {
    Outcome.SYNCED: 200,
    Outcome.RECONNECT_NEEDED: 200,
    Outcome.NOT_CONFIGURED: 200,
    Outcome.GOOGLE_UNAVAILABLE: 503,
    Outcome.FHIR_UNAVAILABLE: 503,
    Outcome.GOOGLE_ERROR: 500,
    Outcome.FHIR_REJECTED: 500,
    Outcome.MAPPING_FAILED: 500,
}


# ---- time ranges ------------------------------------------------------------------------------


def floor_to_window(value: datetime) -> datetime:
    """Round down to a five-minute boundary in UTC (whole seconds)."""
    value = value.astimezone(UTC).replace(microsecond=0)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    seconds = int((value - epoch).total_seconds())
    return epoch + timedelta(seconds=seconds - seconds % int(WINDOW.total_seconds()))


@dataclass(frozen=True)
class TimeRange:
    start: datetime  # inclusive
    end: datetime  # exclusive

    def as_log(self) -> dict:
        return {"start": _utc(self.start), "end": _utc(self.end)}


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_range(now: datetime, lookback: timedelta) -> TimeRange:
    """``[floor(now) - lookback, floor(now))``: the last ``lookback`` of complete-in-time windows.
    The window still in progress is left for the next run."""
    end = floor_to_window(now)
    return TimeRange(end - lookback, end)


def backfill_range(now: datetime, days: int) -> TimeRange:
    """The last ``days`` × 24 hours, ending at the same boundary as a sync would."""
    end = floor_to_window(now)
    return TimeRange(end - timedelta(days=days), end)


def split_range(whole: TimeRange, limit: timedelta = MAX_ROLLUP_RANGE) -> list[TimeRange]:
    """Consecutive pieces of at most ``limit``, oldest first.

    Physical time, not calendar days: the API's 14-day limit is a duration of the UTC range, so
    a piece is 336 hours even when it crosses a daylight-saving change in the user's zone (a
    local "14 days" across the autumn change would be 337 hours and be refused)."""
    pieces = []
    start = whole.start
    while start < whole.end:
        end = min(start + limit, whole.end)
        pieces.append(TimeRange(start, end))
        start = end
    return pieces


def completion_cutoff(now: datetime, final_after: timedelta) -> datetime:
    """``complete_before`` for T06: windows that ended after this are written ``preliminary``.

    A window is final once it ended ``FINAL_AFTER`` ago (default 60 minutes): late samples from
    the phone have had that long to arrive. Every earlier window is re-requested by at least one
    later run while it is still preliminary, because ``LOOKBACK >= FINAL_AFTER + SYNC_INTERVAL``
    (checked at start-up). With the defaults any run from 60 to 175 minutes after a window
    ended finalises it, so about seven consecutive missed runs are tolerated (README,
    "Overlap and status")."""
    return floor_to_window(now - final_after)


# ---- windows ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanWindows:
    items: list[dict]  # oldest first, one per start time
    empty: int  # items with no heart-rate value: no data in that window
    invalid: int  # refused by T06's checks, or a second item for the same start


def clean_windows(items: Sequence[object]) -> CleanWindows:
    """Validate each roll-up item with T06's parser, keep one per start, sort oldest first."""
    by_start: dict[datetime, dict] = {}
    empty = invalid = 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("heartRate"), dict):
            empty += 1
            continue
        try:
            window = window_from_rollup(item)
        except FhirInputError:
            invalid += 1  # never logged: its message can quote the value
            continue
        if window.start in by_start:
            invalid += 1
            continue
        by_start[window.start] = item
    return CleanWindows([by_start[start] for start in sorted(by_start)], empty, invalid)


def batches(items: list[dict], size: int) -> Iterator[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


# ---- the run ----------------------------------------------------------------------------------


class FhirClient:
    """What the run needs from :class:`pulsebridge_gcp.HealthcareClient`."""

    def execute_bundle(self, fhir_store: str, bundle: dict) -> dict: ...  # pragma: no cover


@dataclass
class RunReport:
    """What one run did. Counts, times, codes and statuses only: no values, tokens or ids."""

    outcome: Outcome = Outcome.SYNCED
    reason: str | None = None
    ranges: list[TimeRange] = field(default_factory=list)
    complete_before: datetime | None = None
    pages: int = 0
    windows_fetched: int = 0
    windows_empty: int = 0
    windows_invalid: int = 0
    windows_written: int = 0
    windows_final: int = 0
    windows_preliminary: int = 0
    bundles_written: int = 0
    ranges_completed: int = 0
    google_statuses: list[int] = field(default_factory=list)
    fhir_status: int | None = None
    fhir_issues: list[dict] = field(default_factory=list)
    token_refreshes: int = 0
    refresh_token_rotated: bool = False
    entry_statuses: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.outcome]

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "ranges": [r.as_log() for r in self.ranges],
            "ranges_completed": self.ranges_completed,
            "complete_before": _utc(self.complete_before) if self.complete_before else None,
            "pages": self.pages,
            "windows_fetched": self.windows_fetched,
            "windows_empty": self.windows_empty,
            "windows_invalid": self.windows_invalid,
            "windows_written": self.windows_written,
            "windows_final": self.windows_final,
            "windows_preliminary": self.windows_preliminary,
            "bundles_written": self.bundles_written,
            "google_statuses": sorted(set(self.google_statuses)),
            "fhir_status": self.fhir_status,
            "fhir_issues": self.fhir_issues,
            "token_refreshes": self.token_refreshes,
            "refresh_token_rotated": self.refresh_token_rotated,
            "entry_statuses": dict(sorted(self.entry_statuses.items())),
            "duration_ms": self.duration_ms,
        }


class _Stop(Exception):  # noqa: N818 - internal control flow, not an error type
    """Ends the run with an outcome the caller already classified."""

    def __init__(self, outcome: Outcome, reason: str | None, status: int | None = None) -> None:
        super().__init__(outcome.value)
        self.outcome, self.reason, self.status = outcome, reason, status


def _record_entry_statuses(report: RunReport, bundle: dict, response: dict) -> None:
    """Count response statuses by resource type, e.g. ``Patient 200`` (the conditional create
    matched an existing Patient) or ``Patient 201`` (it created one). Entries answer in request
    order (R4 http.html#transaction-response). This is how the first real write shows whether
    the store matched the setup step's Patient (T06 README, "Unverified server behaviour")."""
    answers = response.get("entry") if isinstance(response, dict) else None
    if not isinstance(answers, list):
        return
    for sent, answer in zip(bundle["entry"], answers, strict=False):
        result = answer.get("response") if isinstance(answer, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        code = status.split(" ", 1)[0] if isinstance(status, str) else "?"
        if not code.isdigit():
            code = "?"
        key = f"{sent['resource']['resourceType']} {code}"
        report.entry_statuses[key] = report.entry_statuses.get(key, 0) + 1


def write_bundle(
    fhir: FhirClient,
    store: str,
    bundle: dict,
    *,
    policy: RetryPolicy,
    deadline: Deadline,
) -> dict:
    """``executeBundle`` with the same bounded retry as Google calls, for 429/5xx/network
    (``RetryableError``) and 409 (two overlapping runs updating one Observation at once). A
    transaction is all-or-nothing, so a failed attempt wrote nothing and resending is safe."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fhir.execute_bundle(store, bundle)
        except (RetryableError, ConflictError) as exc:
            delay = policy.delay(attempt, None)
            if attempt >= policy.max_attempts or not deadline.allows(delay):
                raise
            logger.warning(
                "fhir write retried",
                extra={
                    "fields": {
                        "event": "fhir-retry",
                        "http_status": exc.status,
                        "attempt": attempt,
                        "delay_seconds": delay,
                    }
                },
            )
            policy.sleep(delay)


@dataclass
class SyncDependencies:
    google_transport: Transport
    fhir: FhirClient
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def _device_input(source: DataSourceInfo) -> DeviceInput:
    if source.device_name is not None:
        try:
            return DeviceInput(source.device_name)
        except FhirInputError:
            pass
    return DeviceInput(UNNAMED_DEVICE)


def run_sync(
    config: ServiceConfig,
    deps: SyncDependencies,
    *,
    ranges: list[TimeRange],
    deadline_seconds: float | None,
) -> RunReport:
    """Sync every range in order (each at most 14 days). Never raises for an expected failure:
    the outcome says what happened, and the ranges before a failure stay written."""
    started = deps.monotonic()
    now = deps.now()
    report = RunReport(ranges=ranges, complete_before=completion_cutoff(now, config.final_after))
    deadline = Deadline(deadline_seconds, clock=deps.monotonic)
    policy = RetryPolicy(max_attempts=config.max_attempts, sleep=deps.sleep)
    tokens: TokenSource | None = None
    google: GoogleHealthClient | None = None
    try:
        try:
            credentials = load_credentials(config.secrets)
        except SecretsMissing as exc:
            raise _Stop(Outcome.NOT_CONFIGURED, "secrets-missing:" + ",".join(exc.names)) from None
        http = HttpSender(deps.google_transport, policy, deadline, config.http_timeout)
        tokens = TokenSource(credentials, scope=config.scope, http=http, clock=deps.monotonic)
        google = GoogleHealthClient(tokens, http)
        for piece in ranges:
            _sync_one(config, deps, report, google, piece, policy, deadline)
            report.ranges_completed += 1
    except _Stop as stop:
        report.outcome, report.reason, report.fhir_status = stop.outcome, stop.reason, stop.status
    except ReconnectNeeded as exc:
        report.outcome, report.reason = Outcome.RECONNECT_NEEDED, exc.code
    except GoogleHealthUnavailable as exc:
        report.outcome, report.reason = Outcome.GOOGLE_UNAVAILABLE, _google_reason(exc)
    except GoogleHealthRejected as exc:
        report.outcome, report.reason = Outcome.GOOGLE_ERROR, _google_reason(exc)
    finally:
        if google is not None:
            report.google_statuses = list(google.statuses)
        if tokens is not None:
            report.token_refreshes = tokens.refreshes
            report.refresh_token_rotated = tokens.rotated_refresh_token_ignored
        report.duration_ms = int((deps.monotonic() - started) * 1000)
    return report


def _google_reason(exc: GoogleHealthError) -> str:
    operation = exc.operation.split(" ", 1)[-1]
    status = exc.status if exc.status is not None else "none"
    return f"{operation}:{exc.code}:{status}"


def _sync_one(
    config: ServiceConfig,
    deps: SyncDependencies,
    report: RunReport,
    google: GoogleHealthClient,
    piece: TimeRange,
    policy: RetryPolicy,
    deadline: Deadline,
) -> None:
    rollup = google.rollup(piece.start, piece.end)
    report.pages += rollup.pages
    report.windows_fetched += len(rollup.items)
    clean = clean_windows(rollup.items)
    report.windows_empty += clean.empty
    report.windows_invalid += clean.invalid
    if not clean.items:
        return  # no data in this range: nothing to write, and no Patient or Device either
    source = google.latest_data_source(piece.start, piece.end)
    patient = PatientInput(config.patient_id)
    device = _device_input(source)
    data_source = GoogleDataSource(
        platform=source.platform, recording_method=source.recording_method
    )
    for batch in batches(clean.items, config.batch_windows):
        try:
            bundle = heart_rate_transaction(
                batch,
                patient=patient,
                device=device,
                data_source=data_source,
                tz=config.time_zone,
                recorded=truncate_to_ms(deps.now()),
                complete_before=report.complete_before,
                assembler=ASSEMBLER,
            )
        except FhirInputError:
            # The windows were validated one by one above, so this is a bug; the message can
            # quote a value, so only the type is kept.
            raise _Stop(Outcome.MAPPING_FAILED, "fhir-input-error") from None
        try:
            response = write_bundle(
                deps.fhir, config.fhir_store_id, bundle, policy=policy, deadline=deadline
            )
        except MalformedResponseError as exc:
            # A 2xx whose body is not a Bundle: the write probably happened, but the store's
            # contract changed. A person should look; the next run rewrites the same keys.
            raise _Stop(Outcome.FHIR_REJECTED, "fhir-malformed-response", exc.status) from None
        except ClientError as exc:
            if isinstance(exc, ConflictError):
                raise _Stop(Outcome.FHIR_UNAVAILABLE, "fhir-conflict", exc.status) from None
            report.fhir_issues = operation_outcome_summary(exc.operation_outcome)
            raise _Stop(Outcome.FHIR_REJECTED, "fhir-refused-bundle", exc.status) from None
        except HealthcareApiError as exc:
            # 429/5xx/network after the retries, 401/403 (IAM, possibly still propagating),
            # 404 (store missing): nothing in this Bundle was written; the next run retries.
            raise _Stop(Outcome.FHIR_UNAVAILABLE, "fhir-write-failed", exc.status) from None
        _record_entry_statuses(report, bundle, response)
        report.bundles_written += 1
        report.windows_written += len(batch)
        for entry in bundle["entry"]:
            resource = entry["resource"]
            if resource["resourceType"] == "Observation":
                if resource["status"] == "final":
                    report.windows_final += 1
                else:
                    report.windows_preliminary += 1


__all__ = [
    "HTTP_STATUS",
    "Outcome",
    "RunReport",
    "SyncDependencies",
    "TimeRange",
    "backfill_range",
    "batches",
    "clean_windows",
    "completion_cutoff",
    "floor_to_window",
    "run_sync",
    "split_range",
    "sync_range",
    "write_bundle",
]
