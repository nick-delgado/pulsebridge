"""One seeder run.

Flow (the code below runs in this order):

1. Work out the range: ``[until - length, until)``, both on five-minute boundaries in UTC (the
   same alignment health-sync uses, so keys repeat across runs). ``until`` defaults to now and
   may not be later than now.
2. Generate the windows (:mod:`pulsebridge_seed.generator`): pure, no I/O.
3. ``--dry-run`` stops here and reports counts.
4. **Safety check**, one FHIR search: ``Patient?identifier=urn:pulsebridge:patient|<id>``. It
   fails closed: an answer that is not a ``searchset`` Bundle, or that says more Patients match
   than it returned, stops the run before any write. A Patient the seeder may not write over
   (:func:`pulsebridge_seed.synthetic.is_synthetic_patient`) refuses the run, unless ``--force``.
5. **Setup Bundle**, only when the store has no Patient yet or an incomplete one (decision D11:
   the full Patient is written once). T06's ``patient_setup_transaction``: the full synthetic
   Patient and the synthetic Device as conditional updates (``PUT Patient?identifier=…``).
6. **Read once** (:mod:`pulsebridge_seed.stored`): one search for the heart-rate windows already
   stored for the range; windows stored with the same values and a status that needs no change
   are not written again. If the search fails, every window is written.
7. **Ingest Bundles**, ``BATCH_WINDOWS`` windows each: T06's ``heart_rate_transaction`` (Patient
   and Device as conditional creates, each Observation a conditional update keyed on patient,
   LOINC 8867-4 and window start, each Provenance a ``PUT`` by an id derived from that key).
   Windows that ended after ``floor(now - FINAL_AFTER)`` are ``preliminary``, the rest ``final``:
   health-sync's cutoff rule on the real clock, so the dashboard's provisional display is
   exercised too.

Why a re-run does not duplicate: every write is keyed, and the values are a function of the
seed, the zone and the window start, so the same window is the same Observation with the same
content. A window is written at most twice in normal use: ``preliminary`` when it first appears
within ``FINAL_AFTER`` of now, then ``final`` once.

No deadline: this runs as a one-off command or a Cloud Run job, not behind Cloud Scheduler's
attempt deadline. Each FHIR request has its own timeout and a bounded number of retries.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pulsebridge_fhir import (
    PatientInput,
    build_patient,
    codes,
    heart_rate_key,
    heart_rate_transaction,
    patient_setup_transaction,
    truncate_to_ms,
    window_from_rollup,
    window_status,
)
from pulsebridge_fhir.keys import escape_search_value
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

from pulsebridge_seed import synthetic
from pulsebridge_seed.config import SeedConfig
from pulsebridge_seed.generator import floor_to_window, generate
from pulsebridge_seed.stored import StoredReadError, StoredWindow, plan_writes, stored_windows

logger = logging.getLogger("pulsebridge_seed")

BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 16.0
SAFETY_SEARCH_COUNT = 10


class Outcome(StrEnum):
    SEEDED = "seeded"
    DRY_RUN = "dry-run"
    REFUSED = "refused"  # the safety check found a Patient the seeder may not write over
    FHIR_UNAVAILABLE = "fhir-unavailable"  # 429/5xx/409/network after the retries
    FHIR_MISCONFIGURED = "fhir-misconfigured"  # 401/403/404
    FHIR_REJECTED = "fhir-rejected"  # a 4xx refusal, or a 2xx answer that cannot be used
    BUG = "bug"  # an unexpected exception; only its type is kept


class FhirClient:
    """What the run needs from :class:`pulsebridge_gcp.HealthcareClient`."""

    def execute_bundle(self, fhir_store: str, bundle: dict) -> dict: ...  # pragma: no cover
    def search(
        self, fhir_store: str, resource_type: str, params: object
    ) -> dict: ...  # pragma: no cover
    def next_page(self, fhir_store: str, bundle: dict) -> dict | None: ...  # pragma: no cover


@dataclass(frozen=True)
class Range:
    start: datetime  # inclusive, UTC, aligned
    end: datetime  # exclusive, UTC, aligned


def seed_range(until: datetime, length: timedelta) -> Range:
    """``[floor(until) - length, floor(until))``. ``length`` must be whole windows."""
    if length <= timedelta(0) or length % timedelta(minutes=5):
        raise ValueError("length must be a positive multiple of five minutes")
    end = floor_to_window(until)
    return Range(end - length, end)


def completion_cutoff(now: datetime, final_after: timedelta) -> datetime:
    """health-sync's rule: a window is ``final`` once it ended ``FINAL_AFTER`` ago."""
    return floor_to_window(now - final_after)


