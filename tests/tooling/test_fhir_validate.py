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
import http.server
import json
import os
import shutil
import signal
import subprocess
import threading
import time
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
            "locations": ["Observation.component[0].value.ofType(Quantity).code"],
            "reason": "an attempt to allow-list an error",
        },
        {
            "id": "the-new-ucum-warning",
            "message_id": "Terminology_TX_Confirm_5",
            "locations": ["Observation.component[0].value.ofType(Quantity)"],
            "reason": "allow the accompanying warning, so only the error is left",
        },
    ]
    result = run_script(
        "--report-only", report, "--expected", write_expected(tmp_path, entries), examples
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 ERROR/FATAL" in result.stdout
    assert "(42 expected, 0 new)" in result.stdout  # the warning entry did apply; the error not


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
        ({"id": "x", "message_id": "M", "locations": ["P"]}, "needs a 'reason'"),
        ({"id": "x", "message_id": "M", "locations": ["P"], "reason": "  "}, "needs a 'reason'"),
        ({"id": "x", "locations": ["P"], "reason": "r"}, "needs 'message_id' or 'text_contains'"),
        ({"message_id": "M", "locations": ["P"], "reason": "r"}, "needs an 'id'"),
        (
            {"id": "x", "message_id": "M", "locations": ["P"], "reason": "r", "when": "sometimes"},
            "'when' must be",
        ),
        ({"id": "x", "message_id": "M", "reason": "r"}, "needs 'locations'"),
        ({"id": "x", "message_id": "M", "reason": "r", "locations": []}, "needs 'locations'"),
        (
            {"id": "x", "message_id": "M", "reason": "r", "locations": "Patient"},
            "needs 'locations'",
        ),
        (
            {"id": "x", "message_id": "M", "reason": "r", "location_contains": "Patient"},
            "unknown keys ['location_contains']",
        ),
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
    entries = [
        {"id": "gone", "message_id": "Something", "locations": ["Patient"], "reason": "fixed"}
    ]
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
    # Differs from the right digest in the LAST character only, so a comparison that checks a
    # prefix (or stops early) would wrongly accept it.
    wrong = right[:-1] + ("0" if right[-1] != "0" else "1")
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
    result = run_function(
        f'rc=0; ensure_validator "{cache}" || rc=$?; echo "jar=[$VALIDATOR_JAR]"; exit $rc'
    )
    assert result.returncode == 2
    assert "does not match the pinned SHA-256" in result.stderr
    assert "jar=[]" in result.stdout  # no jar path is handed to the caller
    assert jar.exists()  # left for the user to inspect, not silently replaced


def test_a_download_with_the_wrong_checksum_is_refused_and_removed(tmp_path: Path) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl is not installed")
    served = tmp_path / "served.jar"
    served.write_bytes(b"something else entirely\n")
    cache = tmp_path / "cache"
    result = run_function(
        f'CURL_PROTO="=file"; VALIDATOR_URL="file://{served}"; rc=0; '
        f'ensure_validator "{cache}" || rc=$?; echo "jar=[$VALIDATOR_JAR]"; exit $rc'
    )
    assert result.returncode == 2
    assert "downloaded jar does not match the pinned SHA-256" in result.stderr
    assert "jar=[]" in result.stdout
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
        "probe_tx https://127.0.0.1:9", FHIR_TX_ATTEMPTS="2", FHIR_TX_RETRY_SECONDS="0"
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
        (["--tx", "tx.fhir.org"], "must be an https URL"),
        (["--tx", "http://tx.fhir.org"], "must be an https URL"),
        (["--tx", "https://tx.fhir.org/r4"], "without /r4"),
        (["--tx", "https://tx.fhir.org/r4/"], "without /r4"),
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
    (tmp_path / "empty-subdirectory").mkdir()
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


# --- Expected entries are tied to resource type, element and the exact code ----------------------


def mutated_report(tmp_path: Path, mutate) -> tuple[Path, Path]:
    """The committed examples' real report, with one issue changed by `mutate(by_file)`."""
    data = json.loads((FIXTURES / "real-expected-warnings-only.json").read_text())
    by_file = {}
    for entry in data["entry"]:
        oo = entry["resource"]
        [name] = [e["valueString"] for e in oo["extension"] if e["url"].endswith("-file")]
        by_file[name.removeprefix("@EXAMPLES@/")] = oo
    mutate(by_file)
    examples = tmp_path / "examples"
    examples.mkdir()
    for name in by_file:
        (examples / name).write_text("{}\n")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(data).replace("@EXAMPLES@", str(examples)))
    return examples, report


