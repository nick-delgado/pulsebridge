"""Static checks on the Terraform CI/CD workflows (T26), .github/workflows/terraform-*.yml.

GitHub only runs a workflow after it is pushed, and these ones reach real projects, so the rules
they rely on are checked here instead of being discovered in production:

- every action is pinned to a full commit SHA; no `${{ }}` inside a `run:` script;
- least permissions: a cloud credential (`id-token: write`) and the right to write to a pull
  request never meet in one job; the page is built with no credential at all;
- no key material: no `secrets.*`, no `credentials_json`;
- each authenticating job is gated on exactly the repository variables of the identity it uses,
  runs only from main or, for pull requests, only from the same repository;
- each job uses the narrowest identity: pull-request and prod plans the read-only planner (with
  -lock=false), applies and the destroy the deployer (and no npm code), page deploys the page
  deployer (and nothing of Terraform);
- prod only from its Environments; its apply pins the reviewed commit, tfvars generation and plan;
- applies and destroys of one environment share one non-cancelling concurrency group;
- the page is deployed after every apply; binary plans are never uploaded; the tfvars are
  deleted at the end of every job that fetched them.

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
AUTH_ACTION = "google-github-actions/auth"
UPLOAD = "actions/upload-artifact"
ALLOWED_JOB_PERMISSIONS = {
    ("contents", "read"),
    ("id-token", "write"),
    ("pull-requests", "write"),
    ("actions", "read"),
}
# The account variable each identity uses, per environment.
IDENTITY_VARS = {"DEPLOYER_SA": "deployer", "PLANNER_SA": "planner", "PAGE_DEPLOYER_SA": "page"}
# Every artifact a workflow may upload, and what it holds. None of them is a binary plan.
ALLOWED_ARTIFACTS = {
    "plan-dev": "${{ runner.temp }}/plan-dev/",  # redacted plan text and meta.json
    "plan-prod": "${{ runner.temp }}/plan-prod/",
    "web-dist": "web/dist/",  # the built page, public once deployed
    "page-config-dev": "${{ runner.temp }}/page-dev/",  # firebase.json and the project ID
    "page-config-prod": "${{ runner.temp }}/page-prod/",
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


def identity_of(job: dict) -> tuple[str, str]:
    """(environment, identity) of an authenticating job, from its auth step's service_account."""
    [auth] = auth_steps(job)
    m = re.fullmatch(r"\$\{\{ vars\.(DEV|PROD)_GCP_(\w+) \}\}", auth["with"]["service_account"])
    assert m and m.group(2) in IDENTITY_VARS, auth["with"]["service_account"]
    return m.group(1).lower(), IDENTITY_VARS[m.group(2)]


def authenticating_jobs() -> list[tuple[str, str, dict]]:
    jobs = [(n, j, job) for n, j, job in all_jobs() if auth_steps(job)]
    assert sorted(j for _, j, _ in jobs) == sorted(
        ["plan", "dev", "dev-page", "prod-plan", "prod-apply", "prod-page", "destroy"]
    )
    return jobs


AUTH_JOBS = authenticating_jobs()


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


def test_no_expression_inside_a_script(name: str) -> None:
    # ${{ github.head_ref }} or a plan's text pasted into a script becomes shell code. Values
    # reach scripts only through env.
    for _, job_id, job in [(n, j, jb) for n, j, jb in all_jobs() if n == name]:
        for step in steps(job):
            assert "${{" not in step.get("run", ""), f"{job_id}: ${{{{ }}}} in a run script"


def test_no_key_material_or_secrets(name: str) -> None:
    body = text(name)
    assert "secrets." not in body, "no secrets: authentication is Workload Identity Federation only"
    assert "credentials_json" not in body
    assert "GOOGLE_CREDENTIALS" not in body
    assert "service_account_key" not in body.lower()


def test_no_dangerous_triggers(name: str) -> None:
    assert not {"pull_request_target", "workflow_run"} & set(triggers(load(name)))