@dataclass
class Report:
    """What a run did. Counts, UTC bounds and codes only: no values, no patient identifier."""

    outcome: Outcome = Outcome.SEEDED
    reason: str | None = None
    range_start: datetime | None = None
    range_end: datetime | None = None
    complete_before: datetime | None = None
    windows_in_range: int = 0
    windows_generated: int = 0
    windows_absent: int = 0
    windows_unchanged: int = 0
    windows_new: int = 0
    windows_changed: int = 0
    windows_finalised: int = 0
    windows_withdrawn_kept: int = 0
    windows_written: int = 0
    windows_final: int = 0
    windows_preliminary: int = 0
    bundles_planned: int = 0
    bundles_written: int = 0
    setup_written: bool = False
    # not-run, no-patient, synthetic-patient (complete), synthetic-incomplete, forced, refused
    safety_check: str = "not-run"
    # not-attempted, ok, patient-not-found, skipped-forced, failed:<status or error type>
    stored_read: str = "not-attempted"
    fhir_status: int | None = None
    fhir_issues: list[dict] = field(default_factory=list)
    entry_statuses: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        def utc(value: datetime | None) -> str | None:
            return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None

        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "range": {"start": utc(self.range_start), "end": utc(self.range_end)},
            "complete_before": utc(self.complete_before),
            "windows_in_range": self.windows_in_range,
            "windows_generated": self.windows_generated,
            "windows_absent": self.windows_absent,
            "windows_unchanged": self.windows_unchanged,
            "windows_new": self.windows_new,
            "windows_changed": self.windows_changed,
            "windows_finalised": self.windows_finalised,
            "windows_withdrawn_kept": self.windows_withdrawn_kept,
            "windows_written": self.windows_written,
            "windows_final": self.windows_final,
            "windows_preliminary": self.windows_preliminary,
            "bundles_planned": self.bundles_planned,
            "bundles_written": self.bundles_written,
            "setup_written": self.setup_written,
            "safety_check": self.safety_check,
            "stored_read": self.stored_read,
            "fhir_status": self.fhir_status,
            "fhir_issues": self.fhir_issues,
            "entry_statuses": dict(sorted(self.entry_statuses.items())),
        }


@dataclass
class Dependencies:
    fhir: FhirClient | None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep


class _Stop(Exception):  # noqa: N818 - internal control flow
    def __init__(self, outcome: Outcome, reason: str, status: int | None = None) -> None:
        super().__init__(outcome.value)
        self.outcome, self.reason, self.status = outcome, reason, status