def issue_with(oo: dict, message_id: str) -> dict:
    return next(
        i
        for i in oo["issue"]
        if any(e.get("valueCode") == message_id for e in i.get("extension", []))
    )


def move_pt_warning_to_practitioner(by_file: dict) -> None:
    issue_with(by_file["patient.json"], "Terminology_TX_NoValid_2_CC")["expression"] = [
        "Practitioner.identifier[0].type"
    ]


def hr_minimum_code_on_a_blood_pressure_component(by_file: dict) -> None:
    # The same 96935-2 "not in Vital Signs" warning, on a third component of the BP Observation.
    issue = issue_with(by_file["observation-heart-rate.json"], "Terminology_TX_NoValid_2_CC")
    by_file["observation-heart-rate.json"]["issue"].remove(issue)
    issue["expression"] = ["Observation.component[2].code"]
    by_file["observation-blood-pressure.json"]["issue"].append(issue)


def typo_in_the_local_code_system(by_file: dict) -> None:
    issue = issue_with(by_file["provenance-heart-rate.json"], "UNKNOWN_CODESYSTEM")
    issue["details"]["text"] = issue["details"]["text"].replace(
        "'urn:pulsebridge:recording-method'", "'urn:pulsebridge:recording-methods'"
    )


def hr_minimum_code_with_a_longer_code(by_file: dict) -> None:
    issue = issue_with(by_file["observation-heart-rate.json"], "Terminology_TX_NoValid_2_CC")
    issue["details"]["text"] = issue["details"]["text"].replace("#96935-2)", "#96935-20)")


def hr_maximum_warning_on_a_coding_inside_the_component(by_file: dict) -> None:
    # A location that CONTAINS an expected one ("Observation.component[1].code") is not it.
    issues = [
        i
        for i in by_file["observation-heart-rate.json"]["issue"]
        if "#55426-1)" in i["details"]["text"]
    ]
    issues[0]["expression"] = ["Observation.component[1].code.coding[0]"]


@pytest.mark.parametrize(
    "mutate",
    [
        move_pt_warning_to_practitioner,
        hr_maximum_warning_on_a_coding_inside_the_component,
        hr_minimum_code_on_a_blood_pressure_component,
        typo_in_the_local_code_system,
        hr_minimum_code_with_a_longer_code,
    ],
)
def test_look_alike_warnings_are_new(tmp_path: Path, mutate) -> None:
    examples, report = mutated_report(tmp_path, mutate)
    result = run_script("--report-only", report, examples)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "41 warning(s) (40 expected, 1 new)" in result.stdout
    assert "Result: FAIL, 1 new warning(s)" in result.stdout


def test_in_bundle_and_standalone_locations_match_the_same_entry(tmp_path: Path) -> None:
    # Unchanged report: the Bundle form "Bundle.entry[2].resource/*Observation/null*/.component[0]
    # .code" and the standalone "Observation.component[0].code" both match hr-minimum.
    examples, report = mutated_report(tmp_path, lambda by_file: None)
    result = run_script("--report-only", report, examples)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "expected hr-minimum-component-code: 3 time(s)" in result.stdout


@pytest.mark.parametrize("severity", ["Warning", "warn", None])
def test_an_unknown_severity_counts_as_an_error(tmp_path: Path, severity: str | None) -> None:
    def mutate(by_file: dict) -> None:
        issue = issue_with(by_file["patient.json"], "Terminology_TX_NoValid_2_CC")
        if severity is None:
            del issue["severity"]
        else:
            issue["severity"] = severity

    examples, report = mutated_report(tmp_path, mutate)
    result = run_script("--report-only", report, examples)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"ERROR (unknown severity {severity!r})" in result.stdout
    assert "Result: FAIL, 1 ERROR/FATAL issue(s)" in result.stdout


def test_a_terminology_only_entry_does_not_apply_offline(tmp_path: Path) -> None:
    # The PT warning also appears offline. Marked "when": "terminology", the entry must not
    # apply to an offline run, so those four warnings are new there.
    examples, report = stage(tmp_path, "real-terminology-not-checked.json")
    entries = repo_expected_entries()
    for entry in entries:
        if entry["id"] == "patient-identifier-type-pt":
            entry["when"] = "terminology"
    expected = write_expected(tmp_path, entries)
    result = run_script("--report-only", report, "--offline", "--expected", expected, examples)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "(31 expected, 4 new)" in result.stdout
    # With "when": "always" (the committed file) the same offline run passes.
    assert run_script("--report-only", report, "--offline", examples).returncode == 0


