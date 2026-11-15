"""Tests for scripts/fhir-validate.sh that need neither the validator jar nor the network.

The script's decisions are tested here: reading the validator's report, applying the
expected-warnings list, choosing the exit code, verifying the jar's checksum, detecting an
unreachable terminology server, and argument handling.

The reports in fixtures/fhir_validator/ were recorded from real runs of the pinned HL7 FHIR
Validator over the (synthetic) examples, with the machine's paths replaced by @EXAMPLES@:

- real-expected-warnings-only.json: the committed examples; expected warnings only.
- derived-all-clean.json: the same report with every warning removed (no real run is clean).
- real-error-bad-ucum.json: a heart-rate component unit changed to "beats/mnute".
- real-new-warning-dangling-reference.json: a Bundle whose Observation.subject points at a
  urn:uuid that is not in the Bundle (the validator reports that as a warning).
- real-terminology-not-checked.json: a run with no terminology server (-tx n/a).
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "fhir-validate.sh"
EXPECTED = REPO_ROOT / "scripts" / "fhir-validate-expected-warnings.json"
EXAMPLES = REPO_ROOT / "libs" / "pulsebridge_fhir" / "examples"
FIXTURES = Path(__file__).parent / "fixtures" / "fhir_validator"

# The validator's own message when it cannot reach the terminology server (from a real run).
TX_UNREACHABLE_LOG = (
    "org.hl7.fhir.exceptions.FHIRException: Unable to connect to terminology server at "
    "https://tx.example.invalid. Use parameter '-tx n/a' to run without using terminology "
    "services to validate LOINC, SNOMED, ICD-X etc. Error = org.hl7.fhir.exceptions."
    "FHIRException: Error fetching the server's capability statement: Unexpected response code "
    "for CONNECT: 403\n"
)


def clean_env(**extra: str) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if k not in {"GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"}
    }
    env.update(extra)
    return env


def run_script(*args: str | Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=120,
        env=clean_env(**env),
    )


def run_function(snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Source the script (which does not run main) and run a snippet that calls its functions."""
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {snippet}', "bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env=clean_env(**env),
    )


def report_names(fixture: str) -> list[str]:
    data = json.loads((FIXTURES / fixture).read_text())
    names = []
    for entry in data["entry"]:
        [file_ext] = [
            e for e in entry["resource"]["extension"] if e["url"].endswith("operationoutcome-file")
        ]
        names.append(file_ext["valueString"].removeprefix("@EXAMPLES@/"))
    return names


def stage(tmp_path: Path, fixture: str, examples: Path | None = None) -> tuple[Path, Path]:
    """An examples directory holding the files the report names, and the report pointing at it."""
    if examples is None:
        examples = tmp_path / "examples"
        examples.mkdir()
        for name in report_names(fixture):
            (examples / name).write_text("{}\n")  # --report-only never reads the resources
    report = tmp_path / "report.json"
    text = (FIXTURES / fixture).read_text().replace("@EXAMPLES@", str(examples))
    report.write_text(text)
    return examples, report


def write_expected(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "expected.json"
    path.write_text(json.dumps({"expected_warnings": entries}))
    return path


def repo_expected_entries() -> list[dict]:
    return json.loads(EXPECTED.read_text())["expected_warnings"]


# --- Evaluating a report: severities, the expected-warnings list, exit codes ---------------------


def test_all_clean_run_passes(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "derived-all-clean.json")
    result = run_script("--report-only", report, examples)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 error/fatal; 0 warning(s)" in result.stdout
    assert "Result: PASS" in result.stdout
    assert "terminology checked by https://tx.fhir.org" in result.stdout


def test_committed_examples_report_passes_with_only_expected_warnings(tmp_path: Path) -> None:
    # The real report for the committed examples, against the real examples directory (the
    # script's default) and the committed expected-warnings file.
    _, report = stage(tmp_path, "real-expected-warnings-only.json", examples=EXAMPLES)
    result = run_script("--report-only", report)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "41 warning(s) (41 expected, 0 new)" in result.stdout
    # Warnings are visible, not hidden: each one is printed with its location and message.
    assert result.stdout.count("warning, expected (") == 41
    assert "http://loinc.org#96935-2" in result.stdout
    # Every entry in the expected list is still needed.
    assert "did not occur" not in result.stdout
    for entry in repo_expected_entries():
        assert f"expected {entry['id']}:" in result.stdout


def test_an_error_fails_the_run(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "real-error-bad-ucum.json")
    result = run_script("--report-only", report, examples)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "Unknown code 'beats/mnute' in the CodeSystem 'http://unitsofmeasure.org'" in result.stdout
    )
    assert "FAIL " in result.stdout
    assert "Result: FAIL, 1 ERROR/FATAL issue(s)" in result.stdout


