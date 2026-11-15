"""Which GitHub Actions job can become which service account: the truth table (T26).

Bootstrap maps every GitHub OIDC token to one `ci_context` (a CEL expression in wif.tf), and
each CI account is bound to its own contexts; prod's pool also has an attribute condition. This
test takes the REAL mapping, condition and bindings from bootstrap's own mocked plans (`terraform
test -json -verbose` on tests/identity.tftest.hcl, credential-free), evaluates them with a CEL
implementation (cel-python) for every combination of event, ref, environment and repository, and
compares the identities each token could reach with an expectation written independently here:

- a pull request reaches dev's planner and nothing else; nothing in prod;
- a job on main (push, schedule, manual run) reaches dev's deployer; nothing in prod;
- a page deploy job (Environment dev-page / prod-page, on main) reaches only the page deployer;
- prod-plan reaches only prod's planner; prod only prod's deployer;
- branches, tags, pull_request_target, other events and other repositories reach nothing;
- no token ever reaches two accounts.

Google's CEL implementation is not cel-python; that they agree is checklist item CI4.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

celpy = pytest.importorskip("celpy")

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "infra" / "bootstrap"
REPO_ID = "123456789"  # the tests' synthetic repository (tests/identity.tftest.hcl)
REPO_NAME = "example-owner/example-repo"
RUNS = {
    "dev": "dev_planner_from_pull_requests_deployer_from_main",
    "prod": "prod_identities_are_bound_to_their_environments",
}
CONTEXT_PREFIX = "/attribute.ci_context/"
DROP = (
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CREDENTIALS", "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT", "GCP_SA_KEY_B64", "CLOUDSDK_CORE_PROJECT",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_PROJECT",
)  # fmt: skip

EVENTS = [
    "push", "pull_request", "pull_request_target", "workflow_dispatch", "schedule",
    "merge_group", "workflow_run", "issue_comment", "dynamic",
]  # fmt: skip
REFS = [
    "refs/heads/main", "refs/heads/feat", "refs/heads/main-x", "refs/heads/x/refs/heads/main",
    "refs/pull/7/merge", "refs/tags/v1.0.0", "refs/heads/gh-readonly-queue/main/pr-1",
]  # fmt: skip
ENVIRONMENTS = [None, "dev-page", "prod-plan", "prod", "prod-page", "staging", "", "prod-main"]
REPO_IDS = [REPO_ID, "999"]
MAIN_EVENTS = {"push", "schedule", "workflow_dispatch"}


def plans() -> dict[str, dict]:
    if shutil.which("terraform") is None:
        pytest.skip("terraform is not installed")
    env = {k: v for k, v in os.environ.items() if k not in DROP}
    env.update(
        CLOUDSDK_CONFIG="/nonexistent", GCE_METADATA_HOST="127.0.0.1:9", TF_IN_AUTOMATION="1"
    )
    subprocess.run(
        ["terraform", f"-chdir={BOOTSTRAP}", "init", "-backend=false", "-input=false", "-no-color"],
        env=env, check=True, capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    result = subprocess.run(
        ["terraform", f"-chdir={BOOTSTRAP}", "test", "-no-color", "-json", "-verbose",
         "-filter=tests/identity.tftest.hcl"],
        env=env, capture_output=True, text=True, timeout=900, check=False,
    )  # fmt: skip
    found = {}
    for line in result.stdout.splitlines():
        if line.startswith("{"):
            message = json.loads(line)
            if message.get("type") == "test_plan" and message.get("@testrun") in RUNS.values():
                found[message["@testrun"]] = message["test_plan"]
    assert set(found) == set(RUNS.values()), f"plans missing: {result.stderr[-2000:]}"
    return {env_name: found[run] for env_name, run in RUNS.items()}


def extract(plan: dict) -> tuple[str, str, dict[str, set[str]]]:
    """(ci_context mapping, pool condition, contexts bound per account) from a planned config."""
    changes = {rc["address"]: rc["change"]["after"] for rc in plan["resource_changes"]}
    provider = changes["google_iam_workload_identity_pool_provider.github"]
    bound: dict[str, set[str]] = {}
    for address, after in changes.items():
        if address.startswith("google_service_account_iam_member.github_wif["):
            key = address.split('["', 1)[1].rstrip('"]')
            account = key.split("/", 1)[0]
            assert CONTEXT_PREFIX in after["member"]
            bound.setdefault(account, set()).add(after["member"].split(CONTEXT_PREFIX, 1)[1])
    return (
        provider["attribute_mapping"]["attribute.ci_context"],
        provider["attribute_condition"],
        bound,
    )


def evaluate(program, claims: dict) -> object:
    try:
        value = program.evaluate({"assertion": celpy.json_to_cel(claims)})
    except Exception:  # noqa: BLE001 - a CEL evaluation error rejects the token
        return None
    return None if isinstance(value, celpy.CELEvalError) else value


def claims_of(event: str, ref: str, environment: str | None, repo_id: str) -> dict:
    claims = {"repository": REPO_NAME, "repository_id": repo_id, "event_name": event, "ref": ref}
    if environment is not None:
        claims["environment"] = environment
    return claims


def expected(
    env_name: str, event: str, ref: str, environment: str | None, repo_id: str
) -> set[str]:
    """Written from the design, not from the CEL: which account this job should be able to use."""
    if repo_id != REPO_ID:
        return set()
    on_main = ref == "refs/heads/main" and event in MAIN_EVENTS
    if env_name == "dev":
        if environment is None:
            if event == "pull_request":
                return {"planner"}
            return {"deployer"} if on_main else set()
        return {"page-deployer"} if environment == "dev-page" and on_main else set()
    # prod: the pool admits only main and the three Environments; the contexts pick the account.
    if not on_main:
        return set()
    return {"prod-plan": {"planner"}, "prod": {"deployer"}, "prod-page": {"page-deployer"}}.get(
        environment or "", set()
    )


@pytest.fixture(scope="module")
def truth_table() -> dict[tuple, dict[str, object]]:
    env = celpy.Environment()
    table: dict[tuple, dict[str, object]] = {}
    for env_name, plan in plans().items():
        mapping, condition, bound = extract(plan)
        mapping_program = env.program(env.compile(mapping))
        condition_program = env.program(env.compile(condition))
        for event, ref, environment, repo_id in itertools.product(
            EVENTS, REFS, ENVIRONMENTS, REPO_IDS
        ):
            claims = claims_of(event, ref, environment, repo_id)
            # A condition that is not exactly true (false, or an error) rejects the token.
            admitted = evaluate(condition_program, claims) == celpy.celtypes.BoolType(True)
            context = evaluate(mapping_program, claims)
            reached = (
                {
                    a
                    for a, contexts in bound.items()
                    if context is not None and str(context) in contexts
                }
                if admitted
                else set()
            )
            table[(env_name, event, ref, environment, repo_id)] = {
                "context": None if context is None else str(context),
                "admitted": admitted,
                "reached": reached,
                "bound": bound,
            }
    return table


def test_the_plans_bind_the_documented_contexts(truth_table) -> None:
    some = {k[0]: v["bound"] for k, v in truth_table.items()}
    assert some["dev"] == {
        "deployer": {"main"},
        "planner": {"pull-request"},
        "page-deployer": {"env-dev-page-main"},
    }
    assert some["prod"] == {
        "deployer": {"env-prod-main"},
        "planner": {"env-prod-plan-main"},
        "page-deployer": {"env-prod-page-main"},
    }


def test_every_job_reaches_exactly_the_expected_account(truth_table) -> None:
    wrong = [
        (key, row["context"], sorted(row["reached"]), sorted(expected(*key)))
        for key, row in truth_table.items()
        if row["reached"] != expected(*key)
    ]
    assert not wrong, "\n".join(map(str, wrong[:20]))


def test_no_token_reaches_two_accounts(truth_table) -> None:
    assert all(len(row["reached"]) <= 1 for row in truth_table.values())


def test_page_jobs_reach_only_the_page_deployer(truth_table) -> None:
    for (env_name, event, ref, environment, _repo_id), row in truth_table.items():
        if environment in ("dev-page", "prod-page"):
            assert row["reached"] <= {"page-deployer"}, (env_name, event, ref, environment)
    # And they do reach it from main (the positive case).
    assert truth_table[("dev", "push", "refs/heads/main", "dev-page", REPO_ID)]["reached"] == {
        "page-deployer"
    }
    assert truth_table[("prod", "push", "refs/heads/main", "prod-page", REPO_ID)]["reached"] == {
        "page-deployer"
    }


def test_the_table_is_not_vacuous(truth_table) -> None:
    reached = {(k[0], a) for k, row in truth_table.items() for a in row["reached"]}
    assert reached == {
        (e, a) for e in ("dev", "prod") for a in ("deployer", "planner", "page-deployer")
    }
    assert len(truth_table) == 2 * len(EVENTS) * len(REFS) * len(ENVIRONMENTS) * len(REPO_IDS)
