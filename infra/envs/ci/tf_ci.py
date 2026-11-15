#!/usr/bin/env python3
"""The Terraform steps of PulseBridge's CI/CD workflows (T26), in one place.

The workflows in .github/workflows/terraform-*.yml authenticate (Workload Identity Federation,
google-github-actions/auth) and then call this script, so the logic reads top to bottom here, runs
the same on a laptop, and is tested (tests/tooling/test_tf_ci.py runs it against fake tools).

Commands (most take --env dev|prod and work in infra/envs/<env>):

  fetch-tfvars        download the environment's tfvars from its state bucket (optionally one
                      exact GCS generation) and record the generation it got
  cleanup             delete the tfvars and the recorded generation (run at the end of a job)
  init                terraform init against the GCS backend
  stack-is-up         whether a person has done the first full apply (runbook step 8)
  plan                plan, check the plan for sensitive values, and write the REDACTED plan text
                      and its metadata (digest, change count, tfvars generation, commit) to a
                      directory the workflow uploads
  apply               plan again and apply; with --reviewed, only if the tfvars generation, the
                      commit and the digest are those of the reviewed plan
  destroy             dev only: destroy, then accept only the documented network leftovers
  export-page-config  write the page deploy's inputs (firebase.json, project) for the deploy job
  deploy-page         install the pinned Firebase CLI (deploy-tools/) and deploy web/dist
  require-tip         refuse unless this run's commit is still the tip of main
  check-environments  refuse unless prod's two GitHub Environments are protected as required

Environment: TF_STATE_BUCKET (bootstrap's bucket, from a repository variable), GCP_ACCESS_TOKEN
(fetch-tfvars only: a short-lived token from the auth step), GH_TOKEN, GITHUB_REPOSITORY and
GITHUB_SHA (require-tip, check-environments), RUNNER_TEMP, GITHUB_STEP_SUMMARY and GITHUB_OUTPUT
(set by GitHub Actions; optional elsewhere).

Nothing here prints a credential or a sensitive tfvars value. The sensitive values are
registered with the runner's log masking (`::add-mask::`) as soon as the tfvars are read, and a
plan that shows any of them fails before its text is written anywhere.
"""

from __future__ import annotations

import argparse
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
DEPLOY_TOOLS = Path(__file__).resolve().parent / "deploy-tools"
ENVIRONMENTS = ("dev", "prod")

# Where each environment's tfvars live: an object in that environment's own state bucket, next
# to the state, readable by the same identities. The tfvars name the project, the patient and the
# alert address, so they are kept out of GitHub (infra/envs/README.md, "CI/CD").
TFVARS_OBJECT = "ci/{env}.tfvars"

# Root variables that are `sensitive` (check_env_roots.py checks the declarations).
SENSITIVE_VARIABLES = ("patient_id", "patient_family_name", "patient_given_name", "alert_email")

# Created only by the first FULL apply (runbook step 8): its presence in the state means a person
# finished the bring-up (steps 3 to 7), so CI may take over. Before that, and after the nightly
# destroy, CI plans and applies nothing.
STACK_UP_MARKER = "module.stack.module.phr.google_cloud_run_v2_service.fhir_proxy"

# What a dev destroy may leave: after the Cloud Run services that used Direct VPC egress are
# deleted, Google asks to "wait 1-2 hours" before their subnet can be deleted, and the VPC cannot
# go before its subnet. Both are free; the retry run deletes them.
ALLOWED_DESTROY_LEFTOVERS = (
    "module.stack.module.project_base.google_compute_subnetwork.serverless",
    "module.stack.module.project_base.google_compute_network.this",
)

# prod's two GitHub Environments and the one branch allowed to deploy to them.
PROD_ENVIRONMENTS = ("prod-plan", "prod")
MAIN_BRANCH = "main"

TFVARS_STRING = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')
TFVARS_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


class CiError(Exception):
    """A failure with a message that is safe to print (it never carries a sensitive value)."""


# --- small helpers -----------------------------------------------------------------------------


def root_dir(env: str) -> Path:
    if env not in ENVIRONMENTS:
        raise CiError(f"unknown environment {env!r}; expected one of {ENVIRONMENTS}")
    return ENVS / env


def state_dir() -> Path:
    """Per-job scratch outside the checkout (the runner's temp directory)."""
    path = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "tf-ci"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


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
    """A step output (GITHUB_OUTPUT). Used only for short flags: a value that contains a masked
    string is dropped by the runner, so anything that matters travels in files instead."""
    if "\n" in value:
        raise CiError(f"output {name} must be a single line")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"output {name}: {value}")


def summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def http_get(url: str, headers: dict[str, str], what: str) -> tuple[bytes, dict[str, str]]:
    """GET over HTTPS; every failure becomes a CiError that names `what`, never a traceback (and
    never the request's headers, which hold a token)."""
    if not url.startswith("https://"):
        raise CiError(f"{what}: refusing a non-HTTPS URL")
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https only, checked above
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.read(), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as e:
        raise CiError(f"{what}: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CiError(f"{what}: network error ({type(e).__name__})") from None


def github_api(path: str) -> dict:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise CiError("GH_TOKEN must be set")
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    body, _ = http_get(
        f"{base}{path}",
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        f"GitHub API {path}",
    )
    try:
        data = json.loads(body)
    except ValueError:
        raise CiError(f"GitHub API {path}: the answer is not JSON") from None
    if not isinstance(data, dict):
        raise CiError(f"GitHub API {path}: unexpected answer")
    return data


def repository() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo):
        raise CiError("GITHUB_REPOSITORY must be owner/name")
    return repo


# --- tfvars ------------------------------------------------------------------------------------


def tfvars_path(env: str) -> Path:
    return root_dir(env) / "terraform.tfvars"


def generation_path(env: str) -> Path:
    return state_dir() / f"{env}.generation"


def sensitive_values(tfvars_text: str) -> dict[str, str]:
    """The values of the sensitive variables. Each must be a plain quoted string on one line:
    a heredoc or an expression cannot be checked for or masked, so it is refused."""
    found: dict[str, str] = {}
    for line in tfvars_text.splitlines():
        assignment = TFVARS_ASSIGNMENT.match(line)
        if not assignment or assignment.group(1) not in SENSITIVE_VARIABLES:
            continue
        m = TFVARS_STRING.match(line)
        if not m:
            raise CiError(
                f"{assignment.group(1)} in the tfvars must be a plain quoted string on one line "
                "(no heredoc or expression), so CI can mask it and check plans for it"
            )
        if m.group(2):
            # HCL string escapes: only \" and \\ are expected in these values.
            found[m.group(1)] = re.sub(r"\\(.)", r"\1", m.group(2))
    return found


def mask(tfvars_text: str) -> None:
    """Ask the runner to mask the sensitive values in every later log line of this job."""
    values = sensitive_values(tfvars_text)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for value in values.values():
        print(f"::add-mask::{value}")


def fetch_tfvars(env: str, generation: str | None) -> None:
    bucket = os.environ.get("TF_STATE_BUCKET", "")
    token = os.environ.get("GCP_ACCESS_TOKEN", "")
    if not bucket or not token:
        raise CiError("TF_STATE_BUCKET and GCP_ACCESS_TOKEN must be set")
    if generation is not None and not re.fullmatch(r"[1-9][0-9]{0,24}", generation):
        raise CiError("--generation must be the reviewed object's GCS generation (digits)")
    obj = TFVARS_OBJECT.format(env=env)
    url = (
        f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket, safe='')}"
        f"/o/{urllib.parse.quote(obj, safe='')}?alt=media"
    )
    if generation is not None:
        url += f"&generation={generation}"
    body, headers = http_get(
        url,
        {"Authorization": f"Bearer {token}"},
        f'reading gs://{bucket}/{obj} (infra/envs/README.md, "CI/CD", says how to upload it)',
    )
    got = headers.get("x-goog-generation", "")
    if not re.fullmatch(r"[1-9][0-9]{0,24}", got):
        raise CiError(f"gs://{bucket}/{obj}: the answer carries no object generation")
    if generation is not None and got != generation:
        raise CiError(f"gs://{bucket}/{obj}: asked for generation {generation}, got {got}")
    text = body.decode("utf-8")
    mask(text)
    path = tfvars_path(env)
    # 0600 even when the file already exists (O_CREAT's mode applies only to a new file).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    generation_path(env).write_text(got)
    print(f"wrote {path.relative_to(REPO)} ({len(text.splitlines())} lines), generation {got}")


def recorded_generation(env: str) -> str:
    path = generation_path(env)
    value = path.read_text().strip() if path.exists() else ""
    if not value:
        raise CiError("no tfvars generation recorded: run fetch-tfvars first")
    return value


def cleanup(env: str) -> None:
    for path in (tfvars_path(env), generation_path(env)):
        try:
            path.unlink()
            print(f"deleted {path.name}")
        except FileNotFoundError:
            pass


