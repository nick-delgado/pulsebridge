"""One sync run: Google Health roll-ups in, transaction Bundles out. Used by the scheduled route
(``POST /sync``) and by the backfill command; the two differ only in the range they pass.

Flow:

1. Read the three secrets from their mounted files (fresh every run).
2. For each requested range (at most 14 days), oldest first:
   a. ``dataPoints:rollUp`` for the range, every page.
   b. Keep the windows that carry heart-rate values; drop empty items (no data) and any window
      T06 refuses (out of range, min > avg), counted but never logged by value. Sort oldest
      first, because Google sends newest first.
   c. The first time a range has windows: find the Patient's server id and count stranded
      preliminary windows (FHIR searches). Then read what the store already holds for the
      range (:mod:`pulsebridge_gcp.stored`, shared with the seeder) and keep only the windows
      that are new or changed, or that have just become final.
   d. In batches: one raw ``dataPoints`` call for the device name, platform and recording
      method (roll-ups carry none), then T06's ``heart_rate_transaction`` and ``executeBundle``.

Why this is safe to repeat or overlap: every write is keyed. The Observation is a conditional
update on UUIDv5(patient, LOINC 8867-4, window start in UTC), the Provenance is ``PUT`` by an id
derived from that key, and the Patient and Device are conditional creates. Writing the same
window twice lands on the same resources; skipping unchanged windows only saves the rewrite.

Ranges are aligned to five-minute boundaries in UTC. The API starts its first window at the
start of the requested range ("Every bucket starts sequentially from the beginning of your
range", Google Health API guide "Aggregate data"), so an unaligned start would shift every window
and give every window a new key: the overlap would then duplicate instead of rewrite.

Time: a scheduled run must end before Cloud Scheduler's attempt deadline, or Scheduler may start
a second, overlapping run. Every request's timeout is cut to the time left, and no new range or
Bundle is started with less than ``MIN_STEP_SECONDS`` left (outcome ``deadline-reached``).
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
    build_patient,
    heart_rate_key,
    heart_rate_transaction,
    truncate_to_ms,
    window_from_rollup,
    window_status,
)
from pulsebridge_fhir.stored import Plan, StoredReadError, StoredWindow, plan_writes
from pulsebridge_gcp import (
    ClientError,
    ConflictError,
    HealthcareApiError,
    MalformedResponseError,
    NotFoundError,
    PermissionDeniedError,
    RetryableError,
    operation_outcome_summary,
)
from pulsebridge_gcp.stored import stored_windows
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
from health_sync.retry import MIN_STEP_SECONDS, Deadline, DeadlineReached, RetryPolicy
from health_sync.stored import count_stranded, find_patient_ids

logger = logging.getLogger("health_sync")

ASSEMBLER = "PulseBridge health-sync"
# Used only when the user has no raw heart-rate point with a device name at all. T06's Device
# needs a name; this one claims nothing about the hardware. See README, "Device and data source".
UNNAMED_DEVICE = "Unnamed Google Health device"

# A search is an optimisation, so it gets a short timeout of its own: a slow FHIR store must
# not spend the run's budget on reading and leave nothing for writing.
SEARCH_TIMEOUT_SECONDS = 10.0
SEARCHES_PER_RUN = 3  # Patient, stranded count, stored windows (one page)

# Where a run can stop for lack of time. The reason is "before-<code>" (README, "Deadline").
DEADLINE_STEPS = ("range", "bundle", "token-request", "google-request", "fhir-search", "fhir-write")


class Outcome(StrEnum):
    SYNCED = "synced"
    RECONNECT_NEEDED = "reconnect-needed"
    NOT_CONFIGURED = "not-configured"
    ALREADY_RUNNING = "already-running"
    GOOGLE_UNAVAILABLE = "google-unavailable"
    FHIR_UNAVAILABLE = "fhir-unavailable"
    DEADLINE_REACHED = "deadline-reached"
    GOOGLE_ERROR = "google-error"
    FHIR_REJECTED = "fhir-rejected"
    FHIR_MISCONFIGURED = "fhir-misconfigured"
    MAPPING_FAILED = "mapping-failed"


# The HTTP status the scheduled route answers with (README, "What Scheduler sees"). 200 for the
# states a retry cannot change and the status route already reports (and for a run that found
# another one in progress); 503 for transient failures; 500 for problems that need a person.
HTTP_STATUS = {
    Outcome.SYNCED: 200,
    Outcome.RECONNECT_NEEDED: 200,
    Outcome.NOT_CONFIGURED: 200,
    Outcome.ALREADY_RUNNING: 200,
    Outcome.GOOGLE_UNAVAILABLE: 503,
    Outcome.FHIR_UNAVAILABLE: 503,
    Outcome.DEADLINE_REACHED: 503,
    Outcome.GOOGLE_ERROR: 500,
    Outcome.FHIR_REJECTED: 500,
    Outcome.FHIR_MISCONFIGURED: 500,
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


class FhirClient:
    """What the run needs from :class:`pulsebridge_gcp.HealthcareClient`."""

    def execute_bundle(self, fhir_store: str, bundle: dict) -> dict: ...  # pragma: no cover
    def search(
        self, fhir_store: str, resource_type: str, params: object
    ) -> dict: ...  # pragma: no cover
    def next_page(self, fhir_store: str, bundle: dict) -> dict | None: ...  # pragma: no cover


# A FHIR client whose requests time out after the given number of seconds. A new client is
# made for each request so its timeout can be cut to the time the run has left (the shared
# client's timeout is fixed when it is built).
FhirFactory = Callable[[float], FhirClient]


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
    windows_unchanged: int = 0
    windows_new: int = 0
    windows_changed: int = 0
    windows_finalised: int = 0
    windows_amended: int = 0
    windows_withdrawn_kept: int = 0
    windows_held_final: int = 0
    windows_written: int = 0
    windows_final: int = 0
    windows_preliminary: int = 0
    bundles_written: int = 0
    ranges_completed: int = 0
    # "not-attempted", "ok", "patient-not-found", "patients-duplicated", "skipped-for-time",
    # "failed:<status or error type>"
    stored_read: str = "not-attempted"
    stranded_preliminary: int | None = None
    google_statuses: list[int] = field(default_factory=list)
    fhir_status: int | None = None
    fhir_issues: list[dict] = field(default_factory=list)
    token_refreshes: int = 0
    refresh_token_rotated: bool = False
    scope_not_listed: bool = False
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
            "windows_unchanged": self.windows_unchanged,
            "windows_new": self.windows_new,
            "windows_changed": self.windows_changed,
            "windows_finalised": self.windows_finalised,
            "windows_amended": self.windows_amended,
            "windows_withdrawn_kept": self.windows_withdrawn_kept,
            "windows_held_final": self.windows_held_final,
            "windows_written": self.windows_written,
            "windows_final": self.windows_final,
            "windows_preliminary": self.windows_preliminary,
            "bundles_written": self.bundles_written,
            "stored_read": self.stored_read,
            "stranded_preliminary": self.stranded_preliminary,
            "google_statuses": sorted(set(self.google_statuses)),
            "fhir_status": self.fhir_status,
            "fhir_issues": self.fhir_issues,
            "token_refreshes": self.token_refreshes,
            "refresh_token_rotated": self.refresh_token_rotated,
            "scope_not_listed": self.scope_not_listed,
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


def _fhir(
    deps: SyncDependencies,
    config: ServiceConfig,
    deadline: Deadline,
    where: str,
    cap: float | None = None,
):
    """A FHIR client whose timeout is ``cap`` (default ``FHIR_TIMEOUT_SECONDS``), cut to the
    time the run has left."""
    timeout = config.fhir_timeout if cap is None else min(cap, config.fhir_timeout)
    return deps.fhir(deadline.timeout_for(timeout, where))


def _search_client(run: _Run):
    return _fhir(run.deps, run.config, run.deadline, "fhir-search", SEARCH_TIMEOUT_SECONDS)


def write_bundle(
    deps: SyncDependencies,
    config: ServiceConfig,
    bundle: dict,
    *,
    policy: RetryPolicy,
    deadline: Deadline,
) -> dict:
    """``executeBundle`` with the same bounded retry as Google calls, for 429/5xx/network
    (``RetryableError``) and 409 (two overlapping runs updating one Observation at once). A
    transaction is all-or-nothing, so a failed attempt wrote nothing and resending is safe.
    Each attempt's timeout is cut to the time the run has left."""
    attempt = 0
    while True:
        attempt += 1
        client = _fhir(deps, config, deadline, "fhir-write")
        try:
            return client.execute_bundle(config.fhir_store_id, bundle)
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
    fhir: FhirFactory
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass
class _Run:
    """Everything one run shares between its ranges and batches."""

    config: ServiceConfig
    deps: SyncDependencies
    report: RunReport
    google: GoogleHealthClient
    policy: RetryPolicy
    deadline: Deadline
    patient: PatientInput
    patient_key: str
    earliest: datetime | None = None  # the earliest range start, for the stranded count
    patient_ref: str | None = None  # "Patient/<id>" when the store has the Patient
    patient_read: bool = False

    def key_of(self, item: dict) -> tuple[str, datetime, tuple]:
        window = window_from_rollup(item)
        values = (window.bpm_min, window.bpm_avg, window.bpm_max)
        return heart_rate_key(self.patient_key, window), window.end, values

    def status_of(self, item: dict) -> str:
        return window_status(window_from_rollup(item), self.report.complete_before)


