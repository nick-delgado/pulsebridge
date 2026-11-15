"""``pulsebridge-seed``: the command line, also the Cloud Run job's command (README).

    pulsebridge-seed --days 30                    # first seeding: the last 30 days
    pulsebridge-seed --hours 3                    # scheduled top-up (README, "Running it")
    pulsebridge-seed --days 1 --dry-run           # counts only, no requests, no GCP config
    pulsebridge-seed --days 7 --until 2026-03-09T12:00:00Z --seed 42

Logs (JSON lines) go to stderr, the run report (JSON, counts only) to stdout. Exit codes:
0 seeded or dry run; 1 a FHIR failure or an unexpected error (outcome ``bug``, with the
counts so far; re-run: every write is keyed); 2 bad arguments or
configuration; 4 refused by the safety check (a Patient with this identifier that the seeder
did not write).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import UTC, datetime, timedelta

from pulsebridge_seed.config import ConfigError, SeedConfig
from pulsebridge_seed.logs import configure_logging
from pulsebridge_seed.seeder import Dependencies, Outcome, Report, run

MAX_DAYS = 366
MAX_HOURS = 24 * 14
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 4
# An explicit offset or Z is required: a bare local time would depend on the machine's zone.
_INSTANT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?(Z|[+-][0-9]{2}:[0-9]{2})$"
)

logger = logging.getLogger("pulsebridge_seed")


def _bounded(high: int):
    def parse(text: str) -> int:
        if not re.fullmatch(r"[0-9]{1,4}", text) or not 1 <= int(text) <= high:
            raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {high}")
        return int(text)

    return parse


def _instant(text: str) -> datetime:
    if not _INSTANT.fullmatch(text):
        raise argparse.ArgumentTypeError(
            "must be an ISO 8601 time with Z or an offset, e.g. 2026-03-09T12:00:00Z"
        )
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        raise argparse.ArgumentTypeError("is not a valid time") from None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pulsebridge-seed",
        description="Write deterministic synthetic heart-rate windows for a synthetic patient "
        "(PATIENT_ID must start with SYN-) to the FHIR store. Only for deployments with "
        "use_synthetic_data = true. Safe to re-run.",
    )
    length = parser.add_mutually_exclusive_group(required=True)
    length.add_argument("--days", type=_bounded(MAX_DAYS), help="days of windows to write")
    length.add_argument("--hours", type=_bounded(MAX_HOURS), help="hours of windows to write")
    parser.add_argument(
        "--until",
        type=_instant,
        help="end of the range (exclusive), floored to five minutes; default now; not later "
        "than now",
    )
    parser.add_argument("--seed", help="generator seed (default: SYNTHETIC_SEED, else 1)")
    parser.add_argument("--dry-run", action="store_true", help="generate and count; no writes")
    parser.add_argument(
        "--force",
        action="store_true",
        help="write even if the store holds a Patient with this identifier that the seeder "
        "did not write (it will be replaced)",
    )
    return parser.parse_args(argv)


def _exit_code(report: Report) -> int:
    if report.outcome in (Outcome.SEEDED, Outcome.DRY_RUN):
        return EXIT_OK
    if report.outcome == Outcome.REFUSED:
        return EXIT_REFUSED
    return EXIT_FAILED


def default_dependencies(config: SeedConfig) -> Dependencies:
    # Imported here so --dry-run and the tests never touch google-auth.
    from pulsebridge_gcp import HealthcareClient, default_transport

    if config.fhir is None:
        raise ValueError("a real run needs the FHIR store configuration")
    client = HealthcareClient(
        project=config.fhir.project_id,
        location=config.fhir.location,
        dataset=config.fhir.dataset_id,
        transport=default_transport(),  # the seeder service account (ADC) on Cloud Run
        timeout=config.fhir_timeout,
    )
    return Dependencies(fhir=client)


def main(argv: list[str] | None = None, deps: Dependencies | None = None) -> int:
    args = parse_args(argv)  # exits with 2 on bad arguments
    configure_logging(sys.stderr)
    try:
        config = SeedConfig.from_env(need_fhir=not args.dry_run, seed_override=args.seed)
    except ConfigError as exc:
        print(f"pulsebridge-seed: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.dry_run and deps is None:
        deps = Dependencies(fhir=None)
    now = (deps.now if deps is not None else Dependencies(fhir=None).now)()
    if args.until is not None and args.until > now:
        print("pulsebridge-seed: --until must not be later than now", file=sys.stderr)
        return EXIT_USAGE
    length = timedelta(days=args.days) if args.days else timedelta(hours=args.hours)
    try:
        if deps is None:
            deps = default_dependencies(config)
        report = run(
            config, deps, length=length, until=args.until, dry_run=args.dry_run, force=args.force
        )
    except Exception as exc:  # noqa: BLE001 - a traceback's text could quote data
        # Before the run started (for example no Application Default Credentials). run()
        # itself reports its own unexpected errors as outcome "bug", with its counts.
        report = Report(outcome=Outcome.BUG, reason=type(exc).__name__)
    level = logging.INFO if _exit_code(report) == EXIT_OK else logging.WARNING
    if report.outcome == Outcome.BUG:
        level = logging.ERROR
    logger.log(level, "seed run", extra={"fields": {"event": "seed-run", **report.as_dict()}})
    print(json.dumps(report.as_dict(), indent=2))
    return _exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