# --- terraform -----------------------------------------------------------------------------------


def init(env: str) -> None:
    bucket = os.environ.get("TF_STATE_BUCKET", "")
    if not bucket:
        raise CiError("TF_STATE_BUCKET must be set")
    # The prefix (dev or prod) is fixed in the root's backend.tf; only the bucket is given here.
    result = terraform(env, "init", "-input=false", "-no-color", f"-backend-config=bucket={bucket}")
    if result.returncode != 0:
        raise CiError("terraform init failed")


def state_list(env: str) -> list[str]:
    result = terraform(env, "state", "list", capture=True)
    if result.returncode != 0:
        # An unreadable state is an error, never "not deployed".
        raise CiError("terraform state list failed")
    return result.stdout.split()


def stack_is_up(env: str) -> bool:
    up = STACK_UP_MARKER in state_list(env)
    set_output("up", "true" if up else "false")
    if not up:
        message = (
            f"{env} is not deployed: nothing was planned or applied. CI takes over only after a "
            "person has done the first full apply (infra/envs/README.md, runbook steps 3 to 8)."
        )
        summary(f"## :warning: {message}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning::{message}")
    return up


def _redact(value: object, mask: object) -> object:
    """Replace every value Terraform marks sensitive (before_sensitive/after_sensitive)."""
    if mask is True:
        return "(sensitive)"
    if isinstance(value, dict):
        m = mask if isinstance(mask, dict) else {}
        return {k: _redact(v, m.get(k, False)) for k, v in value.items()}
    if isinstance(value, list):
        m = mask if isinstance(mask, list) else []
        return [_redact(v, m[i] if i < len(m) else False) for i, v in enumerate(value)]
    return value


