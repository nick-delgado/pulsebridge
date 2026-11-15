"""Behaviour of infra/envs/ci/tf_ci.py, the Terraform steps of the CI/CD workflows (T26).

The script runs in-process with its repository root pointed at a temporary directory and fake
`terraform`, `npm` and `npx` programs first on PATH; each fake records its arguments and answers
from files the test writes. HTTP calls (Cloud Storage, the GitHub API) are replaced in-process.
Nothing reaches Google or GitHub, and no test writes into the real infra/envs roots. All values
are synthetic.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "envs" / "ci" / "tf_ci.py"
SHA = "0123456789abcdef0123456789abcdef01234567"

TFVARS = """\
project_id                  = "example-project"
patient_id                  = "SYN-0042"   # synthetic
patient_assigning_authority = "PULSEBRIDGE"
alert_email                 = "alerts@example.com"
time_zone                   = "America/New_York"
"""

FAKE_TERRAFORM = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
fake = Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
with open(fake / "calls.log", "a") as f:
    f.write(json.dumps({"tool": "terraform", "cwd": os.getcwd(), "args": args}) + "\n")
def read(name, default=""):
    p = fake / name
    return p.read_text() if p.exists() else default
cmd = args[0]
if cmd == "state":
    sys.stdout.write(read("state_after" if (fake / "destroyed").exists() else "state"))
    sys.exit(int(read("state_rc", "0")))
elif cmd == "plan":
    out = [a for a in args if a.startswith("-out=")][0][5:]
    Path(out).write_text("binary plan")
    sys.exit(int(read("plan_rc", "0")))
elif cmd == "show":
    sys.stdout.write(read("plan.json") if "-json" in args else read("plan.txt"))
elif cmd == "destroy":
    (fake / "destroyed").write_text("")
    sys.exit(int(read("destroy_rc", "0")))
elif cmd == "output":
    sys.stdout.write(read("output_" + args[-1]))
sys.exit(0)
"""

FAKE_RECORDER = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
fake = Path(os.environ["FAKE_DIR"])
with open(fake / "calls.log", "a") as f:
    f.write(json.dumps({"tool": Path(sys.argv[0]).name, "cwd": os.getcwd(), "args": sys.argv[1:],
                        "ignore_scripts": os.environ.get("NPM_CONFIG_IGNORE_SCRIPTS")}) + "\n")
"""

PLAN = {
    "resource_changes": [
        {
            "address": "module.stack.module.phr.google_cloud_run_v2_service.cuff_sim",
            "change": {
                "actions": ["update"],
                "before": {"max_instance_count": 1, "env": {"PATIENT_ID": "SYN-0042"}},
                "after": {"max_instance_count": 2, "env": {"PATIENT_ID": "SYN-0042"}},
                "before_sensitive": {"env": True},
                "after_sensitive": {"env": True},
                "after_unknown": {},
            },
        },
        {
            "address": 'module.stack.module.identity.google_service_account.this["edge-vm"]',
            "change": {"actions": ["no-op"], "before": {"a": 1}, "after": {"a": 1}},
        },
        {
            "address": "module.stack.module.edge_vm.google_compute_instance.this",
            "change": {"actions": ["delete", "create"], "before": {"x": 1}, "after": {"x": 2}},
        },
    ],
    "output_changes": {"region": {"actions": ["no-op"], "before": "a", "after": "a"}},
}


@pytest.fixture
def ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The tf_ci module, a fake repository and fake tools. Returns (module, fake_dir, repo)."""
    spec = importlib.util.spec_from_file_location("tf_ci_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tf_ci_under_test"] = module
    spec.loader.exec_module(module)

    repo = tmp_path / "repo"
    for env in ("dev", "prod"):
        (repo / "infra" / "envs" / env).mkdir(parents=True)
        (repo / "infra" / "envs" / env / "terraform.tfvars").write_text(TFVARS)
    (repo / "web" / "dist").mkdir(parents=True)
    (repo / "web" / "dist" / "index.html").write_text("<!doctype html>")
    tools = repo / "deploy-tools"
    tools.mkdir()
    (tools / "package.json").write_text('{"dependencies": {"firebase-tools": "15.34.2"}}')
    monkeypatch.setattr(module, "REPO", repo)
    monkeypatch.setattr(module, "ENVS", repo / "infra" / "envs")
    monkeypatch.setattr(module, "DEPLOY_TOOLS", tools)

    fake = tmp_path / "fake"
    bin_dir = fake / "bin"
    bin_dir.mkdir(parents=True)
    for name, body in (
        ("terraform", FAKE_TERRAFORM),
        ("npm", FAKE_RECORDER),
        ("npx", FAKE_RECORDER),
    ):
        path = bin_dir / name
        path.write_text(body.replace("/usr/bin/env python3", sys.executable, 1))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    (fake / "plan.json").write_text(json.dumps(PLAN))
    (fake / "plan.txt").write_text("  ~ env = (sensitive value)\n  ~ max_instance_count = 1 -> 2\n")

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(fake))
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github_output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("TF_STATE_BUCKET", "example-project-tfstate")
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example-owner/example-repo")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    # A recorded generation, as fetch-tfvars leaves it.
    for env in ("dev", "prod"):
        module.generation_path(env).write_text("1700000000000001")
    return module, fake, repo