def test_token_permissions_are_minimal(name: str) -> None:
    workflow = load(name)
    assert workflow["permissions"] in ({}, {"contents": "read"}), "top-level token too broad"
    for job_id, job in workflow["jobs"].items():
        perms = job.get("permissions")
        assert isinstance(perms, dict), f"{job_id} must list its permissions"
        assert set(perms.items()) <= ALLOWED_JOB_PERMISSIONS, f"{job_id}: {perms}"
        has_auth = bool(auth_steps(job))
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


def test_only_known_artifacts_are_uploaded_and_never_a_binary_plan(name: str) -> None:
    assert "cache@" not in text(name)
    for _, job_id, job in [(n, j, jb) for n, j, jb in all_jobs() if n == name]:
        for step in steps(job):
            if step.get("uses", "").startswith(UPLOAD + "@"):
                artifact = step["with"]["name"]
                assert artifact in ALLOWED_ARTIFACTS, f"{job_id} uploads {artifact!r}"
                assert step["with"]["path"] == ALLOWED_ARTIFACTS[artifact], job_id
                assert step["with"]["retention-days"] == 1, job_id
                assert step["with"]["if-no-files-found"] == "error", job_id
        # The plan record directory holds only what tf_ci.py plan --record writes (plan.txt with
        # the redacted text, meta.json); the binary plan lives in tf_ci's own temp directory.
        for m in re.finditer(r"--record \"\$\{RUNNER_TEMP\}/(plan-\w+)\"", runs(job)):
            assert m.group(1) in ALLOWED_ARTIFACTS


def test_terraform_version_matches_ci(name: str) -> None:
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    assert load(name)["env"]["TERRAFORM_VERSION"] == ci["env"]["TERRAFORM_VERSION"]


# --- jobs that authenticate to Google ----------------------------------------------------------


@pytest.mark.parametrize("workflow,job_id,job", AUTH_JOBS)
def test_authenticating_job_is_gated_on_its_identitys_variables(
    workflow: str, job_id: str, job: dict
) -> None:
    env, identity = identity_of(job)
    prefix = env.upper()
    conjuncts = top_level_conjuncts(job["if"])
    sa_var = {"deployer": "DEPLOYER_SA", "planner": "PLANNER_SA", "page": "PAGE_DEPLOYER_SA"}[
        identity
    ]
    needed = [f"{prefix}_GCP_WORKLOAD_IDENTITY_PROVIDER", f"{prefix}_GCP_{sa_var}"]
    if identity != "page":
        needed.append(f"{prefix}_TF_STATE_BUCKET")
        assert job["env"]["TF_STATE_BUCKET"] == f"${{{{ vars.{prefix}_TF_STATE_BUCKET }}}}"
    else:
        assert "TF_STATE_BUCKET" not in (job.get("env") or {}), "the page deployer reads no state"
    for var in needed:
        # A top-level conjunct, so an unset variable (the empty string) skips the job.
        assert f"vars.{var} != ''" in conjuncts, f"{job_id} is not gated on {var}"
    [auth] = auth_steps(job)
    assert auth["with"]["workload_identity_provider"] == (
        f"${{{{ vars.{prefix}_GCP_WORKLOAD_IDENTITY_PROVIDER }}}}"
    )
    other = "vars.PROD_" if env == "dev" else "vars.DEV_"
    body = yaml.safe_dump({k: v for k, v in job.items() if k != "if"})
    assert other not in body, f"{job_id} uses the other environment's variables"


@pytest.mark.parametrize("workflow,job_id,job", AUTH_JOBS)
def test_authenticating_job_never_runs_for_a_fork(workflow: str, job_id: str, job: dict) -> None:
    on = triggers(load(workflow))
    conjuncts = top_level_conjuncts(job["if"])
    if "pull_request" in on:
        assert set(on) == {"pull_request"}
        assert "github.event.pull_request.head.repo.full_name == github.repository" in conjuncts
        assert "github.actor != 'dependabot[bot]'" in conjuncts
    else:
        assert set(on) <= {"push", "schedule", "workflow_dispatch"}
        assert "github.ref == 'refs/heads/main'" in conjuncts, job_id
        if "push" in on:
            assert on["push"] == {"branches": ["main"]}