def plan_digest(plan_json: dict) -> tuple[str, int]:
    """A digest of what the plan does: for every resource that is not a no-op, its address, its
    actions and its before and after values, and the output changes, with every SENSITIVE value
    replaced by a placeholder (so the digest cannot be used to guess a patient ID or a name).
    Two plans with the same digest make the same changes with the same non-sensitive values; a
    change to a sensitive value alone comes from the tfvars, which apply pins by generation."""
    changes = []
    for rc in plan_json.get("resource_changes", []):
        change = rc["change"]
        if change["actions"] == ["no-op"]:
            continue
        changes.append(
            {
                "address": rc["address"],
                "actions": change["actions"],
                "before": _redact(change.get("before"), change.get("before_sensitive", False)),
                "after": _redact(change.get("after"), change.get("after_sensitive", False)),
                "after_unknown": change.get("after_unknown"),
            }
        )
    outputs = {
        name: {
            "actions": c["actions"],
            "before": _redact(c.get("before"), c.get("before_sensitive", False)),
            "after": _redact(c.get("after"), c.get("after_sensitive", False)),
        }
        for name, c in (plan_json.get("output_changes") or {}).items()
        if c["actions"] != ["no-op"]
    }
    changes.sort(key=lambda c: c["address"])
    canonical = json.dumps({"resources": changes, "outputs": outputs}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest(), len(changes)


def check_redaction(plan_text: str, tfvars_text: str) -> None:
    """A BACKSTOP, not the protection (Terraform's `sensitive` is): fail if the human-readable
    plan shows a sensitive tfvars value verbatim, as a whole word. It does not see the value
    changed in case, encoded (base64, URL encoding), or inside a longer identifier."""
    leaked = [
        name
        for name, value in sensitive_values(tfvars_text).items()
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", plan_text)
    ]
    if leaked:
        raise CiError(
            "the plan shows the value of sensitive variable(s) "
            f"{', '.join(sorted(leaked))}; it is not recorded or posted. Find where the value "
            "reaches a non-sensitive attribute."
        )


def make_plan(env: str, workdir: Path, *, lock: bool = True) -> tuple[Path, str, int, str]:
    """Plan into workdir (never into the repository: a binary plan holds sensitive values in
    clear). Returns the plan file, its digest, the number of changes and the redacted text.
    `terraform plan` also prints the plan to the job log, where the masked values are hidden."""
    plan_file = workdir / "tfplan"
    args = ["plan", "-input=false", "-no-color", f"-out={plan_file}"]
    # The read-only planner cannot take the lock (that is an object write); a plan made while an
    # apply runs may then be stale, and nothing is applied from it.
    args += ["-lock-timeout=10m"] if lock else ["-lock=false"]
    if terraform(env, *args).returncode != 0:
        raise CiError("terraform plan failed")
    shown = terraform(env, "show", "-json", str(plan_file), capture=True)
    if shown.returncode != 0:
        raise CiError("terraform show -json failed")
    try:
        digest, changes = plan_digest(json.loads(shown.stdout))
    except (ValueError, KeyError, TypeError):
        raise CiError("terraform show -json gave an unexpected plan") from None
    text = terraform(env, "show", "-no-color", str(plan_file), capture=True)
    if text.returncode != 0:
        raise CiError("terraform show failed")
    tfvars = tfvars_path(env)
    check_redaction(text.stdout, tfvars.read_text() if tfvars.exists() else "")
    if changes and not text.stdout.strip():
        raise CiError("the plan has changes but its text is empty")
    return plan_file, digest, changes, text.stdout


def plan(env: str, record: Path, lock: bool) -> None:
    generation = recorded_generation(env)
    with tempfile.TemporaryDirectory(dir=state_dir()) as tmp:
        _, digest, changes, text = make_plan(env, Path(tmp), lock=lock)
    # Files, not step outputs: the runner drops an output that contains a masked value, and the
    # redacted text could (a short masked name inside an ordinary word).
    record.mkdir(parents=True, exist_ok=True)
    (record / "plan.txt").write_text(text)
    meta = {
        "env": env,
        "digest": digest,
        "changes": changes,
        "tfvars_generation": generation,
        "commit": os.environ.get("GITHUB_SHA", ""),
    }
    (record / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    summary(f"### Terraform plan: {env}\n\n{changes} resource change(s).\n\n````text\n{text}\n````")
    print(f"recorded the plan of {env}: {changes} change(s), digest {digest[:12]}")


def read_reviewed(path: Path, env: str) -> dict:
    try:
        meta = json.loads((path / "meta.json").read_text())
    except (OSError, ValueError):
        raise CiError("the reviewed plan's meta.json is missing or not JSON") from None
    if meta.get("env") != env:
        raise CiError("the reviewed plan is of another environment")
    if not re.fullmatch(r"[0-9a-f]{64}", str(meta.get("digest", ""))):
        raise CiError("the reviewed plan has no valid digest")
    return meta


def apply(env: str, reviewed: Path | None) -> None:
    meta = read_reviewed(reviewed, env) if reviewed is not None else None
    if meta is not None:
        # The reviewer approved a plan made from one commit and one tfvars generation. Anything
        # else (a newer commit, an edited tfvars) is a different change that has not been reviewed.
        if meta["tfvars_generation"] != recorded_generation(env):
            raise CiError("the tfvars are not the generation the reviewed plan used")
        if meta.get("commit") != os.environ.get("GITHUB_SHA", ""):
            raise CiError("this run's commit is not the reviewed plan's commit")
    with tempfile.TemporaryDirectory(dir=state_dir()) as tmp:
        plan_file, digest, changes, _ = make_plan(env, Path(tmp))
        if meta is not None and digest != meta["digest"]:
            raise CiError(
                "the plan differs from the one reviewed (digest "
                f"{digest[:12]} != {meta['digest'][:12]}); nothing was applied. Re-run the "
                "workflow to plan and review again."
            )
        print(f"applying {changes} change(s)")
        if terraform(env, "apply", "-input=false", "-no-color", str(plan_file)).returncode != 0:
            raise CiError("terraform apply failed")


def destroy(env: str, final: bool) -> None:
    if env != "dev":
        raise CiError("CI destroys dev only")
    before = state_list(env)
    if not before:
        summary(f"**{env}: nothing to destroy; it is not deployed.**")
        print("destroy: the state is empty; nothing to do")
        return
    leftovers_only = all(a in ALLOWED_DESTROY_LEFTOVERS for a in before)
    if STACK_UP_MARKER not in before and not leftovers_only:
        # A partial stack: a person is in the middle of the bring-up (runbook steps 3 to 7).
        message = (
            f"{env} holds {len(before)} resource(s) but is not fully deployed (a bring-up in "
            "progress?): not destroyed. Set DEV_NIGHTLY_DESTROY to false during a bring-up."
        )
        summary(f"## :warning: {message}")
        print(f"::warning::{message}" if os.environ.get("GITHUB_ACTIONS") == "true" else message)
        return
    print(f"destroying {len(before)} resource(s)")
    rc = terraform(
        env, "destroy", "-auto-approve", "-input=false", "-no-color", "-lock-timeout=10m"
    ).returncode
    left = state_list(env)
    unexpected = [a for a in left if a not in ALLOWED_DESTROY_LEFTOVERS]
    if unexpected:
        raise CiError(f"destroy left resources that should be gone: {', '.join(unexpected)}")
    if left and final:
        raise CiError(
            f"the serverless subnet and VPC are still there on the retry run ({', '.join(left)}); "
            "check them in the console"
        )
    if left:
        summary(
            f"**{env}: destroyed {len(before) - len(left)} resource(s); the serverless subnet and "
            "the VPC (free) wait for the retry run** (Google: wait 1-2 hours)."
        )
        print(f"destroy exit code {rc}; only the expected network leftovers remain")
        return
    if rc != 0:
        raise CiError("terraform destroy failed although nothing is left in the state")
    summary(f"**{env}: destroyed {len(before)} resource(s); nothing is left in the state.**")


# --- the page ------------------------------------------------------------------------------------


def export_page_config(env: str, out: Path) -> None:
    """The deploy job's inputs. firebase.json holds no secret (the site, the rewrite, the
    headers); the project ID is an identifier. The deploy job's identity cannot read the state,
    so the apply job hands these over."""
    firebase_json = terraform(env, "output", "-json", "firebase_json", capture=True)
    project = terraform(env, "output", "-json", "project_id", capture=True)
    try:
        config = {
            "project_id": json.loads(project.stdout),
            "firebase_json": json.loads(firebase_json.stdout),
        }
        json.loads(config["firebase_json"])
    except (ValueError, TypeError):
        raise CiError(
            "could not read firebase_json and project_id from the Terraform outputs"
        ) from None
    if (
        firebase_json.returncode
        or project.returncode
        or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", str(config["project_id"]))
    ):
        raise CiError("could not read firebase_json and project_id from the Terraform outputs")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, indent=2) + "\n")
    print(f"wrote the page deploy's configuration for {env}")


