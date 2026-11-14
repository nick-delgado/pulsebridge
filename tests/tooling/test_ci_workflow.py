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


@pytest.mark.parametrize("command", ["make lint", "make test", "make secret-scan"])
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
