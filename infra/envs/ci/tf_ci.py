#!/usr/bin/env python3
"""The Terraform steps of PulseBridge's CI/CD workflows (T26), in one place.

The workflows in .github/workflows/terraform-*.yml authenticate (Workload Identity Federation,
google-github-actions/auth) and then call this script. Keeping the logic here rather than in YAML
means it is readable top to bottom, runs the same on a laptop, and is tested
(tests/tooling/test_tf_ci.py runs it against a fake `terraform`).

Commands (each takes --env dev|prod and works in infra/envs/<env>):

  fetch-tfvars   download the environment's terraform.tfvars from the state bucket
  init           terraform init against the GCS backend
  stack-is-up    whether a person has done the first full apply (runbook step 8)
  plan           plan, check the human-readable plan for sensitive values, report a digest
  apply          plan again, optionally require the same digest as a reviewed plan, apply
  destroy        dev only: destroy, then accept only the documented network leftovers
  deploy-page    build web/ and deploy it to Firebase Hosting with firebase.json from Terraform

Environment: TF_STATE_BUCKET (the bucket bootstrap created; from a repository variable),
GCP_ACCESS_TOKEN (fetch-tfvars only: a short-lived deployer access token from the auth step),
GITHUB_OUTPUT / GITHUB_STEP_SUMMARY (set by GitHub Actions; optional elsewhere).

Nothing here prints a credential or a sensitive tfvars value. The values of the sensitive
variables are registered with the runner's log masking (`::add-mask::`) as soon as the tfvars
are read, and a plan that shows any of them fails before it is posted anywhere.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ENVS = REPO / "infra" / "envs"
ENVIRONMENTS = ("dev", "prod")

# Where each environment's tfvars live: an object in that environment's own state bucket, next
# to the state, readable by the same deployer (bootstrap grants it objectAdmin on that bucket
# only). The tfvars name the project, the patient and the alert address, so they are kept out of
# GitHub entirely (infra/envs/README.md, "CI/CD").
TFVARS_OBJECT = "ci/{env}.tfvars"

# Root variables that are `sensitive` (check_env_roots.py checks the declarations). A
# human-readable plan must never show their values.
SENSITIVE_VARIABLES = ("patient_id", "patient_family_name", "patient_given_name", "alert_email")

# Created only by the first FULL apply (runbook step 8): its presence in the state means a person
# has finished the targeted bring-up steps 3 to 7, so CI may take over. Before that (or after the
# nightly destroy), CI plans and applies nothing.
STACK_UP_MARKER = "module.stack.module.phr.google_cloud_run_v2_service.fhir_proxy"

# What a dev destroy may leave behind: after the Cloud Run services that used Direct VPC egress
# are deleted, Google asks to "wait 1-2 hours" before their subnet can be deleted, and the VPC
# cannot go before its subnet. Both are free. The next destroy run deletes them.
ALLOWED_DESTROY_LEFTOVERS = (
    "module.stack.module.project_base.google_compute_subnetwork.serverless",
    "module.stack.module.project_base.google_compute_network.this",
)

# The Firebase CLI version the page deploy runs (an exact version, never a range or `latest`).
FIREBASE_TOOLS_VERSION = "15.34.2"

# GitHub refuses comments over 65,536 characters; leave room for the header.
MAX_PLAN_CHARS = 60_000

TFVARS_STRING = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')


class CiError(Exception):
    """A failure with a message that is safe to print (it never carries a sensitive value)."""


# --- small helpers -----------------------------------------------------------------------------


def root_dir(env: str) -> Path:
    if env not in ENVIRONMENTS:
        raise CiError(f"unknown environment {env!r}; expected one of {ENVIRONMENTS}")
    return ENVS / env


def run(
    args: list[str], cwd: Path, *, capture: bool = False, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run a command; with capture, return its stdout instead of letting it print."""
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"}
    env.update(extra_env or {})
    return subprocess.run(  # noqa: S603 - fixed argument lists, no shell
        args, cwd=cwd, env=env, check=False, text=True, capture_output=capture
    )


def terraform(env: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return run(["terraform", *args], root_dir(env), capture=capture)


def set_output(name: str, value: str) -> None:
    """Write a step output (GITHUB_OUTPUT). Values are single-line by construction."""
    if "\n" in value:
        raise CiError(f"output {name} must be a single line")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"output {name}: {value if len(value) < 120 else f'<{len(value)} characters>'}")


def summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def tfvars_path(env: str) -> Path:
    return root_dir(env) / "terraform.tfvars"


def sensitive_values(tfvars_text: str) -> dict[str, str]:
    """The values of the sensitive variables, as written in the tfvars (plain string values)."""
    found: dict[str, str] = {}
    for line in tfvars_text.splitlines():
        m = TFVARS_STRING.match(line)
        if m and m.group(1) in SENSITIVE_VARIABLES and m.group(2):
            # HCL string escapes: only \" and \\ are expected in these values.
            found[m.group(1)] = re.sub(r"\\(.)", r"\1", m.group(2))
    return found


