"""The smoke command end to end against the in-process stack (real fhir-proxy, cuff-sim and
v2-to-fhir code over fakes): what it reports, what it writes, the real-data guard, secrets,
configuration and the exit status."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import time
from pathlib import Path

import pytest
from e2e_fake_stack import REAL_PATIENT, SECRET_PREFIX, TOKEN_ISSUER, Stack, build_stack
from e2e_runs import BASE, bp_component, edit_json, run
from fhir_proxy.share.testing import FakeOpenEmr
from fhir_proxy.testing import DEMO_LIMITS, PROJECT
from pulsebridge_smoke.first_sync import GcloudSyncRuns
from pulsebridge_smoke.http import Response, TransportError
from pulsebridge_smoke.session import SigningKeyError, secret_manager_signing_key

PART_A_WRITES = ("A1", "A10", "N1")


def _readings_and_shares(stack: Stack) -> list[str]:
    return [
        r["path"]
        for r in stack.transport.requests
        if r["method"] == "POST" and r["path"] in ("/cuff/readings", "/share")
    ]


def test_synthetic_stack_passes_with_part_b_skipped(stack: Stack, signing_key: bytes) -> None:
    result = run(stack, signing_key)
    assert result.code == 0, result.text
    for check_id in ("S1a", "S1b", "S1c", "S1", "S2", "S3", "S4", "S5", "S6", "S7"):
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


def test_what_one_run_adds_and_reports(stack: Stack, signing_key: bytes) -> None:
    result = run(stack, signing_key)
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
    stack: Stack, signing_key: bytes
) -> None:
    assert run(stack, signing_key).code == 0
    # cuff-sim stamps whole seconds and OpenEMR refuses a second reading at the same second
    # (see the next test); a live run takes longer than that anyway.
    time.sleep(1.1)
    second = run(stack, signing_key)
    assert second.code == 0, second.text
    assert "an earlier reading names a SYN- patient" in second.line_of("S7")
    assert len(stack.observations()) == 2
    assert len(stack.env.openemr.observations) == 2


def test_two_readings_in_the_same_second_collide_at_openemr_and_say_so(
    stack: Stack, signing_key: bytes
) -> None:
    """OpenEMR keys a vitals reading on code, Encounter and effectiveDateTime. A first share
    answering already-shared fails A10 with that explanation, not a bare outcome."""
    stack.tamper("POST", "/share", edit_json(lambda body: body.update(outcome="already-shared")))
    result = run(stack, signing_key)
    assert result.status_of("A10") == "FAIL"
    assert "at this exact second" in result.line_of("A10")
    assert result.code == 1


def test_real_data_stack_refuses_part_a_writes_by_default(
    real_stack: Stack, signing_key: bytes
) -> None:
    result = run(real_stack, signing_key)
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


def test_real_data_writes_run_only_with_the_flag(real_stack: Stack, signing_key: bytes) -> None:
    result = run(real_stack, signing_key, "--allow-real-data-writes")
    assert result.status_of("A10") == "PASS", result.text
    assert result.status_of("A5") == "SKIP"  # no SYN- check on a real-data stack
    assert len(real_stack.env.openemr.observations) == 1


def test_a_non_synthetic_patient_on_a_synthetic_stack_stops_all_writes(
    tmp_path: Path, monkeypatch, key_and_cert, signing_key: bytes
) -> None:
    """The stack says synthetic (no health-sync) but cuff-sim sends a non-SYN- patient: stop
    after the one reading, before the share and the negatives."""
    key, cert = key_and_cert
    stack = build_stack(tmp_path, monkeypatch, key, cert, patient_id=REAL_PATIENT)
    first = run(stack, signing_key)
    assert first.status_of("A5") == "FAIL"
    assert first.code == 1
    assert not first.reported("A10")
    assert not first.reported("N1")
    assert _readings_and_shares(stack) == ["/cuff/readings"]
    assert stack.env.openemr.observations == {}
    # The next run sees that earlier reading's patient and writes nothing at all.
    before = len(stack.transport.requests)
    second = run(stack, signing_key)
    assert second.status_of("S7") == "WARN"
    assert second.status_of("A1") == "SKIP"
    posts = [r for r in stack.transport.requests[before:] if r["method"] == "POST"]
    assert [r["path"] for r in posts] == ["/auth/session"]  # the junk-token check only


def _cookie_token(request: dict) -> str | None:
    cookie = request["headers"].get("Cookie", "")
    return cookie.removeprefix("__session=") if cookie.startswith("__session=") else None


def test_the_key_and_token_never_appear_in_the_output(
    stack: Stack, signing_key: bytes, tmp_path: Path
) -> None:
    report = tmp_path / "report.json"
    result = run(stack, signing_key, "--verbose", "--report-json", str(report))
    assert result.code == 0, result.text
    tokens = {_cookie_token(r) for r in stack.transport.requests} - {None}
    # Two sessions: the cuff path's, and the negatives' own (T46's per-session cuff limit).
    assert len(tokens) == 2
    assert all(len(token) > 100 for token in tokens)
    for text in (result.text, report.read_text()):
        assert signing_key.decode() not in text
        assert base64.urlsafe_b64encode(signing_key).decode().rstrip("=") not in text
        for token in tokens:
            assert token not in text
        assert "__session" not in text
    # No Authorization header is ever sent; the session goes as the cookie, and a POST always
    # carries the page's own origin.
    assert all("Authorization" not in r["headers"] for r in stack.transport.requests)
    posts = [r for r in stack.transport.requests if r["method"] == "POST"]
    assert posts and all(r["headers"]["Origin"] == "https://dashboard.example.test" for r in posts)


def test_no_session_goes_over_the_proxys_per_session_limits(
    stack: Stack, signing_key: bytes
) -> None:
    """T46: a session may send 3 cuff readings and make 2 shares in any 10 minutes. The fake
    stack runs the proxy with those production defaults (DEMO_LIMITS), so a run that put the
    reading and the three negatives in one session would be refused (429) at N4. The smoke test
    signs a second session for the negatives; each session stays inside its budget."""
    result = run(stack, signing_key)
    assert result.code == 0, result.text
    sends: dict[str, int] = {}
    shares: dict[str, int] = {}
    for r in stack.transport.requests:
        token = _cookie_token(r)
        if r["method"] == "POST" and r["path"] == "/cuff/readings":
            sends[token] = sends.get(token, 0) + 1
        if r["method"] == "POST" and r["path"] == "/share":
            shares[token] = shares.get(token, 0) + 1
    assert None not in sends and None not in shares
    assert sorted(sends.values()) == [1, 3], sends  # A1; then N1, N2 and N4
    assert list(shares.values()) == [2], shares  # A10 and A12, in the reading's session
    assert set(shares) <= set(sends) and len(sends) == 2
    for check in ("N1", "N2", "N4", "N5", "A10", "A12"):
        assert result.status_of(check) == "PASS", check


def test_the_session_routes_are_checked_before_the_session_is_used(
    stack: Stack, signing_key: bytes
) -> None:
    result = run(stack, signing_key)
    first = [(r["method"], r["path"], _cookie_token(r)) for r in stack.transport.requests[:4]]
    assert [(m, p) for m, p, _ in first] == [
        ("GET", "/auth/config"),
        ("POST", "/auth/session"),
        ("GET", "/fhir/metadata"),
        ("GET", "/fhir/metadata"),
    ]
    assert [c is None for _, _, c in first] == [True, True, True, False]
    junk = json.loads(stack.transport.requests[1]["body"])["recaptcha_token"]
    assert junk.startswith("pulsebridge-smoke-not-a-recaptcha-token-")
    # The junk token reached Fraud Defense (the fake) and was refused there.
    assert stack.env.harness.fraud.assessments == 1
    assert result.status_of("S1b") == "PASS"


def test_the_token_has_the_proxys_claims_and_signature(stack: Stack, signing_key: bytes) -> None:
    run(stack, signing_key)
    token = next(_cookie_token(r) for r in stack.transport.requests if _cookie_token(r))
    head, body, sig = token.split(".")

    def decode(part: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    assert decode(head) == {"alg": "HS256", "typ": "JWT"}
    claims = decode(body)
    assert set(claims) == {"iss", "aud", "iat", "exp", "jti"}
    assert claims["iss"] == claims["aud"] == TOKEN_ISSUER
    assert claims["exp"] - claims["iat"] == 900
    expected = hmac.new(signing_key, f"{head}.{body}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)) == expected


def test_an_echoed_secret_is_redacted(stack: Stack, signing_key: bytes) -> None:
    """A server that quotes the key or a token in an error is still redacted."""
    key_text = signing_key.decode()
    stack.tamper(
        "GET",
        "/status",
        lambda r: Response(500, json.dumps({"x": f"{key_text} Bearer abc.def"}).encode(), {}),
    )
    leaky = TransportError(f"connection to {BASE} failed: {key_text}")

    class Echo:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, method, path, **kwargs):  # noqa: ANN001, ANN201
            self.calls += 1
            if self.calls <= 4:
                return stack.transport.send(method, path, **kwargs)
            raise leaky

    result = run(stack, signing_key, transport=Echo())
    assert result.status_of("X1") == "FAIL"
    assert key_text not in result.text
    assert "[redacted]" in result.line_of("X1")


def test_a_key_the_stack_does_not_use_fails_the_session_and_stops(stack: Stack) -> None:
    result = run(stack, b"not-the-synthetic-signing-key-for-this-stack")
    assert result.status_of("S1") == "FAIL"
    assert "HTTP 401" in result.line_of("S1")
    assert "session-required" in result.line_of("S1")
    assert result.code == 1
    assert len(stack.transport.requests) == 4  # the three route checks and the refused session


def test_a_wrong_issuer_fails_the_session(stack: Stack, signing_key: bytes) -> None:
    result = run(stack, signing_key, "--token-issuer", "pulsebridge-fhir-proxy")
    assert result.status_of("S1") == "FAIL"
    assert "PULSEBRIDGE_TOKEN_ISSUER" in result.line_of("S1")


def test_an_issuer_built_from_the_secret_prefix_is_refused(
    stack: Stack, signing_key: bytes
) -> None:
    # The review's finding: SECRET_NAME_PREFIX ("pb") is not module phr's name_prefix
    # ("pulsebridge"), so an issuer derived from it must not be what the stack accepts.
    derived = f"pulsebridge-fhir-proxy:{PROJECT}:{SECRET_PREFIX}"
    assert derived != TOKEN_ISSUER
    result = run(stack, signing_key, env={"PULSEBRIDGE_TOKEN_ISSUER": derived})
    assert result.status_of("S1") == "FAIL"


def test_the_flag_wins_over_the_environment(stack: Stack, signing_key: bytes) -> None:
    result = run(
        stack,
        signing_key,
        "--token-issuer",
        TOKEN_ISSUER,
        env={"PULSEBRIDGE_TOKEN_ISSUER": "pulsebridge-fhir-proxy:other:other"},
    )
    assert result.status_of("S1") == "PASS", result.text


def test_a_junk_token_that_is_accepted_fails_s1b(stack: Stack, signing_key: bytes) -> None:
    stack.tamper("POST", "/auth/session", lambda r: Response(200, b'{"expires_at": "x"}', {}))
    result = run(stack, signing_key)
    assert result.status_of("S1b") == "FAIL"
    assert "HTTP 200" in result.line_of("S1b")
    assert result.code == 1


def test_a_busy_demo_skips_s1b(stack: Stack, signing_key: bytes) -> None:
    busy = json.dumps(
        {
            "resourceType": "OperationOutcome",
            "issue": [{"code": "throttled", "details": {"coding": [{"code": "demo-busy"}]}}],
        }
    ).encode()
    stack.tamper("POST", "/auth/session", lambda r: Response(503, busy, {}))
    result = run(stack, signing_key)
    assert result.status_of("S1b") == "SKIP"
    assert "demo-busy" in result.line_of("S1b")


def test_a_broken_config_route_or_open_gate_fails(stack: Stack, signing_key: bytes) -> None:
    stack.tamper("GET", "/auth/config", lambda r: Response(200, b"{}", {}))
    stack.tamper(
        "GET",
        "/fhir/metadata",
        lambda r: Response(200, b"{}", {}) if r.status == 401 else r,
    )
    result = run(stack, signing_key)
    assert result.status_of("S1a") == "FAIL"
    assert result.status_of("S1c") == "FAIL"


@pytest.mark.parametrize(
    ("env", "args", "message"),
    [
        ({"PULSEBRIDGE_BASE_URL": ""}, [], "set PULSEBRIDGE_BASE_URL"),
        ({"PULSEBRIDGE_BASE_URL": "http://dashboard.example.test/api"}, [], "https://"),
        ({"PULSEBRIDGE_BASE_URL": "https://x.example.test/api?a=1"}, [], "no query"),
        ({"SECRET_NAME_PREFIX": ""}, [], "no session: set CLOUDSDK_CORE_PROJECT and"),
        ({"CLOUDSDK_CORE_PROJECT": ""}, [], "no session: set CLOUDSDK_CORE_PROJECT and"),
        ({"SECRET_NAME_PREFIX": "PB; rm"}, [], "not a valid name prefix"),
        ({}, ["--token-issuer", "bad issuer"], "not a valid TOKEN_ISSUER"),
        ({"PULSEBRIDGE_TOKEN_ISSUER": "bad issuer"}, [], "not a valid TOKEN_ISSUER"),
        (
            {"PULSEBRIDGE_TOKEN_ISSUER": ""},
            [],
            "terraform -chdir=infra/envs/<env> output -raw token_issuer",
        ),
        ({}, ["--sync-runs-from", "gcloud"], "PULSEBRIDGE_HEALTH_SYNC_SERVICE"),
    ],
)
def test_configuration_errors_exit_2_naming_the_setting(
    stack: Stack, signing_key: bytes, env: dict, args: list[str], message: str
) -> None:
    result = run(stack, signing_key, *args, env=env)
    assert result.code == 2
    assert message in result.text
    assert stack.transport.requests == []


def test_fail_on_skip_makes_skips_exit_3(stack: Stack, signing_key: bytes) -> None:
    assert run(stack, signing_key, "--fail-on-skip").code == 3


def test_report_json_lists_every_result_and_the_trail(
    stack: Stack, signing_key: bytes, tmp_path: Path
) -> None:
    path = tmp_path / "r.json"
    assert run(stack, signing_key, "--report-json", str(path)).code == 0
    report = json.loads(path.read_text())
    statuses = {r["id"]: r["status"] for r in report["results"]}
    assert statuses["A10"] == "PASS"
    assert statuses["B2"] == "SKIP"
    assert report["counts"]["skip"] == 4
    assert any(t["key"] == "FHIR Observation" for t in report["trail"])


def test_a_rate_limited_send_is_waited_out_and_retried(stack: Stack, signing_key: bytes) -> None:
    seen = [0]

    def first_is_429(response: Response) -> Response:
        seen[0] += 1
        if seen[0] == 1:
            return Response(429, b'{"resourceType": "OperationOutcome"}', {"retry-after": "7"})
        return response

    stack.tamper("POST", "/cuff/readings", first_is_429)
    result = run(stack, signing_key)
    assert result.code == 0, result.text
    assert 7 in result.sleeps
    assert "nothing was sent" in result.text


def test_a_429_during_the_negatives_is_retried_in_a_fresh_session(
    stack: Stack, signing_key: bytes
) -> None:
    """cuff-sim answers 429 once during the negatives (N2). The proxy has already counted that
    attempt against the session (T46: every attempt counts), so retrying in the same session
    would make N4 the session's 4th cuff send in 10 minutes and the proxy would refuse it for
    600 s. The retry runs in a newly signed session instead, and no session goes over 3."""
    sends = [0]

    def third_send_is_429(response: Response) -> Response:
        sends[0] += 1  # A1, N1, then N2: the second send of the negatives
        if sends[0] == 3:
            return Response(429, b'{"resourceType": "OperationOutcome"}', {"retry-after": "5"})
        return response

    stack.tamper("POST", "/cuff/readings", third_send_is_429)
    result = run(stack, signing_key)
    assert result.code == 0, result.text
    assert 5 in result.sleeps and 600 not in result.sleeps
    per_session: dict[str | None, int] = {}
    for r in stack.transport.requests:
        if r["method"] == "POST" and r["path"] == "/cuff/readings":
            token = _cookie_token(r)
            per_session[token] = per_session.get(token, 0) + 1
    assert None not in per_session and max(per_session.values()) <= 3, per_session
    assert len(per_session) == 3, per_session  # the reading's, the negatives', the retry's
    for check in ("N2", "N3", "N4", "N5"):
        assert result.status_of(check) == "PASS", check


def test_the_fake_stack_runs_the_proxy_with_the_production_limits(stack: Stack) -> None:
    """The per-session tests above prove something only if the in-process proxy enforces T46's
    defaults, not the generous limits the proxy's own unit tests use."""
    config = stack.env.harness.config
    assert {name: getattr(config, name) for name in DEMO_LIMITS} == DEMO_LIMITS
    assert DEMO_LIMITS["cuff_sends_per_session_per_10_minutes"] == 3
    assert DEMO_LIMITS["shares_per_session_per_10_minutes"] == 2


