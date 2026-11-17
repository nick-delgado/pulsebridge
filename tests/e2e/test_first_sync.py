"""Part B (health-sync's first-sync checks) against synthetic run reports. It cannot run live
yet: no stack with real data exists. The reports below have the shape health-sync logs
(``services/health-sync/health_sync/sync.py``, ``RunReport.as_dict``; ``app.py`` adds
``event`` and ``http_status``), wrapped as ``gcloud logging read --format=json`` prints them.
Every count is invented."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from e2e_fake_stack import Stack
from e2e_runs import run
from pulsebridge_smoke.cli import UsageError, parser, sync_source
from pulsebridge_smoke.first_sync import (
    FileSyncRuns,
    GcloudSyncRuns,
    SourceError,
    run_part_b,
    sync_runs,
)
from pulsebridge_smoke.report import Report


def report_line(
    minute: int,
    *,
    patient: str = "Patient 200",
    stored_read: str = "ok",
    unchanged: int = 33,
    fetched: int = 36,
    stranded: int | None = 0,
    outcome: str = "synced",
) -> dict:
    """One ``sync-run`` log entry, as Cloud Logging returns it."""
    statuses = {patient: 1, "Device 200": 1, "Observation 200": 3, "Provenance 200": 3}
    return {
        "timestamp": f"2026-11-20T10:{minute:02d}:00.000000Z",
        "severity": "INFO",
        "resource": {"type": "cloud_run_revision", "labels": {"service_name": "pb-health-sync"}},
        "jsonPayload": {
            "event": "sync-run",
            "http_status": 200,
            "outcome": outcome,
            "reason": None,
            "windows_fetched": fetched,
            "windows_unchanged": unchanged,
            "windows_written": fetched - unchanged,
            "stored_read": stored_read,
            "stranded_preliminary": stranded,
            "entry_statuses": statuses if fetched else {},
        },
    }


class Source:
    description = "test entries"

    def __init__(self, entries: object, error: SourceError | None = None) -> None:
        self.entries, self.error = entries, error

    def fetch(self) -> object:
        if self.error:
            raise self.error
        return self.entries


def part_b(entries: object = None, *, state: str = "connected", source=None) -> dict:  # noqa: ANN001
    lines: list[str] = []
    report = Report(out=lines.append)
    run_part_b(
        report,
        real_data=True,
        watch_state=state,
        watch_reason=None,
        source=source if source is not None else Source(entries),
    )
    return {r.check_id: r for r in report.results}


GOOD = [report_line(30), report_line(15), report_line(0, unchanged=0)]


def test_good_runs_pass_every_check() -> None:
    results = part_b(GOOD)
    assert {k: r.status.value for k, r in results.items()} == {
        "B1": "PASS",
        "B2": "PASS",
        "B3": "PASS",
        "B4": "PASS",
    }
    assert "Patient 200 in all 3 runs" in results["B2"].detail
    assert "windows_unchanged 33" in results["B3"].detail


def test_a_patient_201_fails_b2_and_names_the_fallback() -> None:
    results = part_b([report_line(30), report_line(15, patient="Patient 201")])
    assert results["B2"].status.value == "FAIL"
    assert "Patient 201 in 1 of 2 runs" in results["B2"].detail
    assert "fixed-ID fallback" in results["B2"].detail


def test_no_run_that_wrote_a_bundle_fails_b2() -> None:
    results = part_b([report_line(30, fetched=0, unchanged=0)])
    assert results["B2"].status.value == "FAIL"
    assert "wrote a Bundle" in results["B2"].detail


def test_only_one_run_with_windows_fails_b3() -> None:
    results = part_b([report_line(30)])
    assert results["B3"].status.value == "FAIL"
    assert "needs the second one" in results["B3"].detail


def test_a_failed_read_fails_b3() -> None:
    results = part_b([report_line(30, stored_read="failed:400"), report_line(15)])
    assert results["B3"].status.value == "FAIL"
    assert "'failed:400'" in results["B3"].detail


def test_the_newest_run_decides_b3() -> None:
    """An older good run does not hide a newer bad one, whatever order the entries come in."""
    entries = [report_line(15), report_line(30, stored_read="skipped-for-time")]
    results = part_b(entries)
    assert results["B3"].status.value == "FAIL"
    assert "skipped-for-time" in results["B3"].detail


def test_zero_unchanged_in_every_run_is_called_silent() -> None:
    results = part_b([report_line(30, unchanged=0), report_line(15, unchanged=0)])
    assert results["B3"].status.value == "FAIL"
    assert "silently matching nothing" in results["B3"].detail


def test_other_outcomes_are_not_counted_as_runs() -> None:
    entries = [
        report_line(45, outcome="already-running"),
        report_line(40, outcome="reconnect-needed", patient="Patient 201"),
        *GOOD,
    ]
    assert part_b(entries)["B2"].status.value == "PASS"
    assert len(sync_runs(entries)) == 3


def test_a_null_stranded_count_is_a_warning_not_a_failure() -> None:
    results = part_b([report_line(30, stranded=None), report_line(15)])
    assert results["B4"].status.value == "WARN"
    assert results["B2"].status.value == "PASS"


def test_not_connected_fails_b1() -> None:
    results = part_b(GOOD, state="reconnect-needed")
    assert results["B1"].status.value == "FAIL"
    assert "'reconnect-needed'" in results["B1"].detail


def test_a_source_that_fails_fails_b2_and_b3() -> None:
    results = part_b(source=Source(None, SourceError("gcloud logging read failed (exit 1)")))
    assert results["B2"].status.value == "FAIL"
    assert "exit 1" in results["B3"].detail


def test_no_source_on_a_real_stack_fails_and_says_what_is_needed() -> None:
    lines: list[str] = []
    report = Report(out=lines.append)
    run_part_b(report, real_data=True, watch_state="connected", watch_reason=None, source=None)
    results = {r.check_id: r for r in report.results}
    assert results["B2"].status.value == "FAIL"
    assert "PULSEBRIDGE_HEALTH_SYNC_SERVICE" in results["B2"].detail
    assert "roles/logging.viewer" in results["B2"].detail


def test_a_synthetic_stack_skips_part_b_whatever_the_logs_say() -> None:
    lines: list[str] = []
    report = Report(out=lines.append)
    bad = Source([report_line(30, patient="Patient 201")])
    run_part_b(report, real_data=False, watch_state="unknown", watch_reason=None, source=bad)
    assert [r.status.value for r in report.results] == ["SKIP"] * 4
    assert all("synthetic data" in r.detail for r in report.results)


# ---- the log sources ----------------------------------------------------------------------


def test_gcloud_source_reads_only_health_syncs_sync_run_lines() -> None:
    calls = []

    def fake(command, **kwargs):  # noqa: ANN001, ANN202
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(GOOD), stderr="")

    source = GcloudSyncRuns("example-project-1", "pb-health-sync", freshness="6h", run=fake)
    assert source.fetch() == GOOD
    command, kwargs = calls[0]
    assert command[:3] == ["gcloud", "logging", "read"]
    assert 'resource.labels.service_name="pb-health-sync"' in command[3]
    assert 'jsonPayload.event="sync-run"' in command[3]
    assert "--project=example-project-1" in command
    assert "--freshness=6h" in command
    assert "--format=json" in command
    assert kwargs["capture_output"] is True


def test_gcloud_failure_is_a_source_error() -> None:
    def fake(command, **_):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="x\nPERMISSION_DENIED")

    with pytest.raises(SourceError, match="exit 1.*PERMISSION_DENIED"):
        GcloudSyncRuns("example-project-1", "pb-health-sync", run=fake).fetch()


@pytest.mark.parametrize(
    ("project", "service", "freshness"),
    [
        ("example-project-1", 'pb" OR x="y', "6h"),  # no filter injection
        ("Example", "pb-health-sync", "6h"),
        ("example-project-1", "pb-health-sync", "6 hours"),
    ],
)
def test_gcloud_source_refuses_malformed_settings(
    project: str, service: str, freshness: str
) -> None:
    with pytest.raises(ValueError):
        GcloudSyncRuns(project, service, freshness=freshness)


def _args(*argv: str) -> argparse.Namespace:
    return parser().parse_args(list(argv))


def test_sync_source_is_chosen_from_flags_and_environment(tmp_path: Path) -> None:
    assert sync_source(_args(), {}) is None
    chosen = sync_source(
        _args(),
        {
            "PULSEBRIDGE_HEALTH_SYNC_SERVICE": "pb-health-sync",
            "CLOUDSDK_CORE_PROJECT": "example-p1",
        },
    )
    assert isinstance(chosen, GcloudSyncRuns)
    assert chosen.service == "pb-health-sync"
    from_file = sync_source(_args("--sync-runs-from", str(tmp_path / "r.json")), {})
    assert isinstance(from_file, FileSyncRuns)
    with pytest.raises(UsageError):
        sync_source(_args("--sync-runs-from", "gcloud"), {"CLOUDSDK_CORE_PROJECT": "example-p1"})


def test_a_real_data_stack_runs_part_b_from_a_saved_log_file(
    real_stack: Stack, password: str, tmp_path: Path
) -> None:
    """The whole command on a real-data stack: Part B from the file the runner saved with
    `gcloud logging read ... --format=json`, and Part A with the explicit flag."""
    path = tmp_path / "sync-runs.json"
    path.write_text(json.dumps(GOOD))
    result = run(real_stack, password, "--sync-runs-from", str(path), "--allow-real-data-writes")
    for check_id in ("B1", "B2", "B3", "B4", "A10", "N5"):
        assert result.status_of(check_id) == "PASS", result.text
    assert result.code == 0, result.text
    assert "Part B read 3 synced run report(s) from file" in result.text
