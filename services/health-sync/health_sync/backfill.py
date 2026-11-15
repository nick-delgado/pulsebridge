"""``backfill --days N``: sync the last N days, for a first connect or after a long outage.

    health-sync-backfill --days 30          # installed console script
    python -m health_sync.backfill --days 30

It reuses the sync code exactly: the same configuration (environment variables and mounted
secret files), the same mapping, keys, batching and retries. The only differences are the range
(N × 24 hours ending at the last five-minute boundary, split into requests of at most 14 days,
the API's limit for heart-rate roll-ups) and that there is no overall deadline. Re-running is
safe: every window lands on the same Observation.

Output: logs (JSON lines) on stderr, the run report (JSON) on stdout. Exit codes: 0 synced;
3 reconnect needed or not configured (re-run after reconnecting); 1 any other failure (re-run;
the ranges already written are rewritten, harmlessly); 2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import sys

from health_sync.app import check_startup, default_dependencies, log_report
from health_sync.config import ConfigError, ServiceConfig
from health_sync.logs import configure_logging
from health_sync.sync import Outcome, SyncDependencies, backfill_range, run_sync, split_range

MAX_DAYS = 366
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_RECONNECT = 0, 1, 2, 3


def _days(text: str) -> int:
    if not text.isdigit() or not 1 <= int(text) <= MAX_DAYS:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_DAYS}")
    return int(text)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="health-sync-backfill",
        description="Sync the last N days of Google Health heart-rate roll-ups into the FHIR "
        "store (requests of at most 14 days; safe to re-run).",
    )
    parser.add_argument("--days", type=_days, required=True, help="how many days back to sync")
    return parser.parse_args(argv)


def run(days: int, config: ServiceConfig, deps: SyncDependencies) -> int:
    pieces = split_range(backfill_range(deps.now(), days))
    report = run_sync(config, deps, ranges=pieces, deadline_seconds=None)
    log_report("backfill-run", report)
    print(json.dumps(report.as_dict(), indent=2))
    if report.outcome == Outcome.SYNCED:
        return EXIT_OK
    if report.outcome in (Outcome.RECONNECT_NEEDED, Outcome.NOT_CONFIGURED):
        return EXIT_RECONNECT
    return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)  # exits with 2 on bad arguments
    configure_logging(sys.stderr)
    try:
        config = ServiceConfig.from_env()
        check_startup(config)
    except ConfigError as exc:
        print(f"health-sync-backfill: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return run(args.days, config, default_dependencies(config))


if __name__ == "__main__":
    sys.exit(main())