def calls(fake: Path, tool: str | None = None) -> list[dict]:
    log = fake / "calls.log"
    entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [e for e in entries if tool is None or e["tool"] == tool]


def tf_calls(fake: Path, command: str) -> list[list[str]]:
    return [c["args"] for c in calls(fake, "terraform") if c["args"][0] == command]


def outputs() -> dict[str, str]:
    path = Path(os.environ["GITHUB_OUTPUT"])
    return (
        dict(line.split("=", 1) for line in path.read_text().splitlines()) if path.exists() else {}
    )


def record_plan(module, tmp: Path, env: str = "prod") -> Path:
    record = tmp / f"plan-{env}"
    assert module.main(["plan", "--env", env, "--no-lock", "--record", str(record)]) == 0
    return record


# --- the tfvars: parsing, redaction ------------------------------------------------------------


def test_sensitive_values_are_read_from_the_tfvars(ci) -> None:
    module, _, _ = ci
    assert module.sensitive_values(TFVARS) == {
        "patient_id": "SYN-0042",
        "alert_email": "alerts@example.com",
    }
    assert module.sensitive_values('patient_given_name = "A \\"B\\""\n') == {
        "patient_given_name": 'A "B"'
    }


@pytest.mark.parametrize(
    "line",
    ["patient_family_name = <<EOT\nExample\nEOT\n", "alert_email = local.address\n"],
)
def test_a_heredoc_or_expression_for_a_sensitive_value_is_refused(ci, line: str) -> None:
    module, _, _ = ci
    with pytest.raises(module.CiError, match="plain quoted string"):
        module.sensitive_values(line)


def test_redaction_passes_a_redacted_plan_and_refuses_a_leak(ci) -> None:
    module, _, _ = ci
    module.check_redaction("patient = (sensitive value)\nauthority = PULSEBRIDGE", TFVARS)
    for leaked in ('PATIENT_ID = "SYN-0042"', "to: alerts@example.com"):
        with pytest.raises(module.CiError) as e:
            module.check_redaction(leaked, TFVARS)
        assert "SYN-0042" not in str(e.value) and "alerts@example.com" not in str(e.value)


def test_redaction_matches_whole_words_only(ci) -> None:
    module, _, _ = ci
    tfvars = 'patient_given_name = "Ann"\n'
    module.check_redaction("annotations = {}  Annual", tfvars)
    with pytest.raises(module.CiError):
        module.check_redaction('given = "Ann"', tfvars)


# --- plan ---------------------------------------------------------------------------------------


def test_plan_records_the_redacted_text_and_metadata_outside_the_repo(ci, tmp_path: Path) -> None:
    module, fake, repo = ci
    record = record_plan(module, tmp_path)
    assert (record / "plan.txt").read_text() == (fake / "plan.txt").read_text()
    meta = json.loads((record / "meta.json").read_text())
    assert meta == {
        "env": "prod",
        "digest": module.plan_digest(PLAN)[0],
        "changes": 2,  # the no-op does not count
        "tfvars_generation": "1700000000000001",
        "commit": SHA,
    }
    [plan_args] = tf_calls(fake, "plan")
    out = Path(next(a for a in plan_args if a.startswith("-out="))[5:])
    assert repo not in out.parents, "the binary plan must not be written into the checkout"
    assert Path(os.environ["RUNNER_TEMP"]) in out.parents
    assert not out.exists(), "the binary plan must be deleted when the step ends"
    assert not list(record.glob("tfplan*")), "the record must not hold the binary plan"
    # The planner: no lock (it cannot write the lock object).
    assert "-lock=false" in plan_args and not any(a.startswith("-lock-timeout") for a in plan_args)
    # Nothing important travels in step outputs (a masked value would blank them).
    assert outputs() == {}


