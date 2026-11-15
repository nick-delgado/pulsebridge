"""One seeder run.

Flow (the code below runs in this order):

1. Work out the range: ``[until - length, until)``, both on five-minute boundaries in UTC (the
   same alignment health-sync uses, so keys repeat across runs). ``until`` defaults to now.
2. Generate the windows (:mod:`pulsebridge_seed.generator`): pure, no I/O.
3. ``--dry-run`` stops here and reports counts.
4. **Safety check**, one FHIR search: ``Patient?identifier=urn:pulsebridge:patient|<id>``. If a
   Patient with this identifier exists and is not one the seeder wrote (no ``HTEST`` tag and not
   the synthetic name), the run is refused before anything is written, unless ``--force``.
5. **Setup Bundle**, once per run, before any data (decision D11): T06's
   ``patient_setup_transaction``, the full synthetic Patient and the synthetic Device as
   conditional updates (``PUT Patient?identifier=…``). Repeating it changes nothing.
6. **Ingest Bundles**, ``BATCH_WINDOWS`` windows each: T06's ``heart_rate_transaction`` (Patient
   and Device as conditional creates, each Observation a conditional update keyed on patient,
   LOINC 8867-4 and window start, each Provenance a ``PUT`` by an id derived from that key).
   Windows that ended after ``floor(now - FINAL_AFTER)`` are ``preliminary``, the rest ``final``:
   health-sync's cutoff rule, so the dashboard's provisional display is exercised too.

Why a re-run does not duplicate: every write is keyed, and the values are a function of the
seed, the zone and the window start, so the same window is the same Observation with the same
content. Only Provenance ``recorded`` (when it was written) and, for a window that has since
become old enough, ``status`` (``preliminary`` to ``final``) change.

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
    codes,
    heart_rate_transaction,
    patient_setup_transaction,
    truncate_to_ms,
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

logger = logging.getLogger("pulsebridge_seed")

BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 16.0


class Outcome(StrEnum):
    SEEDED = "seeded"
    DRY_RUN = "dry-run"
    REFUSED = "refused"  # the safety check found a Patient the seeder did not write
    FHIR_UNAVAILABLE = "fhir-unavailable"  # 429/5xx/409/network after the retries
    FHIR_MISCONFIGURED = "fhir-misconfigured"  # 401/403/404
    FHIR_REJECTED = "fhir-rejected"  # a 4xx refusal or a malformed 2xx


class FhirClient:
    """What the run needs from :class:`pulsebridge_gcp.HealthcareClient`."""

    def execute_bundle(self, fhir_store: str, bundle: dict) -> dict: ...  # pragma: no cover
    def search(
        self, fhir_store: str, resource_type: str, params: object
    ) -> dict: ...  # pragma: no cover


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
    windows_written: int = 0
    windows_final: int = 0
    windows_preliminary: int = 0
    bundles_planned: int = 0
    bundles_written: int = 0
    setup_written: bool = False
    safety_check: str = "not-run"  # not-run, no-patient, synthetic-patient, forced, refused
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
            "windows_written": self.windows_written,
            "windows_final": self.windows_final,
            "windows_preliminary": self.windows_preliminary,
            "bundles_planned": self.bundles_planned,
            "bundles_written": self.bundles_written,
            "setup_written": self.setup_written,
            "safety_check": self.safety_check,
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


def safety_check(
    config: SeedConfig, deps: Dependencies, client: FhirClient, store: str, *, force: bool
) -> str:
    """One search for a Patient with the configured identifier.

    The setup Bundle *replaces* the Patient with that identifier (``PUT``), and the ingest
    Bundles would add synthetic heart rate to its record. If that Patient was not written by
    the seeder (a real person connected through T11, or one health-sync or v2-to-fhir created on
    first contact), seeding would overwrite a real record and mix invented readings into it.
    The ``SYN-`` prefix makes this unlikely; the search makes it impossible without ``--force``.

    Returns ``no-patient``, ``synthetic-patient`` or ``forced``; raises ``_Stop(REFUSED)``.
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
                ("_count", "10"),
            ],
        ),
    )
    patients = [
        entry.get("resource")
        for entry in found.get("entry", []) or []
        if isinstance(entry, dict)
        and isinstance(entry.get("resource"), dict)
        and entry["resource"].get("resourceType") == "Patient"
    ]
    if not patients:
        return "no-patient"
    if len(patients) == 1 and synthetic.is_synthetic_patient(patients[0]):
        return "synthetic-patient"
    reason = "patients-duplicated" if len(patients) > 1 else "patient-not-synthetic"
    if force:
        logger.warning(
            "safety check overridden with --force",
            extra={"fields": {"event": "seed-forced", "reason": reason}},
        )
        return "forced"
    raise _Stop(Outcome.REFUSED, reason)


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
    window_range = seed_range(until if until is not None else now, length)
    cutoff = completion_cutoff(now, config.final_after)
    report = Report(
        range_start=window_range.start, range_end=window_range.end, complete_before=cutoff
    )
    generated = generate(config.seed, config.time_zone, window_range.start, window_range.end)
    report.windows_in_range = generated.slots
    report.windows_generated = len(generated.items)
    report.windows_absent = generated.absent
    report.bundles_planned = -(-len(generated.items) // config.batch_windows)
    if dry_run:
        report.outcome = Outcome.DRY_RUN
        return report
    if deps.fhir is None or config.fhir is None:
        raise ValueError("a real run needs a FHIR client and FHIR configuration")
    client, store = deps.fhir, config.fhir.fhir_store_id
    try:
        try:
            report.safety_check = safety_check(config, deps, client, store, force=force)
            bundle = setup_bundle(config)
            response = _with_retries(config, deps, lambda: client.execute_bundle(store, bundle))
            _record_entry_statuses(report, bundle, response)
            report.setup_written = True
            for batch in batches(generated.items, config.batch_windows):
                bundle = ingest_bundle(config, batch, recorded=deps.now(), complete_before=cutoff)
                response = _with_retries(
                    config,
                    deps,
                    lambda b=bundle: client.execute_bundle(store, b),
                )
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
    except _Stop as stop:
        if stop.outcome == Outcome.REFUSED:
            report.safety_check = "refused"
        report.outcome, report.reason, report.fhir_status = stop.outcome, stop.reason, stop.status
    return report