def test_an_error_cannot_be_allow_listed(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "real-error-bad-ucum.json")
    # An entry matching the error's message id and text still does not excuse it.
    entries = [
        *repo_expected_entries(),
        {
            "id": "try-to-hide-the-error",
            "message_id": "Unknown_Code_in_Version",
            "text_contains": ["beats/mnute"],
            "reason": "an attempt to allow-list an error",
        },
        {
            "id": "the-new-ucum-warning",
            "message_id": "Terminology_TX_Confirm_5",
            "reason": "allow the accompanying warning, so only the error is left",
        },
    ]
    result = run_script(
        "--report-only", report, "--expected", write_expected(tmp_path, entries), examples
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 ERROR/FATAL" in result.stdout


def test_severity_key_is_refused_in_the_expected_file(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "derived-all-clean.json")
    entries = [{"id": "x", "severity": "error", "message_id": "Any", "reason": "no"}]
    result = run_script(
        "--report-only", report, "--expected", write_expected(tmp_path, entries), examples
    )
    assert result.returncode == 2
    assert "unknown keys ['severity']" in result.stderr


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"id": "x", "message_id": "M"}, "needs a 'reason'"),
        ({"id": "x", "message_id": "M", "reason": "  "}, "needs a 'reason'"),
        ({"id": "x", "reason": "r"}, "needs 'message_id' or 'text_contains'"),
        ({"message_id": "M", "reason": "r"}, "needs an 'id'"),
        ({"id": "x", "message_id": "M", "reason": "r", "when": "sometimes"}, "'when' must be"),
    ],
)
def test_malformed_expected_entries_are_refused(tmp_path: Path, entry: dict, message: str) -> None:
    examples, report = stage(tmp_path, "derived-all-clean.json")
    result = run_script(
        "--report-only", report, "--expected", write_expected(tmp_path, [entry]), examples
    )
    assert result.returncode == 2
    assert message in result.stderr


def test_a_new_warning_fails_the_run(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "real-new-warning-dangling-reference.json")
    result = run_script("--report-only", report, examples)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "WARNING, NEW (not in the expected list)" in result.stdout
    assert "URN reference is not locally contained within the bundle" in result.stdout
    assert "(41 expected, 1 new)" in result.stdout
    assert "Result: FAIL, 1 new warning(s)" in result.stdout


def test_allow_new_warnings_reports_but_passes(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "real-new-warning-dangling-reference.json")
    result = run_script("--report-only", report, "--allow-new-warnings", examples)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING, NEW (not in the expected list)" in result.stdout
    assert "Result: PASS (1 new warning(s) allowed)" in result.stdout


def test_removing_an_expected_entry_makes_its_warnings_new(tmp_path: Path) -> None:
    # The allow-list is what keeps the committed examples green: without the heart-rate minimum
    # entry, its three warnings are new and the run fails.
    _, report = stage(tmp_path, "real-expected-warnings-only.json", examples=EXAMPLES)
    entries = [e for e in repo_expected_entries() if e["id"] != "hr-minimum-component-code"]
    result = run_script("--report-only", report, "--expected", write_expected(tmp_path, entries))
    assert result.returncode == 1
    assert "(38 expected, 3 new)" in result.stdout


def test_an_unused_expected_entry_is_reported_but_does_not_fail(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "derived-all-clean.json")
    entries = [{"id": "gone", "message_id": "Something", "reason": "fixed long ago"}]
    result = run_script(
        "--report-only", report, "--expected", write_expected(tmp_path, entries), examples
    )
    assert result.returncode == 0
    assert "expected warning 'gone' did not occur" in result.stdout


def test_terminology_not_checked_fails_with_exit_3(tmp_path: Path) -> None:
    # A report in which codes were not checked must not pass when a server was meant to be used.
    examples, report = stage(tmp_path, "real-terminology-not-checked.json")
    result = run_script("--report-only", report, examples)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "NOT CHECKED: terminology failed" in result.stdout
    assert "could not be checked" in result.stdout


def test_offline_mode_passes_and_says_terminology_was_not_checked(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "real-terminology-not-checked.json")
    result = run_script("--report-only", report, "--offline", examples)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OFFLINE: codes, units and value sets NOT checked" in result.stdout
    assert "Result: PASS (structure only: terminology NOT checked)" in result.stdout


def test_a_file_missing_from_the_report_fails(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "derived-all-clean.json")
    nested = examples / "more"
    nested.mkdir()
    (nested / "extra.json").write_text("{}\n")  # found recursively, but not in the report
    result = run_script("--report-only", report, examples)
    assert result.returncode == 2
    assert "did not report on" in result.stderr
    assert "extra.json" in result.stderr


def test_github_annotations_for_errors(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "real-error-bad-ucum.json")
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", str(SCRIPT), "--report-only", str(report), str(examples)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**clean_env(), "GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)},
    )
    assert result.returncode == 1
    assert "::error file=" in result.stdout and "beats/mnute" in result.stdout
    assert "| 13 | 1 |" in summary.read_text()


# --- Checksum of the validator jar ---------------------------------------------------------------


def test_verify_sha256_accepts_the_right_digest(tmp_path: Path) -> None:
    jar = tmp_path / "stand-in.jar"
    jar.write_bytes(b"not really a jar\n")
    right = hashlib.sha256(jar.read_bytes()).hexdigest()
    wrong = hashlib.sha256(b"something else").hexdigest()
    ok = run_function(f'verify_sha256 "{jar}" {right}')
    assert ok.returncode == 0, ok.stderr
    bad = run_function(f'verify_sha256 "{jar}" {wrong}')
    assert bad.returncode == 1
    assert "checksum mismatch" in bad.stderr
    assert right in bad.stderr