def test_annotation_file_property_is_escaped(tmp_path: Path) -> None:
    examples, report = stage(tmp_path, "real-error-bad-ucum.json")
    odd = "obs,hr:1.json"
    (examples / "observation-heart-rate.json").rename(examples / odd)
    report.write_text(report.read_text().replace("/observation-heart-rate.json", "/" + odd))
    result = subprocess.run(
        ["bash", str(SCRIPT), "--report-only", str(report), str(examples)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**clean_env(), "GITHUB_ACTIONS": "true"},
    )
    assert result.returncode == 1
    annotations = [line for line in result.stdout.splitlines() if line.startswith("::error ")]
    assert annotations
    assert all("obs%2Chr%3A1.json" in line for line in annotations)
    assert not any("obs,hr:1.json" in line for line in annotations)


# --- What is in the examples directory ---------------------------------------------------------


def test_upper_case_json_extension_is_validated(tmp_path: Path) -> None:
    (tmp_path / "UPPER.JSON").write_text("{}\n")
    (tmp_path / "lower.json").write_text("{}\n")
    result = run_function(f'list_json_files "{tmp_path}"')
    assert result.returncode == 0, result.stderr
    assert [Path(line).name for line in result.stdout.splitlines()] == ["UPPER.JSON", "lower.json"]