def _gcloud_answer(key: bytes) -> str:
    """What ``gcloud ... --format=get(payload.data)`` prints: URL-safe base64, a newline."""
    return base64.urlsafe_b64encode(key).decode() + "\n"


def test_the_key_comes_from_secret_manager_with_the_callers_gcloud(
    stack: Stack, signing_key: bytes
) -> None:
    calls = []

    def fake_gcloud(command, **kwargs):  # noqa: ANN001, ANN202
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout=_gcloud_answer(signing_key), stderr=""
        )

    def reader(project: str, prefix: str) -> bytes:
        return secret_manager_signing_key(project, prefix, run=fake_gcloud)

    result = run(stack, b"", key_reader=reader)
    assert result.code == 0, result.text
    (command, kwargs) = calls[0]
    assert command[:5] == ["gcloud", "secrets", "versions", "access", "latest"]
    assert "--secret=pb-session-signing-key" in command
    assert "--project=example-project" in command
    assert "--format=get(payload.data)" in command  # raw bytes survive, no file
    assert kwargs["capture_output"] is True  # stdout is captured, never printed
    assert signing_key.decode() not in result.text


def test_a_binary_key_survives_the_round_trip() -> None:
    # 32 raw bytes, a NUL and a newline among them, whose URL-safe base64 has "-" and "_".
    raw = bytes(range(250, 256)) + bytes(range(26))

    def fake(command, **_):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 0, stdout=_gcloud_answer(raw), stderr="")

    assert secret_manager_signing_key("example-project-1", "pb", run=fake) == raw


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("", "empty latest version"),
        ("!!not base64!!\n", "not base64"),
        (_gcloud_answer(b"short-key"), "shorter than 32 bytes"),
    ],
)
def test_an_unusable_key_is_a_clear_usage_error(stdout: str, message: str) -> None:
    def fake(command, **_):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    with pytest.raises(SigningKeyError, match=message):
        secret_manager_signing_key("example-project-1", "pb", run=fake)