def deploy_page(config_path: Path) -> None:
    try:
        config = json.loads(config_path.read_text())
        project = config["project_id"]
        firebase_json = config["firebase_json"]
        json.loads(firebase_json)
    except (OSError, ValueError, KeyError, TypeError):
        raise CiError("the page configuration is missing or malformed") from None
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", str(project)):
        raise CiError("the page configuration names no valid project")
    web = REPO / "web"
    if not (web / "dist" / "index.html").is_file():
        raise CiError("web/dist/index.html is missing: download the built page first")
    # firebase.json is generated, git-ignored (web/.gitignore) and holds no secret.
    (web / "firebase.json").write_text(firebase_json)
    # The Firebase CLI's whole dependency tree is pinned by deploy-tools/package-lock.json
    # (versions and integrity hashes); npm ci installs exactly that, with install scripts off.
    no_scripts = {"NPM_CONFIG_IGNORE_SCRIPTS": "true", "NPM_CONFIG_UPDATE_NOTIFIER": "false"}
    install = run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        DEPLOY_TOOLS,
        extra_env=no_scripts,
    )
    if install.returncode != 0:
        raise CiError("npm ci of the deploy tools failed")
    # The CLI authenticates with Application Default Credentials: the external-account file the
    # auth step wrote for the page deployer (roles/firebasehosting.admin only). --no-install:
    # never fetch anything that is not in the lockfile.
    deploy = run(
        [
            "npx", "--no-install", "firebase", "deploy", "--only", "hosting",
            "--project", str(project), "--config", str(web / "firebase.json"), "--non-interactive",
        ],
        DEPLOY_TOOLS,
        extra_env=no_scripts,
    )  # fmt: skip
    if deploy.returncode != 0:
        raise CiError("firebase deploy failed")
    version = json.loads((DEPLOY_TOOLS / "package.json").read_text())["dependencies"][
        "firebase-tools"
    ]
    summary(f"**Page deployed** with firebase-tools {version} (locked).")


# --- GitHub checks -------------------------------------------------------------------------------


def require_tip(branch: str) -> None:
    sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CiError("GITHUB_SHA must be a full commit SHA")
    ref = github_api(f"/repos/{repository()}/git/ref/heads/{branch}")
    tip = (ref.get("object") or {}).get("sha")
    if tip != sha:
        raise CiError(
            f"this run's commit {sha[:12]} is no longer the tip of {branch}; a newer run applies "
            "the newer commit. Nothing was applied."
        )
    print(f"{sha[:12]} is the tip of {branch}")