@pytest.mark.parametrize("workflow,job_id,job", AUTH_JOBS)
def test_each_job_uses_the_narrowest_identity(workflow: str, job_id: str, job: dict) -> None:
    env, identity = identity_of(job)
    commands = re.findall(r"tf_ci\.py (\S+)", runs(job))
    uses = [s.get("uses", "").split("@")[0] for s in steps(job)]
    if identity == "planner":
        assert set(commands) <= {"fetch-tfvars", "init", "stack-is-up", "plan", "cleanup"}
        assert "--no-lock" in runs(job), "the planner cannot take the lock"
        assert "plan" in commands
    elif identity == "deployer":
        # The job that holds the deployer runs no npm code at all.
        assert "actions/setup-node" not in uses, job_id
        assert not re.search(r"\b(npm|npx|make web)", runs(job)), job_id
        assert set(commands) & {"apply", "destroy"}, job_id
        assert "deploy-page" not in commands
    else:  # the page deployer: Hosting only, nothing of Terraform
        assert commands == ["deploy-page"], job_id
        assert "hashicorp/setup-terraform" not in uses
        assert "token_format" not in auth_steps(job)[0]["with"]
    if workflow == "plan":
        assert identity == "planner", "pull requests must never get the deployer"


@pytest.mark.parametrize("workflow,job_id,job", AUTH_JOBS)
def test_tfvars_are_deleted_at_the_end(workflow: str, job_id: str, job: dict) -> None:
    if "fetch-tfvars" not in runs(job):
        return
    last = steps(job)[-1]
    assert last.get("if") == "always()" and re.search(r"tf_ci\.py cleanup --env \w+", last["run"])


def test_access_token_is_short_lived_and_only_reaches_the_tfvars_step() -> None:
    for _, job_id, job in AUTH_JOBS:
        [auth] = auth_steps(job)
        if "token_format" not in auth["with"]:
            assert "fetch-tfvars" not in runs(job)
            continue
        assert auth["with"]["access_token_lifetime"] == "300s", job_id  # noqa: S105
        users = [s for s in steps(job) if "steps.auth.outputs.access_token" in str(s)]
        assert len(users) == 1 and "fetch-tfvars" in users[0]["run"], job_id


def test_only_dev_is_planned_on_pull_requests_by_the_planner() -> None:
    jobs = load("plan")["jobs"]
    assert identity_of(jobs["plan"]) == ("dev", "planner")
    assert "PROD_" not in text("plan")
    assert "DEV_GCP_DEPLOYER_SA" not in text("plan"), "no fallback to the deployer"


def test_prod_only_from_its_environments() -> None:
    jobs = load("apply")["jobs"]
    for job_id, job in jobs.items():
        if auth_steps(job) and identity_of(job)[0] == "prod":
            expected = "prod-plan" if identity_of(job)[1] == "planner" else "prod"
            assert environment_of(job) == expected, job_id
    assert [j for j, job in jobs.items() if "apply --env prod" in runs(job)] == ["prod-apply"]


def test_prod_apply_is_pinned_to_the_reviewed_plan() -> None:
    job = load("apply")["jobs"]["prod-apply"]
    script = runs(job)
    order = [s.get("name") or s.get("uses", "").split("@")[0] for s in steps(job)]
    # The tip check runs before anything else that matters.
    assert order.index("Require this commit to be the tip of main") < order.index("Apply prod")
    assert "tf_ci.py require-tip" in script
    assert 'fetch-tfvars --env prod --generation "${generation}"' in script
    assert "jq -r '.tfvars_generation' \"${RUNNER_TEMP}/reviewed/meta.json\"" in script
    assert 'apply --env prod --reviewed "${RUNNER_TEMP}/reviewed"' in script
    [download] = [
        s for s in steps(job) if s.get("uses", "").startswith("actions/download-artifact@")
    ]
    assert download["with"]["name"] == "plan-prod"
    assert job["needs"] == "prod-plan"
    assert "needs.prod-plan.result == 'success'" in top_level_conjuncts(job["if"])