def batches(items: list[dict], size: int) -> Iterator[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def setup_bundle(config: SeedConfig) -> dict:
    """The setup Bundle (D11): full synthetic Patient and the synthetic Device, ``PUT``."""
    return synthetic.mark_synthetic(
        patient_setup_transaction(
            synthetic.full_patient(config.patient_id), devices=[synthetic.device()]
        )
    )


def ingest_bundle(
    config: SeedConfig, batch: list[dict], *, recorded: datetime, complete_before: datetime
) -> dict:
    """One ingest Bundle through the same T06 path health-sync uses."""
    return synthetic.mark_synthetic(
        heart_rate_transaction(
            batch,
            patient=synthetic.ingest_patient(config.patient_id),
            device=synthetic.device(),
            data_source=synthetic.data_source(),
            tz=config.time_zone,
            recorded=truncate_to_ms(recorded),
            complete_before=complete_before,
            assembler=synthetic.ASSEMBLER,
        )
    )


def _record_entry_statuses(report: Report, bundle: dict, response: dict) -> None:
    """Response statuses by resource type, e.g. ``Observation 201`` (created) or ``200``."""
    answers = response.get("entry") if isinstance(response, dict) else None
    if not isinstance(answers, list):
        return
    for sent, answer in zip(bundle["entry"], answers, strict=False):
        result = answer.get("response") if isinstance(answer, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        code = status.split(" ", 1)[0] if isinstance(status, str) else "?"
        key = f"{sent['resource']['resourceType']} {code if code.isdigit() else '?'}"
        report.entry_statuses[key] = report.entry_statuses.get(key, 0) + 1


def _with_retries(config: SeedConfig, deps: Dependencies, call: Callable[[], dict]) -> dict:
    """Bounded exponential backoff for answers that may pass if sent again: 429, 5xx, network
    (``RetryableError``) and 409 (two writes to one resource at once). A transaction is
    all-or-nothing, so a failed attempt wrote nothing and resending it is safe."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return call()
        except (RetryableError, ConflictError) as exc:
            if attempt >= config.max_attempts:
                raise
            delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1))
            logger.warning(
                "fhir request retried",
                extra={
                    "fields": {
                        "event": "fhir-retry",
                        "http_status": exc.status,
                        "attempt": attempt,
                        "delay_seconds": delay,
                    }
                },
            )
            deps.sleep(delay)


def _classify(exc: HealthcareApiError, report: Report) -> _Stop:
    if isinstance(exc, MalformedResponseError):
        return _Stop(Outcome.FHIR_REJECTED, "fhir-malformed-response", exc.status)
    if isinstance(exc, PermissionDeniedError):
        return _Stop(Outcome.FHIR_MISCONFIGURED, "fhir-permission-denied", exc.status)
    if isinstance(exc, NotFoundError):
        return _Stop(Outcome.FHIR_MISCONFIGURED, "fhir-store-missing", exc.status)
    if isinstance(exc, ConflictError):
        return _Stop(Outcome.FHIR_UNAVAILABLE, "fhir-conflict", exc.status)
    if isinstance(exc, ClientError):
        # The OperationOutcome is kept without its free text (diagnostics can quote values).
        report.fhir_issues = operation_outcome_summary(exc.operation_outcome)
        return _Stop(Outcome.FHIR_REJECTED, "fhir-refused", exc.status)
    return _Stop(Outcome.FHIR_UNAVAILABLE, "fhir-request-failed", exc.status)


# ---- safety check -------------------------------------------------------------------------------


def _unusable(reason: str) -> _Stop:
    return _Stop(Outcome.FHIR_REJECTED, reason, 200)


def patients_in_search(found: object) -> list[dict]:
    """The Patients in a safety-search answer, or ``_Stop(FHIR_REJECTED)`` if the answer cannot
    be trusted to list them all. Fail closed: reading a malformed answer as "no Patient" would
    let the setup ``PUT`` replace a Patient the search did not show.

    Required (R4 search, http.html#search): a ``Bundle`` of ``type`` ``searchset``; ``entry``
    absent or a list; each entry a Patient, or an ``OperationOutcome`` with ``search.mode``
    ``outcome`` (the server's notes about the search, which R4 allows in a searchset); and a
    ``total``, when given, no larger than the Patients returned."""
    if not isinstance(found, dict) or found.get("resourceType") != "Bundle":
        raise _unusable("safety-search-not-a-bundle")
    if found.get("type") != "searchset":
        raise _unusable("safety-search-not-a-searchset")
    entries = found.get("entry", [])
    if not isinstance(entries, list):
        raise _unusable("safety-search-malformed-entry")
    patients = []
    for entry in entries:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            raise _unusable("safety-search-malformed-entry")
        kind = resource.get("resourceType")
        if kind == "Patient":
            patients.append(resource)
        elif kind == "OperationOutcome" and (entry.get("search") or {}).get("mode") == "outcome":
            continue
        else:
            raise _unusable("safety-search-malformed-entry")
    total = found.get("total")
    if total is not None:
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise _unusable("safety-search-malformed-total")
        if total > len(patients):
            raise _unusable("safety-search-incomplete")
    return patients


@dataclass(frozen=True)
class SafetyResult:
    state: str  # no-patient, synthetic-patient, synthetic-incomplete, forced
    patient: dict | None  # the one stored Patient, when there is exactly one


def safety_check(
    config: SeedConfig, deps: Dependencies, client: FhirClient, store: str, *, force: bool
) -> SafetyResult:
    """One search for a Patient with the configured identifier.

    The setup Bundle *replaces* the Patient with that identifier (``PUT``), and the ingest
    Bundles add synthetic heart rate to its record. If that Patient holds what looks like a real
    person's record, seeding would overwrite it and mix invented readings into it. The ``SYN-``
    prefix makes this unlikely; the search makes it impossible without ``--force``.

    A search error (5xx after retries, 403, 404, ...) raises the client's error, which stops the
    run as any other FHIR failure does: nothing is written.
    """
    found = _with_retries(
        config,
        deps,
        lambda: client.search(
            store,
            "Patient",
            [
                (
                    "identifier",
                    f"{escape_search_value(codes.PB_PATIENT_ID)}|"
                    f"{escape_search_value(config.patient_id)}",
                ),
                ("_count", str(SAFETY_SEARCH_COUNT)),
            ],
        ),
    )
    patients = patients_in_search(found)
    if not patients:
        return SafetyResult("no-patient", None)
    if len(patients) == 1 and synthetic.is_synthetic_patient(patients[0]):
        complete = synthetic.is_complete(patients[0])
        return SafetyResult(
            "synthetic-patient" if complete else "synthetic-incomplete", patients[0]
        )
    reason = "patients-duplicated" if len(patients) > 1 else "patient-not-synthetic"
    if force:
        logger.warning(
            "safety check overridden with --force",
            extra={"fields": {"event": "seed-forced", "reason": reason}},
        )
        return SafetyResult("forced", None)
    raise _Stop(Outcome.REFUSED, reason)


# ---- read once ----------------------------------------------------------------------------------


def _stored(
    report: Report,
    client: FhirClient,
    store: str,
    safety: SafetyResult,
    window_range: Range,
) -> dict[str, StoredWindow] | None:
    """What is stored for the range, or ``None`` to write everything."""
    if safety.state == "no-patient":
        report.stored_read = "patient-not-found"  # nothing of this patient can be stored
        return {}
    if safety.state == "forced":
        # The stored record is not the seeder's: write every window over it, as --force says.
        report.stored_read = "skipped-forced"
        return None
    patient_id = safety.patient.get("id") if safety.patient else None
    if not isinstance(patient_id, str) or not patient_id:
        report.stored_read = "failed:no-patient-id"
        return None
    try:
        found = stored_windows(
            client, store, f"Patient/{patient_id}", window_range.start, window_range.end
        )
    except (HealthcareApiError, StoredReadError, ValueError) as exc:
        status = getattr(exc, "status", None)
        report.stored_read = f"failed:{status if status is not None else type(exc).__name__}"
        logger.warning(
            "stored windows could not be read; writing every window",
            extra={"fields": {"event": "stored-read-failed", "http_status": status}},
        )
        return None
    report.stored_read = "ok"
    return found


# ---- the run ------------------------------------------------------------------------------------


def run(
    config: SeedConfig,
    deps: Dependencies,
    *,
    length: timedelta,
    until: datetime | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> Report:
    now = deps.now()
    if until is not None and until > now:
        raise ValueError("until must not be later than now (no data from the future)")
    window_range = seed_range(until if until is not None else now, length)
    # The cutoff uses the real clock, not ``until``: a window is final once it really ended
    # FINAL_AFTER ago, however the range was chosen.
    cutoff = completion_cutoff(now, config.final_after)
    report = Report(
        range_start=window_range.start, range_end=window_range.end, complete_before=cutoff
    )
    try:
        _run(config, deps, report, window_range, cutoff, dry_run=dry_run, force=force)
    except _Stop as stop:
        if stop.outcome == Outcome.REFUSED:
            report.safety_check = "refused"
        report.outcome, report.reason, report.fhir_status = stop.outcome, stop.reason, stop.status
    except Exception as exc:  # noqa: BLE001 - reported with its counts; its text could quote data
        report.outcome, report.reason = Outcome.BUG, type(exc).__name__
    return report


def _run(
    config: SeedConfig,
    deps: Dependencies,
    report: Report,
    window_range: Range,
    cutoff: datetime,
    *,
    dry_run: bool,
    force: bool,
) -> None:
    generated = generate(config.seed, config.time_zone, window_range.start, window_range.end)
    report.windows_in_range = generated.slots
    report.windows_generated = len(generated.items)
    report.windows_absent = generated.absent
    report.bundles_planned = -(-len(generated.items) // config.batch_windows)
    if dry_run:
        report.outcome = Outcome.DRY_RUN
        return
    if deps.fhir is None or config.fhir is None:
        raise ValueError("a real run needs a FHIR client and FHIR configuration")
    client, store = deps.fhir, config.fhir.fhir_store_id
    patient_key = build_patient(PatientInput(config.patient_id)).key

    def key_of(item: dict) -> tuple[str, datetime, tuple]:
        window = window_from_rollup(item)
        return (
            heart_rate_key(patient_key, window),
            window.end,
            (window.bpm_min, window.bpm_avg, window.bpm_max),
        )

    def status_of(item: dict) -> str:
        return window_status(window_from_rollup(item), cutoff)

    try:
        safety = safety_check(config, deps, client, store, force=force)
        report.safety_check = safety.state
        if safety.state in ("no-patient", "synthetic-incomplete", "forced"):
            bundle = setup_bundle(config)
            response = _with_retries(config, deps, lambda: client.execute_bundle(store, bundle))
            _record_entry_statuses(report, bundle, response)
            report.setup_written = True
        plan = plan_writes(
            generated.items,
            _stored(report, client, store, safety, window_range),
            key_of=key_of,
            status_of=status_of,
        )
        report.windows_unchanged = plan.unchanged
        report.windows_new = plan.new
        report.windows_changed = plan.changed
        report.windows_finalised = plan.finalised
        report.windows_withdrawn_kept = plan.withdrawn_kept
        report.bundles_planned = -(-len(plan.write) // config.batch_windows)
        for batch in batches(plan.write, config.batch_windows):
            bundle = ingest_bundle(config, batch, recorded=deps.now(), complete_before=cutoff)
            response = _with_retries(config, deps, lambda b=bundle: client.execute_bundle(store, b))
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
    except HealthcareApiError as exc:
        raise _classify(exc, report) from None