def test_an_unreadable_key_exits_2(stack: Stack) -> None:
    def failing(project: str, prefix: str) -> bytes:
        raise SigningKeyError("could not read pb-session-signing-key with gcloud (exit 1).")

    result = run(stack, b"", key_reader=failing)
    assert result.code == 2
    assert "could not read pb-session-signing-key" in result.text
    assert stack.transport.requests == []


def test_secret_manager_failure_is_a_clear_usage_error() -> None:
    stderr = (
        "ERROR: (gcloud.secrets.versions.access)\nPERMISSION_DENIED: Permission denied " + "x" * 900
    )

    def failing(command, **_):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    with pytest.raises(SigningKeyError) as caught:
        secret_manager_signing_key("example-project-1", "pb", run=failing)
    message = str(caught.value)
    assert message.startswith("could not read pb-session-signing-key with gcloud (exit 1).")
    assert "roles/secretmanager.secretAccessor" in message
    # gcloud's whole message, on one line, limited: not just its last line.
    assert "gcloud said: ERROR: (gcloud.secrets.versions.access) PERMISSION_DENIED" in message
    assert message.endswith(" [...]")
    assert len(message) < 1000


def test_gcloud_never_gets_the_old_password_variable(
    monkeypatch: pytest.MonkeyPatch, signing_key: bytes
) -> None:
    monkeypatch.setenv("PULSEBRIDGE_REVIEWER_PASSWORD", "synthetic-password-in-env-0001")
    monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "example-project-1")
    seen = []

    def fake(command, **kwargs):  # noqa: ANN001, ANN202
        seen.append(kwargs["env"])
        out = "[]" if command[1] == "logging" else _gcloud_answer(signing_key)
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

    secret_manager_signing_key("example-project-1", "pb", run=fake)
    GcloudSyncRuns("example-project-1", "pb-health-sync", run=fake).fetch()
    assert len(seen) == 2
    for env in seen:
        assert "PULSEBRIDGE_REVIEWER_PASSWORD" not in env
        assert env.get("CLOUDSDK_CORE_PROJECT") == "example-project-1"  # the rest is passed on