def test_prod_plan_follows_the_preflight_and_a_deployed_dev() -> None:
    job = load("apply")["jobs"]["prod-plan"]
    conjuncts = top_level_conjuncts(job["if"])
    assert job["needs"] == ["dev", "prod-preflight"]
    assert "needs.prod-preflight.result == 'success'" in conjuncts
    assert "needs.dev.result != 'failure'" in conjuncts
    assert (
        "(needs.dev.outputs.up == 'true' || vars.DEV_GCP_WORKLOAD_IDENTITY_PROVIDER == '' "
        "|| vars.PROD_WITHOUT_DEV == 'true')"
    ) in conjuncts
    assert "!cancelled()" in conjuncts
    assert load("apply")["jobs"]["dev"]["outputs"]["up"] == "${{ steps.up.outputs.up }}"


def test_prod_preflight_runs_the_environment_check() -> None:
    job = load("apply")["jobs"]["prod-preflight"]
    assert runs(job).strip() == "python3 infra/envs/ci/tf_ci.py check-environments"
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    [step] = [s for s in steps(job) if "run" in s]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"  # noqa: S105
    assert "(github.event_name != 'workflow_dispatch' || inputs.prod)" in top_level_conjuncts(
        job["if"]
    )


def test_manual_runs_do_dev_only_by_default() -> None:
    dispatch = triggers(load("apply"))["workflow_dispatch"]
    assert dispatch["inputs"]["prod"] == {
        "description": "Also plan and apply prod (after approval)",
        "type": "boolean",
        "default": False,
    }


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
    assert int(retry[1]) - int(first[1]) >= 2
    assert workflow["env"]["RETRY_SCHEDULE"] == crons[1]
    script = runs(job)
    assert 'if [ "${SCHEDULE}" = "${RETRY_SCHEDULE}" ]; then' in script
    assert "destroy --env dev --final" in script


@pytest.mark.parametrize("workflow,job_id,job", AUTH_JOBS)
def test_state_writers_share_one_non_cancelling_group(
    workflow: str, job_id: str, job: dict
) -> None:
    env, _ = identity_of(job)
    writes = re.search(rf"tf_ci\.py (apply|destroy) --env {env}", runs(job))
    if workflow == "plan":
        assert not writes, "a pull request must never apply or destroy"
        return
    if writes:
        assert job["concurrency"] == {
            "group": f"terraform-state-{env}",
            "cancel-in-progress": False,
        }


def test_the_page_is_built_without_credentials_and_deployed_after_every_apply() -> None:
    jobs = load("apply")["jobs"]
    build = jobs["build-page"]
    assert build["permissions"] == {"contents": "read"} and not auth_steps(build)
    assert runs(build).strip() == "make web-build"
    for apply_job, page_job in (("dev", "dev-page"), ("prod-apply", "prod-page")):
        page = jobs[page_job]
        needs = page["needs"] if isinstance(page["needs"], list) else [page["needs"]]
        assert set(needs) == {apply_job, "build-page"}
        conjuncts = top_level_conjuncts(page["if"])
        assert f"needs.{apply_job}.result == 'success'" in conjuncts
        assert "needs.build-page.result == 'success'" in conjuncts
        downloads = [
            s["with"]["name"]
            for s in steps(page)
            if s.get("uses", "").startswith("actions/download-artifact@")
        ]
        env = "dev" if apply_job == "dev" else "prod"
        assert downloads == ["web-dist", f"page-config-{env}"]
        assert f"export-page-config --env {env}" in runs(jobs[apply_job])
    assert "needs.dev.outputs.up == 'true'" in top_level_conjuncts(jobs["dev-page"]["if"])


