"""The smoke command end to end against the in-process stack (real fhir-proxy, cuff-sim and
v2-to-fhir code over fakes): what it reports, what it writes, the real-data guard, secrets,
configuration and the exit status."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from e2e_fake_stack import REAL_PATIENT, Stack, build_stack
from e2e_runs import BASE, bp_component, edit_json, run
from fhir_proxy.share.testing import FakeOpenEmr
from pulsebridge_smoke.cli import secret_manager_password
from pulsebridge_smoke.http import Response, TransportError

PART_A_WRITES = ("A1", "A10", "N1")


def _readings_and_shares(stack: Stack) -> list[str]:
    return [
        r["path"]
        for r in stack.transport.requests
        if r["method"] == "POST" and r["path"] in ("/cuff/readings", "/share")
    ]


def test_synthetic_stack_passes_with_part_b_skipped(stack: Stack, password: str) -> None:
    result = run(stack, password)
    assert result.code == 0, result.text
    for check_id in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"):
        assert result.status_of(check_id) == "PASS", result.text
    for check_id in ("A1", "A2", "A3", "A4", "A5", "A6", "A8", "A9", "A10", "A12"):
        assert result.status_of(check_id) == "PASS", result.text
    for check_id in ("A7a", "A7b", "A7c", "A7d", "A7e", "A7f", "A7g", "A7h"):
        assert result.status_of(check_id) == "PASS", result.text
    for check_id in ("A11a", "A11b", "A11c", "N1", "N2", "N3", "N4", "N5"):
        assert result.status_of(check_id) == "PASS", result.text
    for check_id in ("B1", "B2", "B3", "B4"):
        assert result.status_of(check_id) == "SKIP", result.text
        assert "skipped: synthetic data" in result.line_of(check_id)
    # Skips are listed in the summary and named in the verdict: not a silent pass.
    summary = result.text.split("\nSummary\n", 1)[1]
    assert all(f"SKIP B{n}" in summary for n in range(1, 5))
    assert result.lines[-1].startswith("SMOKE RESULT: PASSED WITH 4 SKIPPED")


def test_what_one_run_adds_and_reports(stack: Stack, password: str) -> None:
    result = run(stack, password)
    assert result.code == 0, result.text
    # Exactly one Observation (the reading); the rejected and unmappable sends made none.
    observations = stack.observations()
    assert len(observations) == 1
    stored = observations[0]
    printed = next(line for line in result.lines if "trail: reading = " in line)
    systolic, diastolic = printed.split("= ", 1)[1].split(" ", 1)[0].split("/")
    assert bp_component(stored, "8480-6")["valueQuantity"]["value"] == int(systolic)
    assert bp_component(stored, "8462-4")["valueQuantity"]["value"] == int(diastolic)
    assert f"trail: FHIR Observation = Observation/{stored['id']}" in result.text
    # One Observation reached OpenEMR, although it was shared twice.
    openemr: FakeOpenEmr = stack.env.openemr
    assert len(openemr.observations) == 1
    (openemr_id,) = openemr.observations
    assert f"OpenEMR Observation = {openemr_id}" in result.text
    # Three sends reached the "edge VM": the reading, the AE sample and the unmappable one;
    # the invalid 80/120 never left cuff-sim.
    assert len(stack.edge.received) == 3


def test_a_rerun_adds_a_new_reading_and_cross_checks_the_earlier_one(
    stack: Stack, password: str
) -> None:
    assert run(stack, password).code == 0
    # cuff-sim stamps whole seconds and OpenEMR refuses a second reading at the same second
    # (see the next test); a live run takes longer than that anyway.
    time.sleep(1.1)
    second = run(stack, password)
    assert second.code == 0, second.text
    assert "an earlier reading names a SYN- patient" in second.line_of("S7")
    assert len(stack.observations()) == 2
    assert len(stack.env.openemr.observations) == 2


def test_two_readings_in_the_same_second_collide_at_openemr_and_say_so(
    stack: Stack, password: str
) -> None:
    """OpenEMR keys a vitals reading on code, Encounter and effectiveDateTime. A first share
    answering already-shared fails A10 with that explanation, not a bare outcome."""
    stack.tamper("POST", "/share", edit_json(lambda body: body.update(outcome="already-shared")))
    result = run(stack, password)
    assert result.status_of("A10") == "FAIL"
    assert "at this exact second" in result.line_of("A10")
    assert result.code == 1


def test_real_data_stack_refuses_part_a_writes_by_default(real_stack: Stack, password: str) -> None:
    result = run(real_stack, password)
    for check_id in PART_A_WRITES:
        assert result.status_of(check_id) == "SKIP", result.text
        assert "refused: the stack reports real data" in result.line_of(check_id)
        assert "--allow-real-data-writes" in result.line_of(check_id)
    assert _readings_and_shares(real_stack) == []
    assert real_stack.edge.received == []
    # Part B runs on a real stack; without log access it cannot, and says so (not a pass).
    assert result.status_of("B1") == "PASS"
    assert result.status_of("B2") == "FAIL"
    assert "Cloud Logging" in result.line_of("B2")
    assert result.code == 1


def test_real_data_writes_run_only_with_the_flag(real_stack: Stack, password: str) -> None:
    result = run(real_stack, password, "--allow-real-data-writes")
    assert result.status_of("A10") == "PASS", result.text
    assert result.status_of("A5") == "SKIP"  # no SYN- check on a real-data stack
    assert len(real_stack.env.openemr.observations) == 1


def test_a_non_synthetic_patient_on_a_synthetic_stack_stops_all_writes(
    tmp_path: Path, monkeypatch, key_and_cert, password: str
) -> None:
    """The stack says synthetic (no health-sync) but cuff-sim sends a non-SYN- patient: stop
    after the one reading, before the share and the negatives."""
    key, cert = key_and_cert
    stack = build_stack(tmp_path, monkeypatch, key, cert, patient_id=REAL_PATIENT)
    first = run(stack, password)
    assert first.status_of("A5") == "FAIL"
    assert first.code == 1
    assert not first.reported("A10")
    assert not first.reported("N1")
    assert _readings_and_shares(stack) == ["/cuff/readings"]
    assert stack.env.openemr.observations == {}
    # The next run sees that earlier reading's patient and writes nothing at all.
    before = len(stack.transport.requests)
    second = run(stack, password)
    assert second.status_of("S7") == "WARN"
    assert second.status_of("A1") == "SKIP"
    posts = [r for r in stack.transport.requests[before:] if r["method"] == "POST"]
    assert [r["path"] for r in posts] == ["/auth/login"]


def test_the_password_and_token_never_appear_in_the_output(
    stack: Stack, password: str, tmp_path: Path
) -> None:
    report = tmp_path / "report.json"
    result = run(stack, password, "--verbose", "--report-json", str(report))
    assert result.code == 0, result.text
    token = stack.transport.requests[1]["headers"]["Authorization"].removeprefix("Bearer ")
    assert len(token) > 20
    for text in (result.text, report.read_text()):
        assert password not in text
        assert token not in text
        assert "Authorization" not in text
    # The token was sent, though, on every request after the login, and only there.
    assert "Authorization" not in stack.transport.requests[0]["headers"]
    assert all(
        r["headers"].get("Authorization") == f"Bearer {token}" for r in stack.transport.requests[1:]
    )


def test_an_echoed_secret_is_redacted(stack: Stack, password: str) -> None:
    """A server that quotes the password or a bearer token in an error is still redacted."""
    stack.tamper(
        "GET",
        "/status",
        lambda r: Response(500, json.dumps({"x": f"{password} Bearer abc.def"}).encode(), {}),
    )
    leaky = TransportError(f"connection to {BASE} failed: {password}")

    class Echo:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, method, path, **kwargs):  # noqa: ANN001, ANN201
            self.calls += 1
            if self.calls == 1:
                return stack.transport.send(method, path, **kwargs)
            raise leaky

    result = run(stack, password, transport=Echo())
    assert result.status_of("X1") == "FAIL"
    assert password not in result.text
    assert "[redacted]" in result.line_of("X1")


def test_wrong_password_fails_the_login_and_stops(stack: Stack) -> None:
    result = run(stack, "not-the-synthetic-password-0000")
    assert result.status_of("S1") == "FAIL"
    assert "HTTP 401" in result.line_of("S1")
    assert result.code == 1
    assert len(stack.transport.requests) == 1


@pytest.mark.parametrize(
    ("env", "args", "message"),
    [
        ({"PULSEBRIDGE_BASE_URL": ""}, [], "must start with https://"),
        ({"PULSEBRIDGE_BASE_URL": "http://dashboard.example.test/api"}, [], "https://"),
        ({"PULSEBRIDGE_BASE_URL": "https://x.example.test/api?a=1"}, [], "no query"),
        ({"PULSEBRIDGE_REVIEWER_PASSWORD": ""}, [], "no password"),
        ({}, ["--sync-runs-from", "gcloud"], "PULSEBRIDGE_HEALTH_SYNC_SERVICE"),
    ],
)
def test_configuration_errors_exit_2_naming_the_setting(
    stack: Stack, password: str, env: dict, args: list[str], message: str
) -> None:
    result = run(stack, password, *args, env=env)
    assert result.code == 2
    assert message in result.text
    assert stack.transport.requests == []


def test_fail_on_skip_makes_skips_exit_3(stack: Stack, password: str) -> None:
    assert run(stack, password, "--fail-on-skip").code == 3


def test_report_json_lists_every_result_and_the_trail(
    stack: Stack, password: str, tmp_path: Path
) -> None:
    path = tmp_path / "r.json"
    assert run(stack, password, "--report-json", str(path)).code == 0
    report = json.loads(path.read_text())
    statuses = {r["id"]: r["status"] for r in report["results"]}
    assert statuses["A10"] == "PASS"
    assert statuses["B2"] == "SKIP"
    assert report["counts"]["skip"] == 4
    assert any(t["key"] == "FHIR Observation" for t in report["trail"])


def test_a_rate_limited_send_is_waited_out_and_retried(stack: Stack, password: str) -> None:
    seen = [0]

    def first_is_429(response: Response) -> Response:
        seen[0] += 1
        if seen[0] == 1:
            return Response(429, b'{"resourceType": "OperationOutcome"}', {"retry-after": "7"})
        return response

    stack.tamper("POST", "/cuff/readings", first_is_429)
    result = run(stack, password)
    assert result.code == 0, result.text
    assert 7 in result.sleeps
    assert "nothing was sent" in result.text


def test_password_from_secret_manager_uses_the_callers_gcloud(stack: Stack, password: str) -> None:
    calls = []

    def fake_gcloud(command, **kwargs):  # noqa: ANN001, ANN202
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=password + "\n", stderr="")

    def reader(project: str, prefix: str) -> str:
        return secret_manager_password(project, prefix, run=fake_gcloud)

    result = run(
        stack,
        "",
        "--password-source",
        "secret-manager",
        env={
            "PULSEBRIDGE_REVIEWER_PASSWORD": "",
            "CLOUDSDK_CORE_PROJECT": "example-project-1",
            "SECRET_NAME_PREFIX": "pb",
        },
        secret_reader=reader,
    )
    assert result.code == 0, result.text
    (command, kwargs) = calls[0]
    assert command[:5] == ["gcloud", "secrets", "versions", "access", "latest"]
    assert "--secret=pb-reviewer-password" in command
    assert "--project=example-project-1" in command
    assert kwargs["capture_output"] is True  # stdout is captured, never printed
    assert password not in result.text


def test_secret_manager_failure_is_a_clear_usage_error() -> None:
    def failing(command, **_):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="PERMISSION_DENIED\n")

    with pytest.raises(Exception, match="could not read pb-reviewer-password: PERMISSION_DENIED"):
        secret_manager_password("example-project-1", "pb", run=failing)
    with pytest.raises(Exception, match="not a valid name prefix"):
        secret_manager_password("example-project-1", "PB; rm", run=failing)