@pytest.mark.parametrize(
    "kind", ["symlink-to-json", "symlink-to-dir", "not-json", "hidden-file", "named-pipe"]
)
def test_entries_that_would_not_be_validated_are_refused(tmp_path: Path, kind: str) -> None:
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "patient.json").write_text("{}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "device.json").write_text("{}\n")
    offender = {
        "symlink-to-json": "linked.json",
        "symlink-to-dir": "linked-dir",
        "not-json": "observation.json.bak",
        "hidden-file": ".DS_Store",
        "named-pipe": "pipe.json",
    }[kind]
    if kind == "named-pipe":
        os.mkfifo(examples / offender)  # named *.json, but not a regular file
    elif kind == "symlink-to-json":
        (examples / offender).symlink_to(elsewhere / "device.json")
    elif kind == "symlink-to-dir":
        (examples / offender).symlink_to(elsewhere)
    else:
        (examples / offender).write_text("x\n")
    result = run_script(examples)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "would not be validated" in result.stderr
    assert offender in result.stderr


# --- main, with a stub `java` (no jar, no network) -----------------------------------------------

STUB_JAVA = """#!/usr/bin/env python3
# Stand-in for `java`: records its arguments, then behaves as STUB_MODE says.
import json, os, sys
args = sys.argv[1:]
if args == ["-version"]:
    print('openjdk version "21.0.1" 2026-01-01', file=sys.stderr)
    sys.exit(0)
with open(os.environ["STUB_ARGS"], "w") as fh:
    json.dump(args, fh)
mode = os.environ["STUB_MODE"]
out = args[args.index("-output") + 1]
files = [a for a in args if a.lower().endswith(".json") and a != out]
if mode == "tx-fail":
    print("org.hl7.fhir.exceptions.FHIRException: Unable to connect to terminology server at x")
    sys.exit(1)
if mode == "crash":
    print("Exception in thread main java.lang.OutOfMemoryError: Java heap space")
    sys.exit(1)
report = {"resourceType": "Bundle", "type": "collection", "entry": [
    {"resource": {"resourceType": "OperationOutcome", "issue": [], "extension": [
        {"url": "http://hl7.org/fhir/StructureDefinition/operationoutcome-file",
         "valueString": f}]}} for f in files]}
with open(out, "w") as fh:
    json.dump(report, fh)
sys.exit(1 if mode == "clean-rc1" else 0)
"""


def run_main_with_stub_java(
    tmp_path: Path, mode: str, *args: str
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text(STUB_JAVA)
    java.chmod(0o755)
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "patient.json").write_text("{}\n")
    # A TMPDIR with a single quote and a space: the cleanup must still remove the work dir.
    tmpdir = tmp_path / "it's tmp"
    tmpdir.mkdir()
    stub_args = tmp_path / "stub-args.json"
    snippet = (
        'ensure_validator() { VALIDATOR_JAR="/stub/validator.jar"; }; '
        'probe_tx() { echo "probed $1" >&2; return 0; }; '
        'main "$@"'
    )
    result = subprocess.run(
        ["bash", "-c", f'source "$1"; shift; {snippet}', "bash", str(SCRIPT), *args, str(examples)],
        capture_output=True,
        text=True,
        timeout=60,
        env=clean_env(
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            STUB_MODE=mode,
            STUB_ARGS=str(stub_args),
            TMPDIR=str(tmpdir),
        ),
    )
    recorded = json.loads(stub_args.read_text()) if stub_args.exists() else []
    return result, recorded, tmpdir


def test_main_passes_r4_terminology_and_display_options(tmp_path: Path) -> None:
    result, java_args, tmpdir = run_main_with_stub_java(tmp_path, "clean")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Result: PASS" in result.stdout
    assert java_args[java_args.index("-version") + 1] == "4.0.1"
    assert java_args[java_args.index("-tx") + 1] == "https://tx.fhir.org"
    assert java_args[java_args.index("-check-display") + 1] == "Check"
    assert "-jar" in java_args and "/stub/validator.jar" in java_args
    assert list(tmpdir.iterdir()) == []  # the work directory was removed


def test_main_offline_passes_tx_na(tmp_path: Path) -> None:
    result, java_args, _ = run_main_with_stub_java(tmp_path, "clean", "--offline")
    assert result.returncode == 0, result.stdout + result.stderr
    assert java_args[java_args.index("-tx") + 1] == "n/a"
    assert "probed" not in result.stderr  # no terminology probe offline


def test_main_normalises_trailing_slashes_on_tx(tmp_path: Path) -> None:
    result, java_args, _ = run_main_with_stub_java(
        tmp_path, "clean", "--tx", "https://tx.example.org//"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probed https://tx.example.org\n" in result.stderr
    assert java_args[java_args.index("-tx") + 1] == "https://tx.example.org"


def test_main_no_report_with_terminology_failure_in_log_exits_3(tmp_path: Path) -> None:
    result, _, tmpdir = run_main_with_stub_java(tmp_path, "tx-fail")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "could not reach the terminology server" in result.stderr
    assert list(tmpdir.iterdir()) == []


def test_main_no_report_after_a_crash_exits_2(tmp_path: Path) -> None:
    result, _, _ = run_main_with_stub_java(tmp_path, "crash")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "OutOfMemoryError" in result.stderr  # the log tail is shown
    assert "exited with status 1 and wrote no report" in result.stderr


def test_main_validator_failure_with_a_clean_report_is_not_a_pass(tmp_path: Path) -> None:
    result, _, _ = run_main_with_stub_java(tmp_path, "clean-rc1")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "exited with status 1 although its report has no errors" in result.stderr


# --- An interrupted download leaves no partial file -----------------------------------------------


def test_interrupted_download_removes_the_partial_file(tmp_path: Path) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl is not installed")
    fifo = tmp_path / "slow.jar"
    os.mkfifo(fifo)
    cache = tmp_path / "cache"
    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            'source "$1"; install_cleanup_traps; CURL_PROTO="=file"; '
            f'VALIDATOR_URL="file://{fifo}"; ensure_validator "{cache}"',
            "bash",
            str(SCRIPT),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=clean_env(),
    )
    try:
        with open(fifo, "wb") as writer:  # blocks until curl opens the pipe
            writer.write(b"partial jar bytes")
            writer.flush()
            deadline = time.monotonic() + 20
            while not list(cache.glob("*.part.*")) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert list(cache.glob("*.part.*")), "curl never started writing the partial file"
            proc.send_signal(signal.SIGTERM)  # handled by bash once curl returns
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 143, proc.stderr.read() if proc.stderr else ""
    assert list(cache.iterdir()) == []  # no .part file, no jar


# --- HTTPS only ----------------------------------------------------------------------------------


def test_download_refuses_a_non_https_url(tmp_path: Path) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl is not installed")
    served = tmp_path / "served.jar"
    served.write_bytes(b"anything\n")
    cache = tmp_path / "cache"
    # Default CURL_PROTO ("=https"): curl must refuse to fetch a file:// URL at all.
    result = run_function(f'VALIDATOR_URL="file://{served}"; ensure_validator "{cache}"')
    assert result.returncode == 2
    assert "could not download" in result.stderr
    assert list(cache.iterdir()) == []


def test_terminology_probe_refuses_plain_http(tmp_path: Path) -> None:
    # A plain-HTTP server that WOULD answer /r4/metadata: the probe must still not accept it.
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        result = run_function(
            f"probe_tx {url}",
            FHIR_TX_ATTEMPTS="1",
            FHIR_TX_RETRY_SECONDS="0",
            NO_PROXY="127.0.0.1",
            no_proxy="127.0.0.1",
        )
    finally:
        server.shutdown()
    assert result.returncode == 3, result.stderr
    assert "unreachable after 1 attempts" in result.stderr
