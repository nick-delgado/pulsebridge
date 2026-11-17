"""Part B: health-sync's first-sync checks (services/health-sync/README.md, "For T25").

They need a stack with real data (``use_synthetic_data = false``): only then is health-sync
deployed. On a synthetic stack every Part B check is reported ``SKIP`` with the reason.

**Where the evidence is.** Each sync run's report (``entry_statuses``, ``stored_read``,
``windows_unchanged``, ...) is the answer to Cloud Scheduler's ``POST /sync``, and the same report
is logged as one structured ``sync-run`` line (``jsonPayload``). health-sync is internal-only
(no public ingress), and fhir-proxy's ``/status`` passes on only the connection state, not the
run report. So from outside the reports can be read **only from Cloud Logging**. The front door
gives the one Part B fact it can: ``watchSync.state`` must be ``connected``.

The runner supplies the log access; nothing about the project is hardcoded:

- ``--sync-runs-from gcloud``: this runs ``gcloud logging read`` with the caller's own
  credentials (``roles/logging.viewer`` on the project is enough), the project from
  ``CLOUDSDK_CORE_PROJECT`` and the service name from ``PULSEBRIDGE_HEALTH_SYNC_SERVICE``
  (``terraform output -raw health_sync_service_name``);
- ``--sync-runs-from <file>``: the JSON that same command printed, saved by the runner.

The log lines hold counts and codes only (health-sync's README, "Logging": never heart-rate
values, tokens or the patient id), so reading them exposes no health data.

**The checks.**

- B1 health-sync reports ``connected`` (else no run can have synced).
- B2 **The Patient entry answers 200, not 201.** health-sync's transaction Bundle creates the
  Patient conditionally ("create only if no Patient has this identifier", FHIR ``ifNoneExist``).
  The setup step already wrote it, so the store must *match* (``200``) and create nothing; a
  ``201`` means it made a second Patient: the store did not resolve the in-Bundle reference to
  the existing one, and T06's fixed-ID fallback is needed (T06 README).
- B3 **The second run reports ``stored_read: ok`` and ``windows_unchanged > 0``.** Each run
  reads what is stored before writing; ``ok`` with unchanged windows proves Cloud Healthcare
  answers the searches the service relies on. ``ok`` with 0 unchanged, run after run, means the
  read silently matches nothing and every window is rewritten.
- B4 (``WARN`` only, not an acceptance criterion) ``stranded_preliminary`` is a number.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pulsebridge_smoke.children import child_environment, gcloud_said
from pulsebridge_smoke.report import Report

SERVICE_NAME = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
FRESHNESS = re.compile(r"[1-9][0-9]{0,3}[mhd]")
GCLOUD_TIMEOUT_SECONDS = 120


class SourceError(Exception):
    """The run reports could not be read. The message names the cause, never a value."""


class SyncRunSource(Protocol):
    description: str

    def fetch(self) -> object:
        """Log entries as ``gcloud logging read --format=json`` prints them."""
        ...


@dataclass
class GcloudSyncRuns:
    """``gcloud logging read`` for health-sync's ``sync-run`` lines, newest first."""

    project: str
    service: str
    freshness: str = "12h"
    limit: int = 50
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def __post_init__(self) -> None:
        if not PROJECT_ID.fullmatch(self.project):
            raise ValueError("the project id (CLOUDSDK_CORE_PROJECT) is not a valid project id")
        if not SERVICE_NAME.fullmatch(self.service):
            raise ValueError("PULSEBRIDGE_HEALTH_SYNC_SERVICE is not a Cloud Run service name")
        if not FRESHNESS.fullmatch(self.freshness):
            raise ValueError("--log-freshness must look like 30m, 12h or 2d")

    @property
    def description(self) -> str:
        return f"Cloud Logging, service {self.service}, last {self.freshness}"

    def command(self) -> list[str]:
        log_filter = (
            'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{self.service}" '
            'AND jsonPayload.event="sync-run"'
        )
        return [
            "gcloud",
            "logging",
            "read",
            log_filter,
            f"--project={self.project}",
            f"--freshness={self.freshness}",
            f"--limit={self.limit}",
            "--order=desc",
            "--format=json",
        ]

    def fetch(self) -> object:
        try:
            done = self.run(
                self.command(),
                capture_output=True,
                text=True,
                timeout=GCLOUD_TIMEOUT_SECONDS,
                check=False,
                env=child_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceError(f"gcloud logging read did not run ({type(exc).__name__})") from None
        if done.returncode != 0:
            raise SourceError(
                f"gcloud logging read failed (exit {done.returncode}). Check that gcloud is "
                "logged in as you (gcloud auth list), that CLOUDSDK_CORE_PROJECT is the stack's "
                "project and that your account has roles/logging.viewer there. "
                + gcloud_said(done.stderr)
            )
        try:
            return json.loads(done.stdout or "[]")
        except ValueError:
            raise SourceError("gcloud logging read did not print JSON") from None


@dataclass
class FileSyncRuns:
    path: Path

    @property
    def description(self) -> str:
        return f"file {self.path}"

    def fetch(self) -> object:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SourceError(f"{self.path} is not readable JSON ({type(exc).__name__})") from None


def sync_runs(entries: object) -> list[dict]:
    """The ``synced`` run reports, newest first. Accepts log entries (each with a
    ``jsonPayload``) or bare reports. Other outcomes (``already-running``, ``reconnect-needed``,
    a 503) carry no Bundle results and are left out."""
    if not isinstance(entries, list):
        raise SourceError("the log entries are not a JSON list")
    runs = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        payload = entry.get("jsonPayload", entry)
        if not isinstance(payload, dict) or payload.get("event") != "sync-run":
            continue
        if payload.get("outcome") == "synced":
            runs.append((str(entry.get("timestamp") or ""), payload))
    runs.sort(key=lambda item: item[0], reverse=True)
    return [payload for _, payload in runs]


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def patient_problem(runs: list[dict]) -> tuple[str | None, str]:
    """B2. (problem, ok text)."""
    with_patient = [
        r
        for r in runs
        if isinstance(r.get("entry_statuses"), dict)
        and any(str(k).startswith("Patient ") for k in r["entry_statuses"])
    ]
    if not with_patient:
        return (
            f"none of the {len(runs)} synced runs found wrote a Bundle (no Patient entry); "
            "wait for a run with data, or widen --log-freshness",
            "",
        )
    created = [r for r in with_patient if _count(r["entry_statuses"].get("Patient 201"))]
    other = [
        r
        for r in with_patient
        if any(str(k).startswith("Patient ") and k != "Patient 200" for k in r["entry_statuses"])
    ]
    if created:
        return (
            f"Patient 201 in {len(created)} of {len(with_patient)} runs: the store created a "
            "second Patient instead of matching the setup step's; switch to the fixed-ID "
            "fallback in the T06 README (libs/pulsebridge_fhir) and record the result",
            "",
        )
    if other:
        statuses = sorted(
            {k for r in other for k in r["entry_statuses"] if k.startswith("Patient")}
        )
        return f"unexpected Patient entry statuses {statuses}", ""
    return None, f"Patient 200 in all {len(with_patient)} runs that wrote a Bundle"


def stored_read_problem(runs: list[dict]) -> tuple[str | None, str]:
    """B3. The newest run with windows, which must not be the only one."""
    with_windows = [r for r in runs if (_count(r.get("windows_fetched")) or 0) > 0]
    if len(with_windows) < 2:
        return (
            f"{len(with_windows)} synced run(s) with windows found; the check needs the second "
            "one (runs are every 15 minutes)",
            "",
        )
    newest = with_windows[0]
    stored_read = newest.get("stored_read")
    unchanged = _count(newest.get("windows_unchanged"))
    if stored_read != "ok":
        return (
            f"newest run: stored_read {stored_read!r}: the read step failed, so every window "
            "was rewritten (health-sync README, 'Read once, write what changed')",
            "",
        )
    if not unchanged:
        never = all(not _count(r.get("windows_unchanged")) for r in with_windows)
        return (
            f"newest run: stored_read 'ok' but windows_unchanged {unchanged!r}"
            + (
                f" in all {len(with_windows)} runs: the read step is silently matching nothing"
                if never
                else ""
            ),
            "",
        )
    return None, f"stored_read ok, windows_unchanged {unchanged} (of {len(with_windows)} runs)"


SKIP_REASON = "skipped: synthetic data (health-sync is not deployed on this stack)"
PART_B = (
    ("B1", "health-sync is connected to Google Health"),
    ("B2", "first sync: the Patient entry answers 200, not 201"),
    ("B3", "second sync: stored_read ok and windows_unchanged > 0"),
    ("B4", "stranded_preliminary is a number"),
)


def run_part_b(
    report: Report,
    *,
    real_data: bool,
    watch_state: str | None,
    watch_reason: str | None,
    source: SyncRunSource | None,
) -> None:
    if not real_data:
        for check_id, title in PART_B:
            report.skip(check_id, title, SKIP_REASON)
        return
    titles = dict(PART_B)
    report.check(
        "B1",
        titles["B1"],
        None
        if watch_state == "connected"
        else f"watchSync.state {watch_state!r}, reason {watch_reason!r}",
    )
    if source is None:
        need = (
            "needs health-sync's run reports from Cloud Logging: pass --sync-runs-from gcloud "
            "(with PULSEBRIDGE_HEALTH_SYNC_SERVICE and CLOUDSDK_CORE_PROJECT, and "
            "roles/logging.viewer) or --sync-runs-from <file>"
        )
        for check_id in ("B2", "B3"):
            report.check(check_id, titles[check_id], need)
        report.skip("B4", titles["B4"], "no run reports")
        return
    try:
        runs = sync_runs(source.fetch())
    except SourceError as exc:
        for check_id in ("B2", "B3"):
            report.check(check_id, titles[check_id], f"{source.description}: {exc}")
        report.skip("B4", titles["B4"], "no run reports")
        return
    report.out(f"      Part B read {len(runs)} synced run report(s) from {source.description}")
    problem, ok = patient_problem(runs)
    report.check("B2", titles["B2"], problem, ok=ok)
    problem, ok = stored_read_problem(runs)
    report.check("B3", titles["B3"], problem, ok=ok)
    newest = runs[0] if runs else {}
    if _count(newest.get("stranded_preliminary")) is None:
        report.warn(
            "B4",
            titles["B4"],
            f"newest run has stranded_preliminary {newest.get('stranded_preliminary')!r}: "
            "the count search failed or was skipped (look for stranded-count-failed lines)",
        )
    else:
        report.check("B4", titles["B4"], None, ok=str(newest["stranded_preliminary"]))
