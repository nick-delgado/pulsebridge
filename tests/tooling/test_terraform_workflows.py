"""Static checks on the Terraform CI/CD workflows (T26), .github/workflows/terraform-*.yml.

GitHub only runs a workflow after it is pushed, and these ones reach real projects, so the rules
they rely on are checked here instead of being discovered in production:

- every action is pinned to a full commit SHA;
- the GITHUB_TOKEN and each job get the least permissions; a cloud credential (`id-token:
  write`) and the right to write to a pull request never meet in one job;
- no key material: no `secrets.*` at all, no `credentials_json`;
- every job that authenticates to Google is gated on its repository variables (so the workflows
  skip cleanly until the user configures them), runs only from main or, for pull requests, only
  from the same repository (never a fork);
- prod is reachable only from jobs in the protected GitHub Environments, and its apply compares
  the reviewed plan's digest;
- applies and destroys of one environment share one non-cancelling concurrency group;
- the page is deployed after every apply; plans are never uploaded as artifacts.

actionlint (a pre-commit hook) separately checks the YAML and expression syntax.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
NAMES = ["checks", "plan", "apply", "destroy-dev"]
TF_CI = "python3 infra/envs/ci/tf_ci.py"
AUTH_ACTION = "google-github-actions/auth"

# The repository variables each environment's jobs read (infra/envs/README.md, "CI/CD").
ENV_VARS = {
    env: [f"{env.upper()}_GCP_WORKLOAD_IDENTITY_PROVIDER", f"{env.upper()}_GCP_DEPLOYER_SA",
          f"{env.upper()}_TF_STATE_BUCKET"]
    for env in ("dev", "prod")
}  # fmt: skip
ALLOWED_JOB_PERMISSIONS = {
    ("contents", "read"),
    ("id-token", "write"),
    ("pull-requests", "write"),
    ("actions", "read"),
}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / f"terraform-{name}.yml").read_text())


def text(name: str) -> str:
    return (WORKFLOWS / f"terraform-{name}.yml").read_text()


@pytest.fixture(scope="module", params=NAMES)
def name(request: pytest.FixtureRequest) -> str:
    return request.param


def triggers(workflow: dict) -> dict:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as True
    return {on: None} if isinstance(on, str) else dict(on or {})


def all_jobs() -> list[tuple[str, str, dict]]:
    return [(n, job_id, job) for n in NAMES for job_id, job in load(n)["jobs"].items()]


def steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def auth_steps(job: dict) -> list[dict]:
    return [s for s in steps(job) if s.get("uses", "").startswith(AUTH_ACTION + "@")]


def runs(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in steps(job))


def environment_of(job: dict) -> str | None:
    env = job.get("environment")
    return env.get("name") if isinstance(env, dict) else env


def top_level_conjuncts(expression: str) -> list[str]:
    """Split an `if:` expression on its top-level `&&`. Refuses a top-level `||`, which could make
    a gate optional; `||` inside parentheses is allowed."""
    expression = " ".join(expression.split())
    parts, depth, current, i = [], 0, "", 0
    while i < len(expression):
        c = expression[i]
        depth += c == "("
        depth -= c == ")"
        if depth == 0 and expression.startswith("&&", i):
            parts.append(current.strip())
            current, i = "", i + 2
            continue
        if depth == 0 and expression.startswith("||", i):
            raise AssertionError(f"top-level || in {expression!r}")
        current += c
        i += 1
    parts.append(current.strip())
    return parts


def tf_ci_envs(job: dict) -> set[str]:
    return set(re.findall(r"tf_ci\.py \S+ --env (\w+)", runs(job)))


# --- every workflow ------------------------------------------------------------------------------


def test_all_four_workflows_exist() -> None:
    found = sorted(p.name for p in WORKFLOWS.glob("terraform-*.yml"))
    assert found == sorted(f"terraform-{n}.yml" for n in NAMES)


def test_every_action_is_pinned_to_a_commit_sha_with_its_tag(name: str) -> None:
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(.+)$", text(name), re.MULTILINE)
    assert uses, f"terraform-{name}.yml uses no action at all?"
    for line in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+(\.\d+)*", line.strip()), (
            f"terraform-{name}.yml: {line.strip()!r} is not pinned to a full commit SHA "
            "with the tag in a trailing comment"
        )


def test_no_key_material_or_secrets(name: str) -> None:
    body = text(name)
    assert "secrets." not in body, "no secrets: authentication is Workload Identity Federation only"
    assert "credentials_json" not in body
    assert "GOOGLE_CREDENTIALS" not in body
    assert "service_account_key" not in body.lower()


def test_no_dangerous_triggers(name: str) -> None:
    on = triggers(load(name))
    # pull_request_target and workflow_run run with the repository's permissions on events a
    # fork can cause.
    assert not {"pull_request_target", "workflow_run"} & set(on)


def test_token_permissions_are_minimal(name: str) -> None:
    workflow = load(name)
    assert workflow["permissions"] in ({}, {"contents": "read"}), "top-level token too broad"
    for job_id, job in workflow["jobs"].items():
        perms = job.get("permissions")
        assert isinstance(perms, dict), f"{job_id} must list its permissions"
        assert set(perms.items()) <= ALLOWED_JOB_PERMISSIONS, f"{job_id}: {perms}"
        has_auth = bool(auth_steps(job))
        # A Google credential only where the job authenticates, and then not the right to write
        # to the pull request (the comment job has no cloud credential).
        assert (perms.get("id-token") == "write") == has_auth, job_id
        if has_auth:
            assert "pull-requests" not in perms, f"{job_id} mixes a credential and PR write"


def test_nothing_is_allowed_to_fail_and_everything_is_bounded(name: str) -> None:
    for job_id, job in load(name)["jobs"].items():
        assert "continue-on-error" not in job, job_id
        assert isinstance(job.get("timeout-minutes"), int), f"{job_id} has no timeout"
        for step in steps(job):
            assert "continue-on-error" not in step, job_id


def test_checkout_never_keeps_the_token(name: str) -> None:
    for job_id, job in load(name)["jobs"].items():
        for step in steps(job):
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, job_id


def test_plans_are_never_uploaded(name: str) -> None:
    body = text(name)
    assert "upload-artifact" not in body, "a plan or its text must never be uploaded"
    assert "cache@" not in body


def test_terraform_version_matches_ci(name: str) -> None:
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    assert load(name)["env"]["TERRAFORM_VERSION"] == ci["env"]["TERRAFORM_VERSION"]


# --- jobs that authenticate to Google ----------------------------------------------------------


def authenticating_jobs() -> list[tuple[str, str, dict]]:
    jobs = [(n, j, job) for n, j, job in all_jobs() if auth_steps(job)]
    assert len(jobs) == 5, f"expected 5 authenticating jobs, found {[j for _, j, _ in jobs]}"
    return jobs


@pytest.mark.parametrize("workflow,job_id,job", authenticating_jobs())
def test_authenticating_job_is_gated_on_its_variables(
    workflow: str, job_id: str, job: dict
) -> None:
    [env] = tf_ci_envs(job)
    conjuncts = top_level_conjuncts(job["if"])
    for var in ENV_VARS[env]:
        # A top-level conjunct, so an unset variable (the empty string) skips the job.
        assert f"vars.{var} != ''" in conjuncts, f"{job_id} is not gated on {var}"
    [auth] = auth_steps(job)
    assert auth["with"]["workload_identity_provider"] == f"${{{{ vars.{ENV_VARS[env][0]} }}}}"
    # Impersonating the deployer (not direct federation): Firebase and Cloud Storage need a
    # service account access token, and the deployer is the identity bootstrap authorised.
    assert auth["with"]["service_account"] == f"${{{{ vars.{ENV_VARS[env][1]} }}}}"
    assert job["env"]["TF_STATE_BUCKET"] == f"${{{{ vars.{ENV_VARS[env][2]} }}}}"
    # The other environment's variables never appear in this job.
    other = "prod" if env == "dev" else "dev"
    assert not any(v in str(job) for v in ENV_VARS[other]), job_id


@pytest.mark.parametrize("workflow,job_id,job", authenticating_jobs())
def test_authenticating_job_never_runs_for_a_fork(workflow: str, job_id: str, job: dict) -> None:
    on = triggers(load(workflow))
    conjuncts = top_level_conjuncts(job["if"])
    if "pull_request" in on:
        assert set(on) == {"pull_request"}
        assert "github.event.pull_request.head.repo.full_name == github.repository" in conjuncts
        assert "github.actor != 'dependabot[bot]'" in conjuncts
    else:
        # push to main, schedule, a manual run: only from main.
        assert set(on) <= {"push", "schedule", "workflow_dispatch"}
        assert "github.ref == 'refs/heads/main'" in conjuncts, job_id
        if "push" in on:
            assert on["push"] == {"branches": ["main"]}


def test_only_dev_is_planned_on_pull_requests() -> None:
    jobs = load("plan")["jobs"]
    assert {e for job in jobs.values() for e in tf_ci_envs(job)} == {"dev"}
    assert "PROD_" not in text("plan")


def test_prod_only_from_its_environments() -> None:
    jobs = load("apply")["jobs"]
    for job_id, job in jobs.items():
        if "prod" in tf_ci_envs(job):
            assert environment_of(job) in {"prod-plan", "prod"}, job_id
    applying = [j for j, job in jobs.items() if "apply --env prod" in runs(job)]
    assert applying == ["prod-apply"]
    assert environment_of(jobs["prod-apply"]) == "prod"
    assert "plan --env prod" in runs(jobs["prod-plan"])
    assert environment_of(jobs["prod-plan"]) == "prod-plan"
    # The apply refuses a plan different from the reviewed one.
    assert '--expect-digest "${REVIEWED_DIGEST}"' in runs(jobs["prod-apply"])
    assert jobs["prod-apply"]["env"]["REVIEWED_DIGEST"] == "${{ needs.prod-plan.outputs.digest }}"
    # Both prod jobs wait for the preflight that checks the environments' protection rules.
    assert jobs["prod-plan"]["needs"] == ["dev", "prod-preflight"]
    assert "needs.prod-preflight.result == 'success'" in top_level_conjuncts(
        jobs["prod-plan"]["if"]
    )
    assert jobs["prod-apply"]["needs"] == "prod-plan"
    assert "needs.prod-plan.result == 'success'" in top_level_conjuncts(jobs["prod-apply"]["if"])


def test_prod_preflight_requires_reviewers_and_branch_policies() -> None:
    job = load("apply")["jobs"]["prod-preflight"]
    script = runs(job)
    assert "for name in prod-plan prod" in script
    assert "required_reviewers" in script
    assert "deployment_branch_policy != null" in script
    assert job["permissions"] == {"actions": "read"}


def test_prod_follows_dev_but_not_a_failed_dev() -> None:
    conjuncts = top_level_conjuncts(load("apply")["jobs"]["prod-plan"]["if"])
    assert "(needs.dev.result == 'success' || needs.dev.result == 'skipped')" in conjuncts
    assert "!cancelled()" in conjuncts


def test_nobody_destroys_prod() -> None:
    for _, job_id, job in all_jobs():
        assert "destroy --env prod" not in runs(job), job_id


def test_nightly_destroy_is_opt_in_and_retries_for_the_subnet() -> None:
    workflow = load("destroy-dev")
    [job] = workflow["jobs"].values()
    assert "vars.DEV_NIGHTLY_DESTROY == 'true'" in top_level_conjuncts(job["if"])
    crons = [s["cron"] for s in triggers(workflow)["schedule"]]
    assert len(crons) == 2
    first, retry = (c.split() for c in crons)
    # The retry comes at least two hours after the first run (Google: "wait 1-2 hours").
    assert int(retry[1]) - int(first[1]) >= 2
    assert workflow["env"]["RETRY_SCHEDULE"] == crons[1]
    script = runs(job)
    assert 'if [ "${SCHEDULE}" = "${RETRY_SCHEDULE}" ]; then' in script
    assert "destroy --env dev --final" in script


@pytest.mark.parametrize("workflow,job_id,job", authenticating_jobs())
def test_state_writers_share_one_non_cancelling_group(
    workflow: str, job_id: str, job: dict
) -> None:
    [env] = tf_ci_envs(job)
    writes = re.search(rf"tf_ci\.py (apply|destroy) --env {env}", runs(job))
    if workflow == "plan":
        assert not writes, "a pull request must never apply or destroy"
        return
    assert job["concurrency"] == {"group": f"terraform-state-{env}", "cancel-in-progress": False}


def test_the_page_is_deployed_after_every_apply() -> None:
    for _, job_id, job in all_jobs():
        names = [s.get("run", "") for s in steps(job)]
        applies = [i for i, r in enumerate(names) if "tf_ci.py apply" in r]
        if not applies:
            continue
        deploys = [i for i, r in enumerate(names) if "tf_ci.py deploy-page" in r]
        assert deploys and deploys[0] > applies[0], f"{job_id}: no page deploy after the apply"
        # Same condition as the apply, so a skipped apply also skips the deploy and vice versa.
        assert steps(job)[deploys[0]].get("if") == steps(job)[applies[0]].get("if"), job_id
        assert any(s.get("uses", "").startswith("actions/setup-node@") for s in steps(job))


def test_plan_comment_job_has_no_credentials_and_reads_through_env() -> None:
    jobs = load("plan")["jobs"]
    comment = jobs["comment"]
    assert comment["permissions"] == {"pull-requests": "write"}
    assert not auth_steps(comment)
    [step] = steps(comment)
    # No ${{ }} inside the script: plan text and names reach it only through env.
    assert "${{" not in step["run"]
    assert step["env"]["PLAN_B64"] == "${{ needs.plan.outputs.text_b64 }}"
    assert jobs["plan"]["outputs"]["text_b64"] == "${{ steps.plan.outputs.text_b64 }}"


def test_access_token_is_short_lived_and_only_reaches_the_tfvars_step() -> None:
    for _, job_id, job in authenticating_jobs():
        [auth] = auth_steps(job)
        assert auth["with"]["access_token_lifetime"] == "300s", job_id  # noqa: S105
        users = [s for s in steps(job) if "steps.auth.outputs.access_token" in str(s)]
        assert len(users) == 1 and "fetch-tfvars" in users[0]["run"], job_id


# --- the credential-free checks ------------------------------------------------------------------


def test_checks_run_unconditionally_on_every_pull_request() -> None:
    workflow = load("checks")
    on = triggers(workflow)
    assert "pull_request" in on and not (on["pull_request"] or {})
    [job] = workflow["jobs"].values()
    assert "if" not in job
    commands = [s.get("run") for s in steps(job) if "run" in s]
    assert commands == ["make tf-validate", "make tf-lint", "make tf-scan"]
    assert not [s for s in steps(job) if "if" in s]


def test_tflint_ruleset_version_matches_the_makefile() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    [make_version] = re.findall(r"^TFLINT_GOOGLE_VERSION := (\S+)$", makefile, re.MULTILINE)
    config = (REPO_ROOT / ".tflint.hcl").read_text()
    [config_version] = re.findall(r'plugin "google" \{[^}]*version = "([^"]+)"', config, re.S)
    assert make_version == config_version
    # Every downloaded tool has a pinned version and a pinned SHA-256.
    for tool in ("TFLINT", "TFLINT_GOOGLE", "TRIVY"):
        assert re.search(rf"^{tool}_VERSION := v?\d+\.\d+\.\d+$", makefile, re.MULTILINE), tool
        assert re.search(rf"^{tool}_SHA256 := [0-9a-f]{{64}}$", makefile, re.MULTILINE), tool


def test_firebase_tools_version_is_exact() -> None:
    script = (REPO_ROOT / "infra" / "envs" / "ci" / "tf_ci.py").read_text()
    [version] = re.findall(r'^FIREBASE_TOOLS_VERSION = "([^"]+)"$', script, re.MULTILINE)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "pin an exact firebase-tools version"


def test_conjunct_splitter() -> None:
    # The gate checks above depend on it.
    assert top_level_conjuncts("a && (b || c) && d") == ["a", "(b || c)", "d"]
    with pytest.raises(AssertionError):
        top_level_conjuncts("vars.X != '' || true")