def run_sync(
    config: ServiceConfig,
    deps: SyncDependencies,
    *,
    ranges: list[TimeRange],
    deadline_seconds: float | None,
) -> RunReport:
    """Sync every range in order (each at most 14 days). Never raises for an expected failure:
    the outcome says what happened, and the ranges and batches before a failure stay written."""
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
            raise _Stop(Outcome.NOT_CONFIGURED, exc.reason) from None
        http = HttpSender(deps.google_transport, policy, deadline, config.http_timeout)
        tokens = TokenSource(credentials, scope=config.scope, http=http, clock=deps.monotonic)
        google = GoogleHealthClient(tokens, http)
        patient = PatientInput(config.patient_id)
        run = _Run(
            config, deps, report, google, policy, deadline, patient, build_patient(patient).key
        )
        run.earliest = min((r.start for r in ranges), default=None)
        for piece in ranges:
            deadline.require(MIN_STEP_SECONDS, "range")
            _sync_one(run, piece)
            report.ranges_completed += 1
    except _Stop as stop:
        report.outcome, report.reason, report.fhir_status = stop.outcome, stop.reason, stop.status
    except DeadlineReached as exc:
        report.outcome, report.reason = Outcome.DEADLINE_REACHED, f"before-{exc.where}"
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
            report.scope_not_listed = tokens.scope_not_listed
        report.duration_ms = int((deps.monotonic() - started) * 1000)
    return report


