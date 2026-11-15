"""Structural checks on the CI workflow.

GitHub only runs ci.yml after a push, so a broken workflow would otherwise go unnoticed until a
pull request quietly stops being checked. These tests parse the workflow and assert the
properties the project relies on: it runs on pull requests, it runs lint, tests and the secret
scan, and no failure is allowed to pass silently.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(workflow: dict) -> dict:
    # PyYAML follows YAML 1.1, where a bare `on` key is read as the boolean True.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on or {}


def all_steps(workflow: dict) -> list[tuple[str, dict]]:
    return [(job_id, step) for job_id, job in workflow["jobs"].items() for step in job["steps"]]


def steps_running(workflow: dict, command: str) -> list[tuple[str, dict]]:
    """Steps whose shell script runs `command` as a command (not inside a comment or echo)."""
    pattern = re.compile(rf"^\s*{re.escape(command)}(\s|$)", re.MULTILINE)
    return [(j, s) for j, s in all_steps(workflow) if pattern.search(s.get("run", ""))]


def test_runs_on_pull_requests(workflow: dict) -> None:
    on = triggers(workflow)
    assert "pull_request" in on
    # A branch or path filter would let some pull requests skip CI entirely.
    assert not (on["pull_request"] or {}), "pull_request trigger must not be filtered"


@pytest.mark.parametrize(
    "command",
    [
        "make lint",
        "make test",
        "make tf-validate",
        "make tf-test",
        "make secret-scan",
        # The dashboard job (T16).
        "make web-lint",
        "make web-test",
        "make web-build",
        "make web-browser-test",
    ],
)
def test_required_command_runs_as_a_step(workflow: dict, command: str) -> None:
    steps = steps_running(workflow, command)
    assert steps, f"no CI step runs `{command}`"
    for job_id, step in steps:
        # A step-level `if:` could skip the command on pull requests.
        assert "if" not in step, f"`{command}` in job {job_id} is conditional"
        assert "if" not in workflow["jobs"][job_id], f"job {job_id} is conditional"


def test_nothing_is_allowed_to_fail(workflow: dict) -> None:
    for job_id, job in workflow["jobs"].items():
        assert "continue-on-error" not in job, f"job {job_id} sets continue-on-error"
        for step in job["steps"]:
            name = step.get("name") or step.get("uses") or step.get("run")
            assert "continue-on-error" not in step, f"step {name!r} in {job_id} sets it"


def test_secret_scan_covers_the_pull_request_commits(workflow: dict) -> None:
    [(job_id, step)] = steps_running(workflow, "make secret-scan")
    job = workflow["jobs"][job_id]
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    # A shallow clone (the default, depth 1) has no base commit, so the range could not resolve.
    assert checkout.get("with", {}).get("fetch-depth") == 0
    # The range is base..head of the pull request, taken from the event payload.
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert 'SECRET_SCAN_RANGE="${BASE_SHA}..${HEAD_SHA}"' in step["run"]


def test_every_action_is_pinned_to_a_commit_sha(workflow: dict) -> None:
    for job_id, step in all_steps(workflow):
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), (
                f"{step['uses']} in {job_id} is not pinned to a full commit SHA"
            )


def test_gitleaks_version_matches_between_hook_and_ci() -> None:
    # The commit hook and the CI history scan should apply the same rules.
    makefile = (REPO_ROOT / "Makefile").read_text()
    [make_version] = re.findall(r"^GITLEAKS_VERSION := (\S+)$", makefile, re.MULTILINE)
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    [hook_repo] = [r for r in config["repos"] if r["repo"].endswith("/gitleaks")]
    assert hook_repo["rev"] == make_version


# --- FHIR validation workflow (T08) ------------------------------------------------------------
# .github/workflows/fhir-validate.yml is held to the same rules as ci.yml (every action pinned to
# a commit SHA, nothing allowed to fail, read-only token), and must actually run the validator
# script on pull requests whenever one of its inputs changes.

FHIR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "fhir-validate.yml"

# A change to any of these must trigger the validator, or a broken example could merge unchecked.
FHIR_VALIDATION_INPUTS = [
    "libs/pulsebridge_fhir/examples/observation-heart-rate.json",
    "libs/pulsebridge_fhir/examples/new-example.json",
    "libs/pulsebridge_fhir/pulsebridge_fhir/observations.py",
    "scripts/fhir-validate.sh",
    "scripts/fhir-validate-expected-warnings.json",
    ".github/workflows/fhir-validate.yml",
    "Makefile",
]


@pytest.fixture(scope="module")
def fhir_workflow() -> dict:
    return yaml.safe_load(FHIR_WORKFLOW.read_text())


def github_path_matches(pattern: str, path: str) -> bool:
    """GitHub's path filter glob: `**` matches across directories, `*` within one."""
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        else:
            regex += re.escape(pattern[i])
            i += 1
    return re.fullmatch(regex, path) is not None


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_fhir_workflow_triggers_on_every_validation_input(fhir_workflow: dict, event: str) -> None:
    on = triggers(fhir_workflow)
    assert event in on, f"fhir-validate.yml must run on {event}"
    config = on[event] or {}
    if event == "push":
        assert config.get("branches") == ["main"]
    assert "paths-ignore" not in config
    paths = config.get("paths")
    if paths is None:
        return  # unfiltered: every change runs it
    for path in FHIR_VALIDATION_INPUTS:
        assert any(github_path_matches(p, path) for p in paths), f"{event} would skip {path}"