# --- commands ----------------------------------------------------------------------------------


def fetch_tfvars(env: str) -> None:
    bucket = os.environ.get("TF_STATE_BUCKET", "")
    token = os.environ.get("GCP_ACCESS_TOKEN", "")
    if not bucket or not token:
        raise CiError("TF_STATE_BUCKET and GCP_ACCESS_TOKEN must be set")
    name = urllib.parse.quote(TFVARS_OBJECT.format(env=env), safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket)}/o/{name}?alt=media"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https only
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise CiError(
            f"could not read gs://{bucket}/{TFVARS_OBJECT.format(env=env)}: HTTP {e.code} "
            '(infra/envs/README.md, "CI/CD", says how to upload it)'
        ) from None
    mask(body)
    path = tfvars_path(env)
    # 0600: the file names the patient. It is git-ignored (*.tfvars) and the runner is discarded.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"wrote {path.relative_to(REPO)} ({len(body.splitlines())} lines)")


def mask(tfvars_text: str) -> None:
    """Ask the runner to mask the sensitive values in every later log line of this job."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for value in sensitive_values(tfvars_text).values():
        print(f"::add-mask::{value}")


def init(env: str) -> None:
    bucket = os.environ.get("TF_STATE_BUCKET", "")
    if not bucket:
        raise CiError("TF_STATE_BUCKET must be set")
    # The prefix (dev or prod) is fixed in the root's backend.tf; only the bucket is given here.
    result = terraform(env, "init", "-input=false", "-no-color", f"-backend-config=bucket={bucket}")
    if result.returncode != 0:
        raise CiError("terraform init failed")


def stack_is_up(env: str) -> bool:
    result = terraform(env, "state", "list", capture=True)
    if result.returncode != 0:
        raise CiError("terraform state list failed")
    up = STACK_UP_MARKER in result.stdout.split()
    set_output("up", "true" if up else "false")
    if not up:
        summary(
            f"**{env}: not deployed yet (or destroyed).** CI changes nothing until a person has "
            "done the first full apply (infra/envs/README.md, runbook step 8)."
        )
    return up


def plan_digest(plan_json: dict) -> tuple[str, int]:
    """A digest of WHAT the plan changes: the sorted "actions address" lines of every resource
    that is not a no-op. Values are left out, so the digest reveals nothing; two plans with the
    same digest create, change and destroy exactly the same resources."""
    lines = sorted(
        f"{','.join(rc['change']['actions'])} {rc['address']}"
        for rc in plan_json.get("resource_changes", [])
        if rc["change"]["actions"] != ["no-op"]
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest(), len(lines)


def check_redaction(plan_text: str, tfvars_text: str) -> None:
    """Fail if the human-readable plan shows the value of a sensitive variable. Terraform should
    already print "(sensitive value)"; this guards against a value reaching a non-sensitive
    attribute through a wiring mistake, before the plan is posted on a pull request."""
    # Whole-word matches, so a short name ("Ann") is not found inside another word.
    leaked = [
        name
        for name, value in sensitive_values(tfvars_text).items()
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", plan_text)
    ]
    if leaked:
        raise CiError(
            "the plan shows the value of sensitive variable(s) "
            f"{', '.join(sorted(leaked))}; it is not posted. Find where the value reaches a "
            "non-sensitive attribute."
        )


def make_plan(env: str, workdir: Path, *, destroy: bool = False) -> tuple[Path, str, int, str]:
    """Plan into workdir (never into the repository: a plan file holds sensitive values in
    clear). Returns the plan file, its digest, the number of changes and the redacted text."""
    plan_file = workdir / "tfplan"
    args = ["plan", "-input=false", "-no-color", "-lock-timeout=10m", f"-out={plan_file}"]
    if destroy:
        args.append("-destroy")
    if terraform(env, *args).returncode != 0:
        raise CiError("terraform plan failed")
    shown = terraform(env, "show", "-json", str(plan_file), capture=True)
    if shown.returncode != 0:
        raise CiError("terraform show -json failed")
    digest, changes = plan_digest(json.loads(shown.stdout))
    text = terraform(env, "show", "-no-color", str(plan_file), capture=True)
    if text.returncode != 0:
        raise CiError("terraform show failed")
    tfvars = tfvars_path(env)
    check_redaction(text.stdout, tfvars.read_text() if tfvars.exists() else "")
    return plan_file, digest, changes, text.stdout


def plan(env: str) -> None:
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP")) as tmp:
        _, digest, changes, text = make_plan(env, Path(tmp))
    set_output("digest", digest)
    set_output("changes", str(changes))
    if len(text) > MAX_PLAN_CHARS:
        text = text[:MAX_PLAN_CHARS] + "\n... (truncated; the full plan is in the job log)\n"
    set_output("text_b64", base64.b64encode(text.encode()).decode())
    summary(f"### Terraform plan: {env}\n\n{changes} resource change(s).\n\n```\n{text}\n```")


def apply(env: str, expect_digest: str | None) -> None:
    # Given but empty or malformed (a skipped plan step, a typo): refuse rather than apply an
    # unreviewed plan.
    if expect_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", expect_digest):
        raise CiError("--expect-digest must be the 64-hex-digit digest of the reviewed plan")
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP")) as tmp:
        plan_file, digest, changes, _ = make_plan(env, Path(tmp))
        # prod: the reviewer approved the plan shown by the prod-plan job. If what would change
        # is different now (someone applied by hand, a resource drifted), stop and plan again.
        if expect_digest is not None and digest != expect_digest:
            raise CiError(
                "the plan differs from the one reviewed (digest "
                f"{digest[:12]} != {expect_digest[:12]}); nothing was applied. Re-run the workflow."
            )
        print(f"applying {changes} change(s)")
        if terraform(env, "apply", "-input=false", "-no-color", str(plan_file)).returncode != 0:
            raise CiError("terraform apply failed")


def destroy(env: str, final: bool) -> None:
    if env != "dev":
        # prod is never destroyed by CI (protect_from_destroy; the brief).
        raise CiError("CI destroys dev only")
    rc = terraform(
        env, "destroy", "-auto-approve", "-input=false", "-no-color", "-lock-timeout=10m"
    ).returncode
    result = terraform(env, "state", "list", capture=True)
    if result.returncode != 0:
        raise CiError("terraform state list failed after the destroy")
    left = result.stdout.split()
    unexpected = [a for a in left if a not in ALLOWED_DESTROY_LEFTOVERS]
    if not left:
        summary(f"**{env}: destroyed; nothing left in the state.**")
        print("destroy: nothing left in the state")
        return
    if unexpected:
        raise CiError(f"destroy left resources that should be gone: {', '.join(unexpected)}")
    if final:
        raise CiError(
            f"the serverless subnet and VPC are still there on the retry run ({', '.join(left)}); "
            "check them in the console"
        )
    summary(
        f"**{env}: destroyed, except the serverless subnet and VPC** (free). Google asks to wait "
        "1-2 hours after the Cloud Run services are gone; the retry run deletes them."
    )
    print(f"destroy exit code {rc}; only the expected network leftovers remain: {', '.join(left)}")


def deploy_page(env: str) -> None:
    firebase_json = terraform(env, "output", "-raw", "firebase_json", capture=True)
    project = terraform(env, "output", "-raw", "project_id", capture=True)
    if firebase_json.returncode != 0 or project.returncode != 0 or not project.stdout:
        raise CiError("could not read firebase_json and project_id from the Terraform outputs")
    web = REPO / "web"
    # firebase.json is generated, git-ignored (web/.gitignore) and holds no secret.
    (web / "firebase.json").write_text(firebase_json.stdout)
    if run(["make", "web-build"], REPO).returncode != 0:
        raise CiError("make web-build failed")
    # The Firebase CLI authenticates with Application Default Credentials: the external-account
    # credential file the auth step wrote (GOOGLE_APPLICATION_CREDENTIALS), which exchanges the
    # job's GitHub OIDC token for a deployer access token. Install scripts are switched off: the
    # CLI needs none, and a package's install script is a common supply-chain attack path. (npm
    # reads NPM_CONFIG_* environment variables case-insensitively.)
    deploy = run(
        [
            "npm", "exec", "--yes", f"--package=firebase-tools@{FIREBASE_TOOLS_VERSION}", "--",
            "firebase", "deploy", "--only", "hosting", "--project", project.stdout.strip(),
            "--non-interactive",
        ],
        web,
        extra_env={"NPM_CONFIG_IGNORE_SCRIPTS": "true", "NPM_CONFIG_UPDATE_NOTIFIER": "false"},
    )  # fmt: skip
    if deploy.returncode != 0:
        raise CiError("firebase deploy failed")
    summary(f"**{env}: page deployed** with firebase-tools {FIREBASE_TOOLS_VERSION}.")


# --- entry point -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        choices=["fetch-tfvars", "init", "stack-is-up", "plan", "apply", "destroy", "deploy-page"],
    )
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--expect-digest", help="apply: the digest of the reviewed plan")
    parser.add_argument("--final", action="store_true", help="destroy: no leftovers allowed")
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch-tfvars":
            fetch_tfvars(args.env)
        elif args.command == "init":
            init(args.env)
        elif args.command == "stack-is-up":
            stack_is_up(args.env)
        elif args.command == "plan":
            plan(args.env)
        elif args.command == "apply":
            apply(args.env, args.expect_digest)
        elif args.command == "destroy":
            destroy(args.env, args.final)
        elif args.command == "deploy-page":
            deploy_page(args.env)
    except CiError as e:
        print(f"::error::{e}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