def _google_reason(exc: GoogleHealthError) -> str:
    operation = exc.operation.split(" ", 1)[-1]
    status = exc.status if exc.status is not None else "none"
    return f"{operation}:{exc.code}:{status}"


def _read_failed(report: RunReport, exc: Exception) -> None:
    status = getattr(exc, "status", None)
    report.stored_read = f"failed:{status if status is not None else type(exc).__name__}"
    logger.warning(
        "stored windows could not be read; writing every window",
        extra={"fields": {"event": "stored-read-failed", "http_status": status}},
    )


def _reads_affordable(run: _Run, windows: int) -> bool:
    """Whether the searches fit in the time left together with the writes they would save:
    each search at most ``SEARCH_TIMEOUT_SECONDS``, and each Bundle one data-source call and one
    ``executeBundle`` at their full timeouts. If not, the run skips the reads and writes
    everything: writing never waits for reading."""
    remaining = run.deadline.remaining()
    if remaining is None:
        return True
    bundles = -(-windows // run.config.batch_windows)
    needed = SEARCHES_PER_RUN * SEARCH_TIMEOUT_SECONDS + bundles * (
        run.config.http_timeout + run.config.fhir_timeout
    )
    return remaining >= needed


def _read_patient(run: _Run, earliest: datetime | None) -> None:
    """The Patient's server id, and the stranded-preliminary count. A failure is logged and
    the run goes on, writing everything (reading is only an optimisation)."""
    report = run.report
    try:
        ids = find_patient_ids(_search_client(run), run.config.fhir_store_id, run.config.patient_id)
    except (HealthcareApiError, StoredReadError, ValueError) as exc:
        _read_failed(report, exc)
        return
    if not ids:
        report.stored_read = "patient-not-found"
        report.stranded_preliminary = 0  # no Patient, so nothing of theirs is stored
        return
    if len(ids) > 1:
        report.stored_read = "patients-duplicated"
        logger.warning(
            "more than one Patient has the configured identifier; writing every window",
            extra={"fields": {"event": "patients-duplicated", "patients": len(ids)}},
        )
        return
    run.patient_ref = f"Patient/{ids[0]}"
    report.stored_read = "ok"
    if earliest is None:
        return
    try:
        report.stranded_preliminary = count_stranded(
            _search_client(run), run.config.fhir_store_id, run.patient_ref, earliest
        )
    except (HealthcareApiError, StoredReadError, ValueError) as exc:
        # Only the count is lost: the stored-window read and the writes go on.
        logger.warning(
            "stranded preliminary windows could not be counted",
            extra={
                "fields": {
                    "event": "stranded-count-failed",
                    "http_status": getattr(exc, "status", None),
                }
            },
        )
        return
    if report.stranded_preliminary:
        logger.warning(
            "preliminary windows older than the range will not be finalised; run a backfill",
            extra={
                "fields": {
                    "event": "stranded-preliminary",
                    "stranded_preliminary": report.stranded_preliminary,
                }
            },
        )


def _skip_reads(report: RunReport) -> None:
    report.stored_read = "skipped-for-time"
    logger.warning(
        "too little time to read the store and write; writing every window",
        extra={"fields": {"event": "stored-read-skipped"}},
    )


def _stored_for(run: _Run, piece: TimeRange, windows: int) -> dict[str, StoredWindow] | None:
    if run.patient_ref is None:
        # No Patient in the store: nothing of theirs can be stored. Otherwise the read was
        # skipped, failed or found duplicate Patients: write everything.
        return {} if run.report.stored_read == "patient-not-found" else None
    if not _reads_affordable(run, windows):
        _skip_reads(run.report)
        return None
    try:
        return stored_windows(
            lambda: _search_client(run),
            run.config.fhir_store_id,
            run.patient_ref,
            piece.start,
            piece.end,
            logger=logger,  # max_pages: the library default, MAX_SEARCH_PAGES (20)
        )
    except (HealthcareApiError, StoredReadError, ValueError) as exc:
        _read_failed(run.report, exc)
        return None


def _data_source(run: _Run, start: datetime, end: datetime) -> DataSourceInfo:
    """The newest raw point in ``[start, end)``; when that gives no device name, the newest
    point before ``start``, so the Device keeps the name earlier runs used rather than becoming
    "unnamed" for one batch (README, "Device and data source")."""
    source = run.google.latest_data_source(start, end)
    if source.device_name is not None:
        return source
    earlier = run.google.latest_data_source(None, start)
    return DataSourceInfo(
        device_name=earlier.device_name,
        platform=source.platform or earlier.platform,
        recording_method=source.recording_method or earlier.recording_method,
    )


def _device_input(source: DataSourceInfo) -> DeviceInput:
    if source.device_name is not None:
        try:
            return DeviceInput(source.device_name)
        except FhirInputError:
            pass
    return DeviceInput(UNNAMED_DEVICE)


def _apply(report: RunReport, plan: Plan) -> None:
    report.windows_unchanged += plan.unchanged
    report.windows_new += plan.new
    report.windows_changed += plan.changed
    report.windows_finalised += plan.finalised
    report.windows_amended += plan.amended
    report.windows_withdrawn_kept += plan.withdrawn_kept
    report.windows_held_final += plan.held_final


def _sync_one(run: _Run, piece: TimeRange) -> None:
    report, config = run.report, run.config
    rollup = run.google.rollup(piece.start, piece.end)
    report.pages += rollup.pages
    report.windows_fetched += len(rollup.items)
    clean = clean_windows(rollup.items)
    report.windows_empty += clean.empty
    report.windows_invalid += clean.invalid
    if not clean.items:
        return  # no data in this range: nothing to write, and no Patient or Device either
    if not run.patient_read:
        # Once per run, and only when there is something to write: a run that ends at the
        # token refresh or finds no data does not touch the FHIR store at all.
        run.patient_read = True
        if _reads_affordable(run, len(clean.items)):
            _read_patient(run, run.earliest)
        else:
            _skip_reads(report)
    plan = plan_writes(
        clean.items,
        _stored_for(run, piece, len(clean.items)),
        key_of=run.key_of,
        status_of=run.status_of,
    )
    _apply(report, plan)
    for batch in batches(plan.write, config.batch_windows):
        run.deadline.require(MIN_STEP_SECONDS, "bundle")
        first, last = window_from_rollup(batch[0]), window_from_rollup(batch[-1])
        source = _data_source(run, first.start, last.end)
        # Stored final, values changed since: written "amended", not "final" (T33).
        amended = [
            window_from_rollup(item).start
            for item in batch
            if run.key_of(item)[0] in plan.amend_keys
        ]
        try:
            bundle = heart_rate_transaction(
                batch,
                patient=run.patient,
                device=_device_input(source),
                data_source=GoogleDataSource(
                    platform=source.platform, recording_method=source.recording_method
                ),
                tz=config.time_zone,
                recorded=truncate_to_ms(run.deps.now()),
                complete_before=report.complete_before,
                amended=amended,
                assembler=ASSEMBLER,
            )
        except FhirInputError:
            # The windows were validated one by one above, so this is a bug; the message can
            # quote a value, so only the type is kept.
            raise _Stop(Outcome.MAPPING_FAILED, "fhir-input-error") from None
        try:
            response = write_bundle(
                run.deps, config, bundle, policy=run.policy, deadline=run.deadline
            )
        except MalformedResponseError as exc:
            # A 2xx whose body is not a Bundle: the write probably happened, but the store's
            # contract changed. A person should look; the next run rewrites the same keys.
            raise _Stop(Outcome.FHIR_REJECTED, "fhir-malformed-response", exc.status) from None
        except PermissionDeniedError as exc:
            # 401/403: the service account lacks the role (or it is still propagating after an
            # apply). Not something the next run fixes by itself, so a person is alerted.
            raise _Stop(Outcome.FHIR_MISCONFIGURED, "fhir-permission-denied", exc.status) from None
        except NotFoundError as exc:
            raise _Stop(Outcome.FHIR_MISCONFIGURED, "fhir-store-missing", exc.status) from None
        except ClientError as exc:
            if isinstance(exc, ConflictError):
                raise _Stop(Outcome.FHIR_UNAVAILABLE, "fhir-conflict", exc.status) from None
            report.fhir_issues = operation_outcome_summary(exc.operation_outcome)
            raise _Stop(Outcome.FHIR_REJECTED, "fhir-refused-bundle", exc.status) from None
        except HealthcareApiError as exc:
            # 429/5xx/network after the retries: nothing in this Bundle was written; the next
            # run retries.
            raise _Stop(Outcome.FHIR_UNAVAILABLE, "fhir-write-failed", exc.status) from None
        _record_entry_statuses(report, bundle, response)
        report.bundles_written += 1
        report.windows_written += len(batch)
        for entry in bundle["entry"]:
            resource = entry["resource"]
            if resource["resourceType"] == "Observation":
                # final or amended: complete windows (amended ones are also in windows_amended).
                if resource["status"] == "preliminary":
                    report.windows_preliminary += 1
                else:
                    report.windows_final += 1


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