def test_github_path_matcher() -> None:
    # The matcher above decides the previous test, so it must itself be right.
    assert github_path_matches("libs/pulsebridge_fhir/**", "libs/pulsebridge_fhir/a/b.json")
    assert not github_path_matches("libs/pulsebridge_fhir/*", "libs/pulsebridge_fhir/a/b.json")
    assert not github_path_matches("scripts/fhir-validate.sh", "scripts/fhir-validate.shx")


def test_fhir_workflow_runs_the_validator_unconditionally(fhir_workflow: dict) -> None:
    steps = steps_running(fhir_workflow, "make fhir-validate")
    assert steps, "no step runs `make fhir-validate`"
    for job_id, step in steps:
        assert "if" not in step, f"`make fhir-validate` in job {job_id} is conditional"
        assert "if" not in fhir_workflow["jobs"][job_id], f"job {job_id} is conditional"
        # CI must check terminology and fail on new warnings: no weakening switches.
        for weaker in ("--offline", "--allow-new-warnings", "FHIR_VALIDATE_ARGS"):
            assert weaker not in step["run"], f"CI passes {weaker}"
        assert "env" not in step, "the validator step must not override the script's settings"


def test_fhir_workflow_nothing_is_allowed_to_fail(fhir_workflow: dict) -> None:
    test_nothing_is_allowed_to_fail(fhir_workflow)


def test_fhir_workflow_actions_are_pinned_to_commit_shas(fhir_workflow: dict) -> None:
    test_every_action_is_pinned_to_a_commit_sha(fhir_workflow)


def test_fhir_workflow_is_least_privilege_and_bounded(fhir_workflow: dict) -> None:
    assert fhir_workflow["permissions"] == {"contents": "read"}
    for job_id, job in fhir_workflow["jobs"].items():
        assert "permissions" not in job, f"job {job_id} widens the token"
        assert isinstance(job.get("timeout-minutes"), int), f"job {job_id} has no timeout"


def test_fhir_workflow_sets_up_java_and_caches_by_validator_version(fhir_workflow: dict) -> None:
    [(job_id, _)] = steps_running(fhir_workflow, "make fhir-validate")
    steps = fhir_workflow["jobs"][job_id]["steps"]
    uses = [s.get("uses", "").split("@")[0] for s in steps]
    assert "actions/setup-java" in uses
    java = steps[uses.index("actions/setup-java")]
    assert java["with"]["java-version"], "Java version must be pinned"
    cache = steps[uses.index("actions/cache")]
    # The key must change when the pinned validator version changes.
    assert "steps.pin.outputs.version" in cache["with"]["key"]
    [pin_step] = [s for s in steps if s.get("id") == "pin"]
    assert "scripts/fhir-validate.sh --validator-version" in pin_step["run"]
    # Only the checksummed jar is cached, at the script's default cache directory: the FHIR
    # package cache has no pinned checksum and would freeze the "latest" terminology packages.
    script = (REPO_ROOT / "scripts" / "fhir-validate.sh").read_text()
    assert "${XDG_CACHE_HOME:-$HOME/.cache}/pulsebridge/fhir-validator" in script
    assert 'jar="$cache/validator_cli-${VALIDATOR_VERSION}.jar"' in script
    assert cache["with"]["path"] == (
        "~/.cache/pulsebridge/fhir-validator/validator_cli-${{ steps.pin.outputs.version }}.jar"
    )
    # All steps that matter come before the validator runs.
    order = [s.get("id") or s.get("uses", "").split("@")[0] or s.get("run") for s in steps]
    assert order.index("actions/setup-java") < order.index("make fhir-validate")
    assert order.index("actions/cache") < order.index("make fhir-validate")


def test_fhir_workflow_keeps_its_weekly_schedule(fhir_workflow: dict) -> None:
    # The schedule is what notices drift no commit causes (terminology server, new packages).
    on = triggers(fhir_workflow)
    crons = [entry["cron"] for entry in on.get("schedule") or []]
    assert crons, "fhir-validate.yml must keep a schedule"
    for cron in crons:
        fields = cron.split()
        assert len(fields) == 5, f"bad cron {cron!r}"
        assert fields[2] == "*" and fields[3] == "*", f"{cron!r} is not at least weekly"
        assert fields[4] != "*" or fields[1] != "*", f"{cron!r} runs more than daily"