def check_environments() -> None:
    """prod's deployer and planner trust these two Environments; GitHub creates a missing one
    with no rules the first time a job names it, and "protected branches only" lets every branch
    deploy when no branch is protected. So each must allow exactly the main branch through a
    custom policy, with no admin bypass, and prod must have required reviewers. Anything else,
    including an API error or an answer of an unexpected shape, fails."""
    repo = repository()
    problems: list[str] = []
    for name in PROD_ENVIRONMENTS:
        try:
            env = github_api(f"/repos/{repo}/environments/{name}")
            policy = env.get("deployment_branch_policy")
            if policy != {"protected_branches": False, "custom_branch_policies": True}:
                problems.append(
                    f"{name}: deployment branches must be 'Selected branches and tags' "
                    "(custom_branch_policies true, protected_branches false)"
                )
            if env.get("can_admins_bypass") is not False:
                problems.append(f"{name}: 'Allow administrators to bypass' must be off")
            listing = github_api(f"/repos/{repo}/environments/{name}/deployment-branch-policies")
            branches = [(p.get("name"), p.get("type")) for p in listing.get("branch_policies", [])]
            if branches != [(MAIN_BRANCH, "branch")] or listing.get("total_count") != 1:
                problems.append(
                    f"{name}: the only deployment branch policy must be '{MAIN_BRANCH}'"
                )
            if name == "prod":
                reviewers = [
                    r
                    for rule in env.get("protection_rules") or []
                    if isinstance(rule, dict) and rule.get("type") == "required_reviewers"
                    for r in rule.get("reviewers") or []
                ]
                if not reviewers:
                    problems.append("prod: required reviewers are missing (the manual gate)")
        except CiError as e:
            problems.append(str(e))
        except (AttributeError, TypeError):
            problems.append(f"{name}: unexpected answer from the GitHub API")
    if problems:
        raise CiError(
            "prod's GitHub Environments are not protected as required: " + "; ".join(problems)
        )
    print("prod-plan and prod: main only, no admin bypass; prod has required reviewers")


# --- entry point ---------------------------------------------------------------------------------

COMMANDS = (
    "fetch-tfvars", "cleanup", "init", "stack-is-up", "plan", "apply", "destroy",
    "export-page-config", "deploy-page", "require-tip", "check-environments",
)  # fmt: skip
NEEDS_ENV = {
    "fetch-tfvars",
    "cleanup",
    "init",
    "stack-is-up",
    "plan",
    "apply",
    "destroy",
    "export-page-config",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--env", choices=ENVIRONMENTS)
    parser.add_argument("--generation", help="fetch-tfvars: this exact GCS generation")
    parser.add_argument("--record", type=Path, help="plan: directory for plan.txt and meta.json")
    parser.add_argument("--no-lock", action="store_true", help="plan: -lock=false (the planner)")
    parser.add_argument("--reviewed", type=Path, help="apply: the reviewed plan's directory")
    parser.add_argument("--final", action="store_true", help="destroy: no leftovers allowed")
    parser.add_argument("--out", type=Path, help="export-page-config: the file to write")
    parser.add_argument("--config", type=Path, help="deploy-page: the exported configuration")
    parser.add_argument("--branch", default=MAIN_BRANCH, help="require-tip: the branch")
    args = parser.parse_args(argv)
    try:
        if args.command in NEEDS_ENV and not args.env:
            raise CiError(f"{args.command} needs --env")
        if args.command == "fetch-tfvars":
            fetch_tfvars(args.env, args.generation)
        elif args.command == "cleanup":
            cleanup(args.env)
        elif args.command == "init":
            init(args.env)
        elif args.command == "stack-is-up":
            stack_is_up(args.env)
        elif args.command == "plan":
            if args.record is None:
                raise CiError("plan needs --record")
            plan(args.env, args.record, lock=not args.no_lock)
        elif args.command == "apply":
            apply(args.env, args.reviewed)
        elif args.command == "destroy":
            destroy(args.env, args.final)
        elif args.command == "export-page-config":
            if args.out is None:
                raise CiError("export-page-config needs --out")
            export_page_config(args.env, args.out)
        elif args.command == "deploy-page":
            if args.config is None:
                raise CiError("deploy-page needs --config")
            deploy_page(args.config)
        elif args.command == "require-tip":
            require_tip(args.branch)
        elif args.command == "check-environments":
            check_environments()
    except CiError as e:
        print(f"::error::{e}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