# A key whose text has every character repr() or json.dumps() escapes: backslash, newline, both
# quotes and non-ASCII. Synthetic, and over 32 bytes.
ESCAPED_KEY = "syn\\the\"tic\nKëy-für-tésts-'0001-padding".encode()


def test_an_echoed_key_is_redacted_in_every_escaped_form(stack: Stack, tmp_path: Path) -> None:
    stack.env.harness.key_file.write_bytes(ESCAPED_KEY)
    text = ESCAPED_KEY.decode()

    def echo(body: dict) -> None:
        # The detail is shown with !r; the apostrophe makes repr choose double quotes there.
        body["fhirStore"].update(reachable=False, detail=f"it's {text}")
        body["observationCounts"]["cuff"] = {"echo": text}  # shown as a dict

    stack.tamper("GET", "/status", edit_json(echo))
    report = tmp_path / "report.json"
    result = run(stack, ESCAPED_KEY, "--report-json", str(report))
    assert result.status_of("S1") == "PASS", result.text  # the proxy really used this key
    assert result.status_of("S4") == "FAIL"
    assert "[redacted]" in result.line_of("S4")
    assert "[redacted]" in result.line_of("S5")
    raw = report.read_text(encoding="utf-8")
    decoded = json.dumps(json.loads(raw), ensure_ascii=False)
    forms = {
        text,
        repr(text)[1:-1],
        repr("'\"" + text)[3:-1],
        json.dumps(text)[1:-1],
        json.dumps(text, ensure_ascii=False)[1:-1],
        "Këy-für",
        "K\\u00eby",
    }
    for out in (result.text, raw, decoded, json.loads(raw)["results"].__repr__()):
        for form in forms:
            assert form not in out, (form, out[:200])


def test_an_unexpected_answer_shape_is_a_fail_not_a_traceback(
    stack: Stack, signing_key: bytes
) -> None:
    class Boom:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, method, path, **kwargs):  # noqa: ANN001, ANN201
            self.calls += 1
            if self.calls <= 4:
                return stack.transport.send(method, path, **kwargs)
            raise RuntimeError(f"a library bug quoting {signing_key.decode()}")

    result = run(stack, signing_key, transport=Boom())
    assert result.code == 1
    assert result.status_of("X2") == "FAIL"
    assert "unexpected RuntimeError" in result.line_of("X2")
    assert signing_key.decode() not in result.text
    assert "a library bug" not in result.text  # the type only, never the text