def test_plan_with_the_lock_by_default(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    assert module.main(["plan", "--env", "dev", "--record", str(tmp_path / "r")]) == 0
    [plan_args] = tf_calls(fake, "plan")
    assert "-lock-timeout=10m" in plan_args and "-lock=false" not in plan_args


def test_plan_fails_and_records_nothing_when_the_text_leaks(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    (fake / "plan.txt").write_text('  + value = "alerts@example.com"\n')
    assert module.main(["plan", "--env", "dev", "--record", str(tmp_path / "r")]) == 1
    assert not (tmp_path / "r").exists()


def test_plan_fails_when_changes_have_no_text(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    (fake / "plan.txt").write_text("   \n")
    assert module.main(["plan", "--env", "dev", "--record", str(tmp_path / "r")]) == 1


def test_plan_needs_a_recorded_generation(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    module.generation_path("dev").unlink()
    assert module.main(["plan", "--env", "dev", "--record", str(tmp_path / "r")]) == 1
    assert tf_calls(fake, "plan") == []


# --- the digest ---------------------------------------------------------------------------------


def test_digest_ignores_order_and_no_ops(ci) -> None:
    module, _, _ = ci
    reordered = copy.deepcopy(PLAN)
    reordered["resource_changes"].reverse()
    assert module.plan_digest(PLAN) == module.plan_digest(reordered)
    assert module.plan_digest(PLAN)[1] == 2


def test_digest_changes_with_a_non_sensitive_value(ci) -> None:
    module, _, _ = ci
    changed = copy.deepcopy(PLAN)
    changed["resource_changes"][0]["change"]["after"]["max_instance_count"] = 5
    assert module.plan_digest(changed)[0] != module.plan_digest(PLAN)[0]
    changed_output = copy.deepcopy(PLAN)
    changed_output["output_changes"]["region"] = {
        "actions": ["update"],
        "before": "a",
        "after": "b",
    }
    assert module.plan_digest(changed_output)[0] != module.plan_digest(PLAN)[0]


def test_digest_ignores_drift_the_plan_overwrites(ci) -> None:
    module, _, _ = ci
    # Only `before` (the refreshed value) differs: the outcome is the same, so is the digest.
    drifted = copy.deepcopy(PLAN)
    drifted["resource_changes"][2]["change"]["before"]["x"] = 7
    assert module.plan_digest(drifted)[0] == module.plan_digest(PLAN)[0]


def test_digest_does_not_carry_sensitive_values(ci) -> None:
    module, _, _ = ci
    # Two plans that differ only in a sensitive value have the same digest: the hash input has a
    # placeholder there, so the digest cannot be used to test guesses of the patient ID.
    other = copy.deepcopy(PLAN)
    other["resource_changes"][0]["change"]["after"]["env"]["PATIENT_ID"] = "SYN-9999"
    assert module.plan_digest(other)[0] == module.plan_digest(PLAN)[0]
    assert module._redact({"a": "x", "b": ["y", "z"]}, {"b": [False, True]}) == {
        "a": "x",
        "b": ["y", "(sensitive)"],
    }


# --- apply ---------------------------------------------------------------------------------------


def test_apply_applies_the_plan_it_made(ci) -> None:
    module, fake, _ = ci
    assert module.main(["apply", "--env", "dev"]) == 0
    [apply_args] = tf_calls(fake, "apply")
    [plan_args] = tf_calls(fake, "plan")
    assert apply_args[-1] == next(a for a in plan_args if a.startswith("-out="))[5:]
    assert "-lock-timeout=10m" in plan_args


def test_apply_with_the_reviewed_plan_applies(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    record = record_plan(module, tmp_path)
    assert module.main(["apply", "--env", "prod", "--reviewed", str(record)]) == 0
    assert len(tf_calls(fake, "apply")) == 1


def test_apply_refuses_a_changed_value(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    record = record_plan(module, tmp_path)
    changed = copy.deepcopy(PLAN)
    changed["resource_changes"][2]["change"]["after"]["x"] = 3
    (fake / "plan.json").write_text(json.dumps(changed))
    assert module.main(["apply", "--env", "prod", "--reviewed", str(record)]) == 1
    assert tf_calls(fake, "apply") == []


def test_apply_refuses_another_tfvars_generation(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    record = record_plan(module, tmp_path)
    module.generation_path("prod").write_text("1700000000000002")
    assert module.main(["apply", "--env", "prod", "--reviewed", str(record)]) == 1
    assert tf_calls(fake, "apply") == []


def test_apply_refuses_another_commit(ci, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, fake, _ = ci
    record = record_plan(module, tmp_path)
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    assert module.main(["apply", "--env", "prod", "--reviewed", str(record)]) == 1
    assert tf_calls(fake, "apply") == []


@pytest.mark.parametrize("broken", ["missing", "not-json", "other-env", "bad-digest"])
def test_apply_refuses_a_broken_reviewed_record(ci, tmp_path: Path, broken: str) -> None:
    module, fake, _ = ci
    record = record_plan(module, tmp_path)
    meta = json.loads((record / "meta.json").read_text())
    if broken == "missing":
        (record / "meta.json").unlink()
    elif broken == "not-json":
        (record / "meta.json").write_text("{")
    elif broken == "other-env":
        (record / "meta.json").write_text(json.dumps({**meta, "env": "dev"}))
    else:
        (record / "meta.json").write_text(json.dumps({**meta, "digest": ""}))
    count = len(tf_calls(fake, "plan"))
    assert module.main(["apply", "--env", "prod", "--reviewed", str(record)]) == 1
    assert tf_calls(fake, "apply") == [] and len(tf_calls(fake, "plan")) == count


def test_a_failed_plan_applies_nothing(ci) -> None:
    module, fake, _ = ci
    (fake / "plan_rc").write_text("1")
    assert module.main(["apply", "--env", "dev"]) == 1
    assert tf_calls(fake, "apply") == []


# --- init, stack-is-up -------------------------------------------------------------------------


def test_init_gives_only_the_bucket(ci) -> None:
    module, fake, repo = ci
    assert module.main(["init", "--env", "prod"]) == 0
    [call] = [c for c in calls(fake, "terraform") if c["args"][0] == "init"]
    assert "-backend-config=bucket=example-project-tfstate" in call["args"]
    assert Path(call["cwd"]) == repo / "infra" / "envs" / "prod"


@pytest.mark.parametrize(
    "state,up",
    [
        ("module.stack.module.phr.google_cloud_run_v2_service.fhir_proxy\n", "true"),
        ("module.stack.module.project_base.google_compute_network.this\n", "false"),
        ("", "false"),
    ],
)
def test_stack_is_up_only_after_the_first_full_apply(ci, state: str, up: str) -> None:
    module, fake, _ = ci
    (fake / "state").write_text(state)
    assert module.main(["stack-is-up", "--env", "dev"]) == 0
    assert outputs()["up"] == up
    if up == "false":
        assert (
            "dev is not deployed: nothing was planned or applied"
            in Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
        )


def test_an_unreadable_state_is_an_error_not_down(ci) -> None:
    module, fake, _ = ci
    (fake / "state_rc").write_text("1")
    assert module.main(["stack-is-up", "--env", "dev"]) == 1
    assert "up" not in outputs()


# --- destroy -------------------------------------------------------------------------------------

UP = (
    "module.stack.module.phr.google_cloud_run_v2_service.fhir_proxy\n"
    "module.stack.module.edge_vm.google_compute_instance.this\n"
)
LEFTOVERS = (
    "module.stack.module.project_base.google_compute_subnetwork.serverless\n"
    "module.stack.module.project_base.google_compute_network.this\n"
)


def test_destroy_of_an_empty_state_destroys_nothing_and_says_so(ci) -> None:
    module, fake, _ = ci
    (fake / "state").write_text("")
    assert module.main(["destroy", "--env", "dev"]) == 0
    assert tf_calls(fake, "destroy") == []
    text = Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
    assert "nothing to destroy" in text and "nothing is left" not in text


def test_destroy_leaves_a_bring_up_in_progress_alone(ci) -> None:
    module, fake, _ = ci
    (fake / "state").write_text(
        LEFTOVERS + 'module.stack.module.identity.google_service_account.this["edge-vm"]\n'
    )
    assert module.main(["destroy", "--env", "dev"]) == 0
    assert tf_calls(fake, "destroy") == []
    assert "not destroyed" in Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()


def test_destroy_of_a_deployed_dev_with_nothing_left_passes(ci) -> None:
    module, fake, _ = ci
    (fake / "state").write_text(UP)
    (fake / "state_after").write_text("")
    assert module.main(["destroy", "--env", "dev", "--final"]) == 0
    [args] = tf_calls(fake, "destroy")
    assert "-auto-approve" in args
    assert "destroyed 2 resource(s)" in Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()


def test_destroy_allows_the_subnet_and_vpc_until_the_retry(ci) -> None:
    module, fake, _ = ci
    (fake / "state").write_text(UP + LEFTOVERS)
    (fake / "destroy_rc").write_text("1")
    (fake / "state_after").write_text(LEFTOVERS)
    assert module.main(["destroy", "--env", "dev"]) == 0
    (fake / "destroyed").unlink()
    assert module.main(["destroy", "--env", "dev", "--final"]) == 1


def test_the_retry_run_destroys_the_leftovers(ci) -> None:
    module, fake, _ = ci
    (fake / "state").write_text(LEFTOVERS)  # fhir-proxy is gone: only the network waited
    (fake / "state_after").write_text("")
    assert module.main(["destroy", "--env", "dev", "--final"]) == 0
    assert len(tf_calls(fake, "destroy")) == 1


def test_destroy_fails_on_any_other_leftover(ci) -> None:
    module, fake, _ = ci
    (fake / "state").write_text(UP)
    (fake / "destroy_rc").write_text("1")
    (fake / "state_after").write_text(
        LEFTOVERS + "module.stack.module.edge_vm.google_compute_instance.this\n"
    )
    assert module.main(["destroy", "--env", "dev"]) == 1


def test_a_failed_destroy_with_nothing_left_is_still_a_failure(ci) -> None:
    module, fake, _ = ci
    (fake / "state").write_text(UP)
    (fake / "destroy_rc").write_text("1")
    (fake / "state_after").write_text("")
    assert module.main(["destroy", "--env", "dev"]) == 1


def test_prod_is_never_destroyed(ci) -> None:
    module, fake, _ = ci
    assert module.main(["destroy", "--env", "prod"]) == 1
    assert calls(fake) == []


# --- the page ------------------------------------------------------------------------------------


def test_export_page_config_hands_over_firebase_json_and_the_project(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    (fake / "output_firebase_json").write_text(
        json.dumps('{"hosting": {"site": "example-project"}}')
    )
    (fake / "output_project_id").write_text('"example-project"')
    out = tmp_path / "page" / "page.json"
    assert module.main(["export-page-config", "--env", "dev", "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == {
        "project_id": "example-project",
        "firebase_json": '{"hosting": {"site": "example-project"}}',
    }


def test_export_page_config_needs_the_outputs(ci, tmp_path: Path) -> None:
    module, _, _ = ci
    out = tmp_path / "page.json"
    assert module.main(["export-page-config", "--env", "dev", "--out", str(out)]) == 1
    assert not out.exists()


def write_page_config(tmp_path: Path) -> Path:
    config = tmp_path / "page.json"
    config.write_text(
        json.dumps(
            {"project_id": "example-project", "firebase_json": '{"hosting": {"public": "dist"}}'}
        )
    )
    return config


def test_deploy_page_installs_the_locked_cli_and_deploys(ci, tmp_path: Path) -> None:
    module, fake, repo = ci
    assert module.main(["deploy-page", "--config", str(write_page_config(tmp_path))]) == 0
    assert (repo / "web" / "firebase.json").read_text() == '{"hosting": {"public": "dist"}}'
    npm, npx = calls(fake, "npm"), calls(fake, "npx")
    assert npm[0]["args"] == ["ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    assert Path(npm[0]["cwd"]) == repo / "deploy-tools"
    assert npx[0]["args"] == [
        "--no-install", "firebase", "deploy", "--only", "hosting", "--project", "example-project",
        "--config", str(repo / "web" / "firebase.json"), "--non-interactive",
    ]  # fmt: skip
    assert npm[0]["ignore_scripts"] == npx[0]["ignore_scripts"] == "true"
    assert calls(fake, "terraform") == [], "the page deploy runs no Terraform"


def test_deploy_page_needs_the_built_page(ci, tmp_path: Path) -> None:
    module, fake, repo = ci
    (repo / "web" / "dist" / "index.html").unlink()
    assert module.main(["deploy-page", "--config", str(write_page_config(tmp_path))]) == 1
    assert calls(fake) == []


def test_deploy_page_refuses_a_bad_configuration(ci, tmp_path: Path) -> None:
    module, fake, _ = ci
    config = tmp_path / "page.json"
    config.write_text(json.dumps({"project_id": "Not A Project", "firebase_json": "{}"}))
    assert module.main(["deploy-page", "--config", str(config)]) == 1
    assert calls(fake) == []


# --- fetch-tfvars, cleanup -----------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str]):
        super().__init__(body)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_storage(monkeypatch, module, generation="1700000000000005", body=TFVARS):
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        headers = {"x-goog-generation": generation} if generation else {}
        return FakeResponse(body.encode(), headers)

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("GCP_ACCESS_TOKEN", "fake-token-for-test")
    return seen


def test_fetch_tfvars_writes_a_private_file_records_the_generation_and_masks(
    ci, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    module, _, repo = ci
    seen = fake_storage(monkeypatch, module)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    target = repo / "infra" / "envs" / "dev" / "terraform.tfvars"
    target.chmod(0o644)  # an existing, too-open file is tightened
    assert module.main(["fetch-tfvars", "--env", "dev"]) == 0
    assert seen["url"] == (
        "https://storage.googleapis.com/storage/v1/b/example-project-tfstate/o/ci%2Fdev.tfvars?alt=media"
    )
    assert seen["auth"] == "Bearer fake-token-for-test"
    assert target.read_text() == TFVARS
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert module.generation_path("dev").read_text() == "1700000000000005"
    printed = capsys.readouterr().out
    assert "::add-mask::SYN-0042" in printed and "::add-mask::alerts@example.com" in printed
    assert "fake-token-for-test" not in printed
    assert printed.count("SYN-0042") == 1


def test_fetch_tfvars_asks_for_and_checks_the_reviewed_generation(
    ci, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _, _ = ci
    seen = fake_storage(monkeypatch, module, generation="1700000000000007")
    assert module.main(["fetch-tfvars", "--env", "prod", "--generation", "1700000000000007"]) == 0
    assert seen["url"].endswith("?alt=media&generation=1700000000000007")
    fake_storage(monkeypatch, module, generation="1700000000000008")
    assert module.main(["fetch-tfvars", "--env", "prod", "--generation", "1700000000000007"]) == 1


@pytest.mark.parametrize("generation", ["", "abc", "1 2"])
def test_fetch_tfvars_refuses_a_bad_generation(
    ci, monkeypatch: pytest.MonkeyPatch, generation: str
) -> None:
    module, _, _ = ci
    fake_storage(monkeypatch, module)
    assert module.main(["fetch-tfvars", "--env", "prod", "--generation", generation]) == 1


def test_fetch_tfvars_needs_the_generation_header(ci, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, _ = ci
    fake_storage(monkeypatch, module, generation=None)
    assert module.main(["fetch-tfvars", "--env", "dev"]) == 1


def test_fetch_tfvars_refuses_a_heredoc_sensitive_value(
    ci, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _, repo = ci
    fake_storage(monkeypatch, module, body="alert_email = <<EOT\nalerts@example.com\nEOT\n")
    target = repo / "infra" / "envs" / "dev" / "terraform.tfvars"
    target.unlink()
    assert module.main(["fetch-tfvars", "--env", "dev"]) == 1
    assert not target.exists()


@pytest.mark.parametrize("error", ["http", "network", "timeout"])
def test_fetch_tfvars_reports_errors_without_a_traceback_or_the_token(
    ci, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, error: str
) -> None:
    module, _, _ = ci

    def fail(request, timeout):
        if error == "http":
            raise module.urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        if error == "network":
            raise module.urllib.error.URLError("no route")
        raise TimeoutError

    monkeypatch.setattr(module.urllib.request, "urlopen", fail)
    monkeypatch.setenv("GCP_ACCESS_TOKEN", "fake-token-for-test")
    assert module.main(["fetch-tfvars", "--env", "prod"]) == 1
    printed = capsys.readouterr()
    assert "Traceback" not in printed.out + printed.err
    assert "fake-token-for-test" not in printed.out + printed.err
    assert "ci/prod.tfvars" in printed.out


def test_cleanup_deletes_the_tfvars_and_the_generation(ci) -> None:
    module, _, repo = ci
    assert module.main(["cleanup", "--env", "dev"]) == 0
    assert not (repo / "infra" / "envs" / "dev" / "terraform.tfvars").exists()
    assert not module.generation_path("dev").exists()
    assert module.main(["cleanup", "--env", "dev"]) == 0  # nothing left: still fine


# --- the GitHub checks ---------------------------------------------------------------------------


def fake_github(monkeypatch, module, answers: dict[str, object]):
    def api(path):
        if path not in answers:
            raise module.CiError(f"GitHub API {path}: HTTP 404")
        return copy.deepcopy(answers[path])

    monkeypatch.setattr(module, "github_api", api)


REPO = "/repos/example-owner/example-repo"


def good_environments(names=("prod-plan", "prod", "prod-page")) -> dict[str, object]:
    answers: dict[str, object] = {}
    for name in names:
        answers[f"{REPO}/environments/{name}"] = {
            "name": name,
            "can_admins_bypass": False,
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
            "protection_rules": [{"type": "branch_policy"}]
            + (
                [
                    {
                        "type": "required_reviewers",
                        "reviewers": [{"type": "User", "reviewer": {"id": 1}}],
                    }
                ]
                if name == "prod"
                else []
            ),
        }
        answers[f"{REPO}/environments/{name}/deployment-branch-policies"] = {
            "total_count": 1,
            "branch_policies": [{"id": 1, "name": "main", "type": "branch"}],
        }
    return answers


def test_check_environments_passes_the_required_settings(
    ci, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _, _ = ci
    fake_github(monkeypatch, module, good_environments())
    assert module.main(["check-environments", "--env", "prod"]) == 0
    fake_github(monkeypatch, module, good_environments(("dev-page",)))
    assert module.main(["check-environments", "--env", "dev"]) == 0


def test_check_environments_checks_every_environment_of_prod(
    ci, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _, _ = ci
    for missing in ("prod-plan", "prod", "prod-page"):
        answers = good_environments()
        del answers[f"{REPO}/environments/{missing}"]
        fake_github(monkeypatch, module, answers)
        assert module.main(["check-environments", "--env", "prod"]) == 1, missing


def test_check_environments_needs_an_env(ci) -> None:
    module, _, _ = ci
    assert module.main(["check-environments"]) == 1


# The reviewer's fuzz cases (T26 review 2, m2), applied to each Environment in turn: every one
# must fail; GitHub answers booleans, integers, lists and dicts, and nothing else may pass.
ENV_CASES = {
    "policy null": {"deployment_branch_policy": None},
    "protected true": {
        "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}
    },
    "both true": {
        "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": True}
    },
    "policy extra key": {
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
            "x": 1,
        }
    },
    "policy as 0/1": {
        "deployment_branch_policy": {"protected_branches": 0, "custom_branch_policies": 1}
    },
    "bypass missing": "DEL can_admins_bypass",
    "bypass null": {"can_admins_bypass": None},
    "bypass 0": {"can_admins_bypass": 0},
    "bypass 'false'": {"can_admins_bypass": "false"},
    "bypass true": {"can_admins_bypass": True},
    "rules null": {"protection_rules": None},
    "rules dict": {"protection_rules": {"type": "required_reviewers"}},
    "rules str": {"protection_rules": "x"},
    "rules [null]": {"protection_rules": [None]},
}
REVIEWER_CASES = {
    "reviewers empty": [],
    "reviewers str": "abc",
    "reviewers dict": {"a": 1},
    "reviewers [null]": [None],
    "reviewers [{}]": [{}],
    "reviewer type Bot": [{"type": "Bot", "reviewer": {"id": 1}}],
    "reviewer id str": [{"type": "User", "reviewer": {"id": "1"}}],
    "reviewer id bool": [{"type": "User", "reviewer": {"id": True}}],
    "reviewer without reviewer": [{"type": "Team"}],
    "one good, one bad": [{"type": "User", "reviewer": {"id": 1}}, None],
}
LIST_CASES = {
    "list two": {
        "total_count": 2,
        "branch_policies": [{"name": "main", "type": "branch"}, {"name": "*", "type": "branch"}],
    },
    "list count lies": {
        "total_count": 1,
        "branch_policies": [{"name": "main", "type": "branch"}, {"name": "*", "type": "branch"}],
    },
    "list paginated count 31": {
        "total_count": 31,
        "branch_policies": [{"name": "main", "type": "branch"}],
    },
    "list tag main": {"total_count": 1, "branch_policies": [{"name": "main", "type": "tag"}]},
    "list no type": {"total_count": 1, "branch_policies": [{"name": "main"}]},
    "list pattern main*": {
        "total_count": 1,
        "branch_policies": [{"name": "main*", "type": "branch"}],
    },
    "list Main": {"total_count": 1, "branch_policies": [{"name": "Main", "type": "branch"}]},
    "list policies dict": {"total_count": 1, "branch_policies": {"name": "main"}},
    "list policies null": {"total_count": 1, "branch_policies": None},
    "list policies [str]": {"total_count": 1, "branch_policies": ["main"]},
    "list missing count": {"branch_policies": [{"name": "main", "type": "branch"}]},
    "list count '1'": {"total_count": "1", "branch_policies": [{"name": "main", "type": "branch"}]},
    "list count True": {
        "total_count": True,
        "branch_policies": [{"name": "main", "type": "branch"}],
    },
}


@pytest.mark.parametrize("target", ["prod-plan", "prod", "prod-page"])
@pytest.mark.parametrize("case", sorted(ENV_CASES))
def test_check_environments_refuses_a_malformed_environment(
    ci, monkeypatch: pytest.MonkeyPatch, case: str, target: str
) -> None:
    module, _, _ = ci
    answers = good_environments()
    env = answers[f"{REPO}/environments/{target}"]
    change = ENV_CASES[case]
    if change == "DEL can_admins_bypass":
        del env["can_admins_bypass"]
    else:
        env.update(change)
    fake_github(monkeypatch, module, answers)
    assert module.main(["check-environments", "--env", "prod"]) == 1


@pytest.mark.parametrize("case", sorted(REVIEWER_CASES))
def test_check_environments_refuses_malformed_reviewers(
    ci, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    module, _, _ = ci
    answers = good_environments()
    answers[f"{REPO}/environments/prod"]["protection_rules"] = [
        {"type": "required_reviewers", "reviewers": REVIEWER_CASES[case]}
    ]
    fake_github(monkeypatch, module, answers)
    assert module.main(["check-environments", "--env", "prod"]) == 1


def test_check_environments_accepts_a_team_reviewer(ci, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, _ = ci
    answers = good_environments()
    answers[f"{REPO}/environments/prod"]["protection_rules"] = [
        {"type": "required_reviewers", "reviewers": [{"type": "Team", "reviewer": {"id": 7}}]}
    ]
    fake_github(monkeypatch, module, answers)
    assert module.main(["check-environments", "--env", "prod"]) == 0


@pytest.mark.parametrize("target", ["prod-plan", "prod", "prod-page"])
@pytest.mark.parametrize("case", sorted(LIST_CASES))
def test_check_environments_refuses_a_malformed_branch_policy_list(
    ci, monkeypatch: pytest.MonkeyPatch, case: str, target: str
) -> None:
    module, _, _ = ci
    answers = good_environments()
    answers[f"{REPO}/environments/{target}/deployment-branch-policies"] = copy.deepcopy(
        LIST_CASES[case]
    )
    fake_github(monkeypatch, module, answers)
    assert module.main(["check-environments", "--env", "prod"]) == 1


def test_check_environments_refuses_an_api_error_or_a_non_dict(
    ci, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _, _ = ci
    fake_github(monkeypatch, module, {})
    assert module.main(["check-environments", "--env", "dev"]) == 1


def test_github_api_needs_a_token(ci, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, _ = ci
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert module.main(["check-environments", "--env", "prod"]) == 1


def test_require_tip_passes_on_the_tip(ci, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, _ = ci
    fake_github(monkeypatch, module, {f"{REPO}/git/ref/heads/main": {"object": {"sha": SHA}}})
    assert module.main(["require-tip"]) == 0


@pytest.mark.parametrize("answer", [{"object": {"sha": "f" * 40}}, {"object": {}}, {}])
def test_require_tip_refuses_a_stale_commit(ci, monkeypatch: pytest.MonkeyPatch, answer) -> None:
    module, _, _ = ci
    fake_github(monkeypatch, module, {f"{REPO}/git/ref/heads/main": answer})
    assert module.main(["require-tip"]) == 1


def test_require_tip_needs_a_full_sha(ci, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, _ = ci
    fake_github(monkeypatch, module, {f"{REPO}/git/ref/heads/main": {"object": {"sha": SHA}}})
    monkeypatch.setenv("GITHUB_SHA", "abc")
    assert module.main(["require-tip"]) == 1


def test_github_api_reads_over_https_with_the_token(ci, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, _ = ci
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(b'{"object": {"sha": "x"}}', {})

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("GH_TOKEN", "fake-github-token")
    assert module.github_api("/repos/a/b/git/ref/heads/main") == {"object": {"sha": "x"}}
    assert seen == {
        "url": "https://api.github.com/repos/a/b/git/ref/heads/main",
        "auth": "Bearer fake-github-token",
    }