def test_plan_comment_job_has_no_credentials_and_reads_the_artifact() -> None:
    jobs = load("plan")["jobs"]
    comment = jobs["comment"]
    assert comment["permissions"] == {"pull-requests": "write"}
    assert not auth_steps(comment)
    script = runs(comment)
    assert "dev is not deployed: nothing was planned" in script
    assert 'if [ "${changes}" != "0" ] && [ ! -s "${plan}" ]; then' in script
    [download] = [s for s in steps(comment) if s.get("uses", "").startswith("actions/download")]
    assert download["with"]["name"] == "plan-dev"


# --- the credential-free checks ------------------------------------------------------------------


def test_checks_run_unconditionally_on_every_pull_request() -> None:
    workflow = load("checks")
    on = triggers(workflow)
    assert "pull_request" in on and not (on["pull_request"] or {})
    [job] = workflow["jobs"].values()
    assert "if" not in job
    commands = [s.get("run") for s in steps(job) if "run" in s]
    assert commands == ["make tf-validate", "make tf-test", "make tf-lint", "make tf-scan"]
    assert not [s for s in steps(job) if "if" in s]


def test_tflint_ruleset_version_matches_the_makefile() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    [make_version] = re.findall(r"^TFLINT_GOOGLE_VERSION := (\S+)$", makefile, re.MULTILINE)
    config = (REPO_ROOT / ".tflint.hcl").read_text()
    [config_version] = re.findall(r'plugin "google" \{[^}]*version = "([^"]+)"', config, re.S)
    assert make_version == config_version
    for tool in ("TFLINT", "TFLINT_GOOGLE", "TRIVY"):
        assert re.search(rf"^{tool}_VERSION := v?\d+\.\d+\.\d+$", makefile, re.MULTILINE), tool
        assert re.search(rf"^{tool}_SHA256 := [0-9a-f]{{64}}$", makefile, re.MULTILINE), tool


def test_every_download_is_checked_against_its_pinned_sha256() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    fetch = re.search(r"^define fetch\n(.*?)^endef", makefile, re.S | re.M).group(1)
    assert "sha256sum --check --strict --quiet" in fetch
    # The file is moved into place only after the check passed (the && / || chain).
    assert fetch.index("sha256sum --check") < fetch.index('mv "$(3).download" "$(3)"')
    for tool in ("TFLINT", "TFLINT_GOOGLE", "TRIVY"):
        assert re.search(
            rf"\$\(call fetch,https://github\.com/[^,]+,\$\({tool}_SHA256\),\$@\)", makefile
        ), tool
    assert len(re.findall(r"^\t\$\(call fetch,", makefile, re.MULTILINE)) == 3


def test_deploy_tools_pin_the_cli_tree() -> None:
    import json

    tools = REPO_ROOT / "infra" / "envs" / "ci" / "deploy-tools"
    manifest = json.loads((tools / "package.json").read_text())
    assert manifest["dependencies"] == {
        "firebase-tools": manifest["dependencies"]["firebase-tools"]
    }
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["dependencies"]["firebase-tools"])
    lock = json.loads((tools / "package-lock.json").read_text())
    packages = {k: v for k, v in lock["packages"].items() if k}
    assert len(packages) > 100
    assert all(p["integrity"].startswith("sha512-") for p in packages.values())
    assert all(p["resolved"].startswith("https://registry.npmjs.org/") for p in packages.values())
    script = (REPO_ROOT / "infra" / "envs" / "ci" / "tf_ci.py").read_text()
    assert '["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]' in script
    assert '"npx", "--no-install", "firebase", "deploy"' in script


def test_conjunct_splitter() -> None:
    assert top_level_conjuncts("a && (b || c) && d") == ["a", "(b || c)", "d"]
    with pytest.raises(AssertionError):
        top_level_conjuncts("vars.X != '' || true")
