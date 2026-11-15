"""Behaviour of infra/envs/ci/tf_ci.py, the Terraform steps of the CI/CD workflows (T26).

The script runs in-process with its repository root pointed at a temporary directory, and with
fake `terraform`, `make` and `npm` programs first on PATH. Each fake records its arguments (and
the npm settings) and answers from files the test writes. Nothing reaches Google, and no test
writes into the real infra/envs roots. All values are synthetic.
"""

from __future__ import annotations

import base64
import hashlib
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
    f.write(json.dumps({"cwd": os.getcwd(), "args": args}) + "\n")
def read(name, default=""):
    p = fake / name
    return p.read_text() if p.exists() else default
cmd = args[0]
if cmd == "state":
    after = fake / "destroyed"
    sys.stdout.write(read("state_after" if after.exists() else "state"))
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
name = Path(sys.argv[0]).name
with open(fake / "calls.log", "a") as f:
    f.write(json.dumps({"tool": name, "cwd": os.getcwd(), "args": sys.argv[1:],
                        "ignore_scripts": os.environ.get("NPM_CONFIG_IGNORE_SCRIPTS")}) + "\n")
"""


def plan_json(*changes: tuple[list[str], str]) -> str:
    return json.dumps(
        {"resource_changes": [{"address": a, "change": {"actions": acts}} for acts, a in changes]}
    )


CHANGES = (
    (["create"], "module.stack.module.phr.google_cloud_run_v2_service.cuff_sim"),
    (["no-op"], 'module.stack.module.identity.google_service_account.this["edge-vm"]'),
    (["delete", "create"], "module.stack.module.edge_vm.google_compute_instance.this"),
)
EXPECTED_DIGEST = hashlib.sha256(
    b"create module.stack.module.phr.google_cloud_run_v2_service.cuff_sim\n"
    b"delete,create module.stack.module.edge_vm.google_compute_instance.this"
).hexdigest()


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
    (repo / "web").mkdir()
    monkeypatch.setattr(module, "REPO", repo)
    monkeypatch.setattr(module, "ENVS", repo / "infra" / "envs")

    fake = tmp_path / "fake"
    bin_dir = fake / "bin"
    bin_dir.mkdir(parents=True)
    for name, body in (
        ("terraform", FAKE_TERRAFORM),
        ("make", FAKE_RECORDER),
        ("npm", FAKE_RECORDER),
    ):
        path = bin_dir / name
        path.write_text(body.replace("/usr/bin/env python3", sys.executable, 1))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    (fake / "plan.json").write_text(plan_json(*CHANGES))
    (fake / "plan.txt").write_text('  + env { value = (sensitive value) }\n  + name = "cuff-sim"\n')

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    outputs = tmp_path / "github_output"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(fake))
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("TF_STATE_BUCKET", "example-project-tfstate")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return module, fake, repo


def calls(fake: Path) -> list[dict]:
    log = fake / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def tf_calls(fake: Path, command: str) -> list[list[str]]:
    return [c["args"] for c in calls(fake) if "tool" not in c and c["args"][0] == command]


def outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


# --- the redaction check -------------------------------------------------------------------------


def test_sensitive_values_are_read_from_the_tfvars(ci) -> None:
    module, _, _ = ci
    assert module.sensitive_values(TFVARS) == {
        "patient_id": "SYN-0042",
        "alert_email": "alerts@example.com",
    }
    assert module.sensitive_values('patient_given_name = "A \\"B\\""\n') == {
        "patient_given_name": 'A "B"'
    }


def test_redaction_passes_a_redacted_plan_and_refuses_a_leak(ci) -> None:
    module, _, _ = ci
    module.check_redaction("patient = (sensitive value)\nauthority = PULSEBRIDGE", TFVARS)
    for leaked in ('PATIENT_ID = "SYN-0042"', "to: alerts@example.com"):
        with pytest.raises(module.CiError) as e:
            module.check_redaction(leaked, TFVARS)
        # The message names the variable, never its value.
        assert "SYN-0042" not in str(e.value) and "alerts@example.com" not in str(e.value)


def test_redaction_matches_whole_words_only(ci) -> None:
    module, _, _ = ci
    tfvars = 'patient_given_name = "Ann"\n'
    module.check_redaction("annotations = {}  Annual", tfvars)
    with pytest.raises(module.CiError):
        module.check_redaction('given = "Ann"', tfvars)


def test_plan_fails_and_reports_nothing_when_the_text_leaks(ci) -> None:
    module, fake, _ = ci
    (fake / "plan.txt").write_text('  + value = "alerts@example.com"\n')
    assert module.main(["plan", "--env", "dev"]) == 1
    out = Path(os.environ["GITHUB_OUTPUT"])
    assert not out.exists() or "text_b64" not in out.read_text()


# --- plan and apply ------------------------------------------------------------------------------


def test_plan_reports_digest_count_and_text_and_keeps_the_plan_out_of_the_repo(ci) -> None:
    module, fake, repo = ci
    assert module.main(["plan", "--env", "dev"]) == 0
    got = outputs(Path(os.environ["GITHUB_OUTPUT"]))
    assert got["digest"] == EXPECTED_DIGEST
    assert got["changes"] == "2"  # the no-op does not count
    assert base64.b64decode(got["text_b64"]).decode() == (fake / "plan.txt").read_text()
    [plan_args] = tf_calls(fake, "plan")
    out = Path(next(a for a in plan_args if a.startswith("-out="))[5:])
    assert repo not in out.parents, "the binary plan must not be written into the checkout"
    assert Path(os.environ["RUNNER_TEMP"]) in out.parents
    assert not out.exists(), "the binary plan must be deleted when the step ends"
    assert "-lock-timeout=10m" in plan_args


def test_plan_text_is_truncated_for_the_comment(ci) -> None:
    module, fake, _ = ci
    (fake / "plan.txt").write_text("x" * (module.MAX_PLAN_CHARS + 500))
    assert module.main(["plan", "--env", "dev"]) == 0
    text = base64.b64decode(outputs(Path(os.environ["GITHUB_OUTPUT"]))["text_b64"]).decode()
    assert len(text) < module.MAX_PLAN_CHARS + 200 and "truncated" in text


def test_digest_ignores_values_and_no_ops(ci) -> None:
    module, _, _ = ci
    a = module.plan_digest(json.loads(plan_json(*CHANGES)))
    b = module.plan_digest(json.loads(plan_json(*reversed(CHANGES))))
    assert a == b == (EXPECTED_DIGEST, 2)
    c = module.plan_digest(json.loads(plan_json((["update"], CHANGES[0][1]))))
    assert c[0] != EXPECTED_DIGEST


def test_apply_applies_the_plan_it_made(ci) -> None:
    module, fake, _ = ci
    assert module.main(["apply", "--env", "dev"]) == 0
    [apply_args] = tf_calls(fake, "apply")
    [plan_args] = tf_calls(fake, "plan")
    assert apply_args[-1] == next(a for a in plan_args if a.startswith("-out="))[5:]


def test_apply_with_the_reviewed_digest_applies(ci) -> None:
    module, fake, _ = ci
    assert module.main(["apply", "--env", "prod", "--expect-digest", EXPECTED_DIGEST]) == 0
    assert len(tf_calls(fake, "apply")) == 1


def test_apply_refuses_a_plan_that_differs_from_the_reviewed_one(ci) -> None:
    module, fake, _ = ci
    assert module.main(["apply", "--env", "prod", "--expect-digest", "0" * 64]) == 1
    assert tf_calls(fake, "apply") == []


@pytest.mark.parametrize("digest", ["", "abc", EXPECTED_DIGEST.upper()])
def test_apply_refuses_an_empty_or_malformed_digest(ci, digest: str) -> None:
    module, fake, _ = ci
    assert module.main(["apply", "--env", "prod", "--expect-digest", digest]) == 1
    assert tf_calls(fake, "plan") == [] and tf_calls(fake, "apply") == []


def test_a_failed_plan_applies_nothing(ci) -> None:
    module, fake, _ = ci
    (fake / "plan_rc").write_text("1")
    assert module.main(["apply", "--env", "dev"]) == 1
    assert tf_calls(fake, "apply") == []


# --- init, stack-is-up ---------------------------------------------------------------------------


def test_init_gives_only_the_bucket(ci) -> None:
    module, fake, repo = ci
    assert module.main(["init", "--env", "prod"]) == 0
    [call] = [c for c in calls(fake) if c["args"][0] == "init"]
    assert "-backend-config=bucket=example-project-tfstate" in call["args"]
    assert Path(call["cwd"]) == repo / "infra" / "envs" / "prod"


def test_init_needs_the_bucket(ci, monkeypatch: pytest.MonkeyPatch) -> None:
    module, fake, _ = ci
    monkeypatch.delenv("TF_STATE_BUCKET")
    assert module.main(["init", "--env", "dev"]) == 1
    assert calls(fake) == []


@pytest.mark.parametrize(
    "state,up",
    [
        ("module.stack.module.phr.google_cloud_run_v2_service.fhir_proxy\n", "true"),
        # Targeted steps 3 to 6 done, step 8 not yet: CI must not take over.
        ("module.stack.module.project_base.google_compute_network.this\n", "false"),
        ("", "false"),
    ],
)
def test_stack_is_up_only_after_the_first_full_apply(ci, state: str, up: str) -> None:
    module, fake, _ = ci
    (fake / "state").write_text(state)
    assert module.main(["stack-is-up", "--env", "dev"]) == 0
    assert outputs(Path(os.environ["GITHUB_OUTPUT"]))["up"] == up


# --- destroy -------------------------------------------------------------------------------------

LEFTOVERS = (
    "module.stack.module.project_base.google_compute_subnetwork.serverless\n"
    "module.stack.module.project_base.google_compute_network.this\n"
)


def test_destroy_with_nothing_left_passes(ci) -> None:
    module, fake, _ = ci
    (fake / "state_after").write_text("")
    assert module.main(["destroy", "--env", "dev", "--final"]) == 0
    [args] = tf_calls(fake, "destroy")
    assert "-auto-approve" in args


def test_destroy_allows_the_subnet_and_vpc_until_the_retry(ci) -> None:
    module, fake, _ = ci
    (fake / "destroy_rc").write_text("1")  # terraform reports the subnet it could not delete
    (fake / "state_after").write_text(LEFTOVERS)
    assert module.main(["destroy", "--env", "dev"]) == 0
    assert module.main(["destroy", "--env", "dev", "--final"]) == 1


def test_destroy_fails_on_any_other_leftover(ci) -> None:
    module, fake, _ = ci
    (fake / "destroy_rc").write_text("1")
    (fake / "state_after").write_text(
        LEFTOVERS + "module.stack.module.edge_vm.google_compute_instance.this\n"
    )
    assert module.main(["destroy", "--env", "dev"]) == 1


def test_prod_is_never_destroyed(ci) -> None:
    module, fake, _ = ci
    assert module.main(["destroy", "--env", "prod"]) == 1
    assert calls(fake) == []


# --- the page deploy -----------------------------------------------------------------------------


def test_deploy_page_writes_firebase_json_builds_and_deploys_with_the_pinned_cli(ci) -> None:
    module, fake, repo = ci
    (fake / "output_firebase_json").write_text('{"hosting": {"site": "example-project"}}')
    (fake / "output_project_id").write_text("example-project")
    assert module.main(["deploy-page", "--env", "dev"]) == 0
    assert (
        repo / "web" / "firebase.json"
    ).read_text() == '{"hosting": {"site": "example-project"}}'
    tools = [c for c in calls(fake) if "tool" in c]
    assert [t["tool"] for t in tools] == ["make", "npm"]
    assert tools[0]["args"] == ["web-build"]
    npm = tools[1]
    assert Path(npm["cwd"]) == repo / "web"
    assert npm["args"] == [
        "exec", "--yes", f"--package=firebase-tools@{module.FIREBASE_TOOLS_VERSION}", "--",
        "firebase", "deploy", "--only", "hosting", "--project", "example-project",
        "--non-interactive",
    ]  # fmt: skip
    assert npm["ignore_scripts"] == "true"


def test_deploy_page_needs_the_outputs(ci) -> None:
    module, fake, repo = ci
    assert module.main(["deploy-page", "--env", "dev"]) == 1
    assert not (repo / "web" / "firebase.json").exists()
    assert not [c for c in calls(fake) if c.get("tool") == "npm"]


# --- fetch-tfvars --------------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_tfvars_writes_a_private_file_and_masks_sensitive_values(
    ci, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    module, _, repo = ci
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(TFVARS.encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("GCP_ACCESS_TOKEN", "fake-token-for-test")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    target = repo / "infra" / "envs" / "dev" / "terraform.tfvars"
    target.unlink()
    assert module.main(["fetch-tfvars", "--env", "dev"]) == 0
    assert seen["url"] == (
        "https://storage.googleapis.com/storage/v1/b/example-project-tfstate/o/ci%2Fdev.tfvars?alt=media"
    )
    assert seen["auth"] == "Bearer fake-token-for-test"
    assert target.read_text() == TFVARS
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    printed = capsys.readouterr().out
    assert "::add-mask::SYN-0042" in printed and "::add-mask::alerts@example.com" in printed
    # Nothing else from the file is printed, and never the token.
    assert "fake-token-for-test" not in printed
    assert printed.count("SYN-0042") == 1


def test_fetch_tfvars_reports_a_missing_object_without_the_token(
    ci, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    module, _, _ = ci

    def not_found(request, timeout):
        raise module.urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(module.urllib.request, "urlopen", not_found)
    monkeypatch.setenv("GCP_ACCESS_TOKEN", "fake-token-for-test")
    assert module.main(["fetch-tfvars", "--env", "prod"]) == 1
    printed = capsys.readouterr().out
    assert "HTTP 404" in printed and "ci/prod.tfvars" in printed
    assert "fake-token-for-test" not in printed