def test_a_tampered_cached_jar_is_refused(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    version = run_script("--validator-version").stdout.split()[0]
    jar = cache / f"validator_cli-{version}.jar"
    jar.write_bytes(b"a swapped jar\n")
    result = run_function(f'ensure_validator "{cache}"')
    assert result.returncode == 2
    assert "does not match the pinned SHA-256" in result.stderr
    assert result.stdout == ""  # no jar path is handed to the caller
    assert jar.exists()  # left for the user to inspect, not silently replaced


def test_a_download_with_the_wrong_checksum_is_refused_and_removed(tmp_path: Path) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl is not installed")
    served = tmp_path / "served.jar"
    served.write_bytes(b"something else entirely\n")
    cache = tmp_path / "cache"
    result = run_function(f'VALIDATOR_URL="file://{served}"; ensure_validator "{cache}"')
    assert result.returncode == 2
    assert "downloaded jar does not match the pinned SHA-256" in result.stderr
    assert list(cache.iterdir()) == []  # neither the jar nor the partial download is kept


def test_validator_version_prints_the_pin() -> None:
    result = run_script("--validator-version")
    assert result.returncode == 0
    version, digest = result.stdout.split()
    script = SCRIPT.read_text()
    assert f'readonly VALIDATOR_VERSION="{version}"' in script
    assert f'readonly VALIDATOR_SHA256="{digest}"' in script
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


# --- Terminology server unreachable --------------------------------------------------------------


def test_unreachable_terminology_server_is_retried_then_fails_with_exit_3() -> None:
    # Port 9 (discard) on loopback: nothing answers. Two attempts, no delay.
    result = run_function(
        "probe_tx http://127.0.0.1:9", FHIR_TX_ATTEMPTS="2", FHIR_TX_RETRY_SECONDS="0"
    )
    assert result.returncode == 3
    assert "attempt 1 of 2" in result.stderr
    assert "unreachable after 2 attempts" in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        TX_UNREACHABLE_LOG,
        # Each of the validator's messages on its own (all from the same real failed run).
        "FHIRException: Unable to connect to terminology server at https://tx.example.invalid.\n",
        "org.hl7.fhir.exceptions.FHIRException: Error fetching the server's capability statement\n",
        "Caused by: org.hl7.fhir.exceptions.TerminologyServiceException: (see above)\n",
    ],
)
def test_validator_log_with_a_connection_failure_is_recognised(tmp_path: Path, line: str) -> None:
    failed = tmp_path / "failed.log"
    failed.write_text("  Loading packages\n" + line)
    assert run_function(f'tx_failure_in_log "{failed}"').returncode == 0


def test_validator_log_without_a_connection_failure_is_not_mistaken(tmp_path: Path) -> None:
    fine = tmp_path / "fine.log"
    fine.write_text("  Terminology server https://tx.fhir.org - Version Connected\nDone.\n")
    assert run_function(f'tx_failure_in_log "{fine}"').returncode != 0


# --- Arguments -----------------------------------------------------------------------------------


def test_help() -> None:
    result = run_script("--help")
    assert result.returncode == 0
    assert "Usage: scripts/fhir-validate.sh" in result.stdout
    assert "never use" in result.stdout and "real patient data" in result.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--no-such-option"], "unknown option --no-such-option"),
        (["--tx"], "--tx needs a value"),
        (["--expected"], "--expected needs a value"),
        (["--offline", "--tx", "https://tx.example.org"], "contradict"),
        (["--tx", "tx.fhir.org"], "must be an http(s) URL"),
        (["/nonexistent/examples"], "examples directory not found"),
        (["--expected", "/nonexistent.json"], "expected-warnings file not found"),
        (["--report-only", "/nonexistent-report.json"], "report not found"),
    ],
)
def test_bad_arguments_exit_2(args: list[str], message: str) -> None:
    result = run_script(*args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert message in result.stderr


def test_two_directories_are_refused(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    result = run_script(tmp_path / "a", tmp_path / "b")
    assert result.returncode == 2
    assert "only one examples directory" in result.stderr


def test_a_directory_without_json_files_is_refused(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a resource\n")
    result = run_script(tmp_path)
    assert result.returncode == 2
    assert "no *.json files under" in result.stderr


def test_default_directory_is_the_library_examples() -> None:
    result = run_function('list_json_files "$DEFAULT_EXAMPLES_DIR"')
    assert result.returncode == 0
    listed = [Path(line).name for line in result.stdout.splitlines()]
    assert listed == sorted(p.name for p in EXAMPLES.glob("*.json"))
    assert len(listed) == 13


def test_make_target_runs_the_script() -> None:
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", "-C", str(REPO_ROOT), "fhir-validate"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "scripts/fhir-validate.sh"
    check = subprocess.run(
        ["make", "--no-print-directory", "-n", "-C", str(REPO_ROOT), "check"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert "fhir-validate" not in check.stdout  # needs Java and network: not part of `check`
