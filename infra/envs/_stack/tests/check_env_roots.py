#!/usr/bin/env python3
"""Static and cross-root checks for infra/envs (task T25). Exit code 0 = pass.

What `terraform test` cannot check, because it looks at one configuration at a time:

1. dev and prod differ ONLY where documented: every file is byte-identical except env.tf (the
   per-environment settings, compared with the table below and in infra/envs/README.md),
   backend.tf (the state prefix), terraform.tfvars.example and tests/root.tftest.hcl.
2. Nothing author-specific is committed under infra/envs: no project ID, project number, bucket,
   email address or domain other than placeholders and Google's own hosts.
3. terraform.tfvars and backend.hcl are git-ignored; the *.example files and the roots' lock
   files are not; the examples hold placeholders only, name only declared variables, set every
   variable that has no default, and PLAN (a credential-free `terraform test` with the example as
   its -var-file).
4. The roots' lock files exist, pin google and google-beta at a version the modules' "~> 8.11"
   allows, and cover the same platforms as bootstrap's (identical hashes).
5. The providers: project set explicitly, google-beta with user_project_override and
   billing_project, no credentials argument; no public principal or key resource in any .tf.

Run from anywhere. Terraform runs with every Google credential source removed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ENVS = Path(__file__).resolve().parent.parent.parent  # infra/envs
REPO = ENVS.parent.parent
ROOTS = ("dev", "prod")

# The documented per-environment settings (infra/envs/README.md, "dev and prod"). Anything else
# that differs between the two roots is a failure.
EXPECTED_ENV = {
    "dev": {"environment": "dev", "protect_from_destroy": "false",
            "allow_reviewer_password_from_tfvars": "true", "use_synthetic_data_default": "true"},
    "prod": {"environment": "prod", "protect_from_destroy": "true",
             "allow_reviewer_password_from_tfvars": "false", "use_synthetic_data_default": "false"},
}
MAY_DIFFER = {"env.tf", "backend.tf", "terraform.tfvars.example", "tests/root.tftest.hcl"}

# Host names that may appear: placeholders and Google's (or the tools') own hosts.
ALLOWED_DOMAIN_SUFFIXES = (
    "example.com", "example.org", "googleapis.com", "google.com", "run.app", "web.app",
    "firebaseapp.com", "pkg.dev", "gcr.io", "gserviceaccount.com", "github.com",
    "githubusercontent.com", "terraform.io", "hashicorp.com", "terraform-docs.io",
)
ALLOWED_PROJECTS = {"example-project", "cos-cloud", "your-project-id", "your-dev-project-id", "your-prod-project-id"}
ALLOWED_NUMBERS = {"100000000001", "123456789012", "000000000000"}
PLACEHOLDER_STRINGS = {
    "SYN-0001", "PAT-0001", "PULSEBRIDGE", "America/New_York", "Family", "Given", "you@example.com",
    "your-dev-project-id", "your-prod-project-id", "your-project-id-tfstate",
}

CREDENTIAL_VARS = (
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CREDENTIALS", "GOOGLE_CLOUD_KEYFILE_JSON",
    "GCLOUD_KEYFILE_JSON", "GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_PROJECT", "GCP_SA_KEY_B64", "CLOUDSDK_CORE_PROJECT",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
)

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)


def candidate_files() -> list[Path]:
    """Files under infra/envs that git would commit: tracked, or untracked and not ignored."""
    result = git("ls-files", "-co", "--exclude-standard", "--", "infra/envs")
    if result.returncode != 0:
        raise SystemExit(f"FAIL: git ls-files failed: {result.stderr.strip()}")
    return [REPO / line for line in result.stdout.splitlines() if line and (REPO / line).is_file()]


def root_files(root: str) -> dict[str, Path]:
    base = ENVS / root
    return {str(p.relative_to(base)): p for p in candidate_files() if base in p.parents}


# ---- 1. dev and prod differ only where documented ---------------------------------------------

def env_settings(text: str) -> dict[str, str]:
    settings = {}
    locals_block = re.search(r"^locals\s*\{(.*?)^\}", text, re.S | re.M)
    body = re.sub(r"#.*", "", locals_block.group(1)) if locals_block else ""
    for key, value in re.findall(r"^\s*(\w+)\s*=\s*(\S+)\s*$", body, re.M):
        settings[key] = value.strip('"')
    variables = re.findall(r'^variable\s+"(\w+)"', text, re.M)
    if variables != ["use_synthetic_data"]:
        fail(f"env.tf may declare only the use_synthetic_data variable, found {variables}")
    default = re.search(r'^variable\s+"use_synthetic_data"\s*\{.*?^\s*default\s*=\s*(\w+)', text, re.S | re.M)
    settings["use_synthetic_data_default"] = default.group(1) if default else "?"
    return settings


def check_roots_differ_only_where_documented() -> None:
    files = {root: root_files(root) for root in ROOTS}
    if set(files["dev"]) != set(files["prod"]):
        fail(f"dev and prod must have the same files; only in one: {sorted(set(files['dev']) ^ set(files['prod']))}")
    for name in sorted(set(files["dev"]) & set(files["prod"])):
        same = files["dev"][name].read_bytes() == files["prod"][name].read_bytes()
        if name not in MAY_DIFFER and not same:
            fail(f"{name} differs between dev and prod; only {sorted(MAY_DIFFER)} may")
    for root in ROOTS:
        got = env_settings((ENVS / root / "env.tf").read_text())
        if got != EXPECTED_ENV[root]:
            fail(f"{root}/env.tf settings {got} != documented {EXPECTED_ENV[root]}")
    backends = {root: (ENVS / root / "backend.tf").read_text() for root in ROOTS}
    neutral = {root: re.sub(r"\b(dev|prod)\b", "<env>", text) for root, text in backends.items()}
    if neutral["dev"] != neutral["prod"]:
        fail("dev/backend.tf and prod/backend.tf may differ only in the environment name")
    for root, text in backends.items():
        if not re.search(r'backend\s+"gcs"\s*\{\s*prefix\s*=\s*"' + root + r'"\s*\}', text):
            fail(f'{root}/backend.tf must be a partial gcs backend with only prefix = "{root}" (no bucket)')
    readme = (ENVS / "README.md").read_text()
    for setting in ("protect_from_destroy", "allow_reviewer_password_from_tfvars", "use_synthetic_data"):
        if setting not in readme:
            fail(f"infra/envs/README.md must document the dev/prod setting {setting}")


# ---- 2. nothing author-specific --------------------------------------------------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+${}<>-]+@[A-Za-z0-9.${}<>-]+\.[a-z]{2,}")
HOST = re.compile(r"(?<![\w.@/-])(?:[a-z0-9][a-z0-9-]*\.)+(?:com|org|net|io|app|dev|cloud|me|ai)\b")
PROJECT_PATH = re.compile(r"projects/([a-z][a-z0-9-]{4,28}[a-z0-9])\b")
NUMBER = re.compile(r"(?<![\w.:-])\d{12}(?![\w.-])")


def check_no_hardcoded_identity() -> None:
    for path in candidate_files():
        if path.suffix in (".png", ".jpg"):
            continue
        rel = path.relative_to(REPO)
        text = path.read_text(errors="replace")
        for m in EMAIL.finditer(text):
            addr = m.group(0)
            domain = addr.split("@", 1)[1]
            placeholder = any(c in addr for c in "<${") or domain in ("example.com", "example.org")
            sa = domain.endswith(".iam.gserviceaccount.com") and domain.split(".")[0] in ALLOWED_PROJECTS
            if not (placeholder or sa):
                fail(f"{rel}: email address {addr!r} is not a placeholder")
        for m in HOST.finditer(text):
            host = m.group(0)
            if not any(host == s or host.endswith("." + s) for s in ALLOWED_DOMAIN_SUFFIXES):
                fail(f"{rel}: host {host!r} is not a placeholder or a Google/tool host")
        for m in PROJECT_PATH.finditer(text):
            if m.group(1) not in ALLOWED_PROJECTS:
                fail(f"{rel}: project {m.group(1)!r} in a resource path is not a placeholder")
        for m in NUMBER.finditer(text):
            if m.group(0) not in ALLOWED_NUMBERS:
                fail(f"{rel}: 12-digit number {m.group(0)} looks like a project number")
        for m in re.finditer(r'\bbucket\s*=\s*"([^"]*)"', text):
            if m.group(1) != "your-project-id-tfstate":
                fail(f"{rel}: bucket {m.group(1)!r} is not the placeholder")
        if path.suffix == ".tf":
            code = re.sub(r"#.*", "", text)
            for word in ("allUsers", "allAuthenticatedUsers", "google_service_account_key"):
                if word in code:
                    fail(f"{rel}: {word!r} must not appear in Terraform code under infra/envs")
            # A provider `credentials = ...` argument would point at a key file.
            if re.search(r"^\s*(credentials|access_token)\s*=", code, re.M):
                fail(f"{rel}: a credentials or access_token argument must not be configured")


# ---- 3. git-ignore rules and the examples ----------------------------------------------------

def check_ignored() -> None:
    must_ignore = [f"infra/envs/{r}/{n}" for r in ROOTS for n in ("terraform.tfvars", "backend.hcl", "terraform.tfstate", "plan.tfplan")]
    must_ignore.append("infra/envs/_stack/.terraform.lock.hcl")
    must_keep = [f"infra/envs/{r}/{n}" for r in ROOTS for n in ("terraform.tfvars.example", "backend.hcl.example", ".terraform.lock.hcl")]
    for path in must_ignore:
        if git("check-ignore", "-q", "--no-index", path).returncode != 0:
            fail(f"{path} must be git-ignored")
    for path in must_keep:
        if git("check-ignore", "-q", "--no-index", path).returncode == 0:
            fail(f"{path} must NOT be git-ignored (it is committed)")
        if not (REPO / path).is_file():
            fail(f"{path} is missing")


def declared_variables(root: str) -> dict[str, bool]:
    """variable name -> has a default, from the root's .tf files."""
    out = {}
    for tf in (ENVS / root).glob("*.tf"):
        for m in re.finditer(r'^variable\s+"(\w+)"\s*\{(.*?)^\}', tf.read_text(), re.S | re.M):
            out[m.group(1)] = bool(re.search(r"^\s*default\s*=", m.group(2), re.M))
    return out


def check_example(root: str) -> None:
    path = ENVS / root / "terraform.tfvars.example"
    declared = declared_variables(root)
    assigned = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^(\w+)\s*=\s*(.+?)\s*(?:#.*)?$", line)
        if m:
            assigned[m.group(1)] = m.group(2)
    for name, value in assigned.items():
        if name not in declared:
            fail(f"{root}/terraform.tfvars.example sets {name}, which is not a variable of the root")
        for s in re.findall(r'"([^"]*)"', value):
            if s not in PLACEHOLDER_STRINGS:
                fail(f"{root}/terraform.tfvars.example: {name} = {s!r} is not a known placeholder")
    required = {n for n, has_default in declared.items() if not has_default}
    missing = required - set(assigned)
    if missing:
        fail(f"{root}/terraform.tfvars.example lacks the required variable(s) {sorted(missing)}")


def tf_env() -> tuple[dict[str, str], str]:
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}
    home = tempfile.mkdtemp(prefix="env-roots-check-home-")
    env.update(HOME=home, CLOUDSDK_CONFIG=os.path.join(home, "gcloud"), GCE_METADATA_HOST="127.0.0.1:9",
               GCE_METADATA_IP="127.0.0.1:9", NO_GCE_CHECK="True", CHECKPOINT_DISABLE="1", TF_IN_AUTOMATION="1")
    return env, home


EXAMPLE_TEST = '''
# Generated by check_env_roots.py: the committed terraform.tfvars.example must plan.
mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "100000000001" }
  }
}
mock_provider "google-beta" {}

# Step 3 of the runbook, with the example exactly as committed (no images yet).
run "example_plans_the_first_targeted_apply" {
  command = plan
  plan_options {
    target = [module.stack.module.project_base]
  }
}

# A full plan once the build's image block is pasted in.
run "example_plans_in_full_with_images" {
  command = plan
  variables {
    v2_to_fhir_image  = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    health_sync_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    seed_image        = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/seed@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    fhir_proxy_image  = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    cuff_sim_image    = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"
  }
}
'''


def check_example_plans(root: str) -> None:
    """Copy the root (without .terraform) into a temp dir and plan it with the example."""
    env, home = tf_env()
    work = Path(tempfile.mkdtemp(prefix=f"env-roots-{root}-"))
    try:
        # The root's ../_stack and ../../modules sources must resolve: copy infra/ minimally.
        shutil.copytree(REPO / "infra" / "modules", work / "infra" / "modules",
                        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl", "tests"))
        shutil.copytree(ENVS / "_stack", work / "infra" / "envs" / "_stack",
                        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl", "tests"))
        dest = work / "infra" / "envs" / root
        shutil.copytree(ENVS / root, dest, ignore=shutil.ignore_patterns(".terraform", "tests", "terraform.tfvars", "backend.hcl"))
        (dest / "example_tests").mkdir()
        (dest / "example_tests" / "example.tftest.hcl").write_text(EXAMPLE_TEST)
        cache = ENVS / root / ".terraform" / "providers"
        init = ["terraform", f"-chdir={dest}", "init", "-backend=false", "-input=false", "-no-color", "-lockfile=readonly"]
        if cache.is_dir():
            # Reuse the providers `make tf-test` already downloaded for this root.
            env["TF_PLUGIN_CACHE_DIR"] = str(work / "plugin-cache")
            shutil.copytree(cache, work / "plugin-cache")
        result = subprocess.run(init, env=env, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            fail(f"{root}: terraform init for the example plan failed: {result.stderr.strip()[-500:]}")
            return
        result = subprocess.run(
            ["terraform", f"-chdir={dest}", "test", "-no-color", "-test-directory=example_tests",
             "-var-file=terraform.tfvars.example"],
            env=env, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            fail(f"{root}/terraform.tfvars.example does not plan:\n{result.stdout[-2000:]}{result.stderr[-1000:]}")
        else:
            print(f"{root}: terraform.tfvars.example plans (targeted step 3, and in full with images)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


# ---- 4. lock files -------------------------------------------------------------------------

def lock_entries(path: Path) -> dict[str, dict]:
    text = path.read_text()
    out = {}
    for m in re.finditer(r'provider "([^"]+)" \{(.*?)\n\}', text, re.S):
        body = m.group(2)
        out[m.group(1)] = {
            "version": re.search(r'version\s+=\s+"([^"]+)"', body).group(1),
            "constraints": re.search(r'constraints\s+=\s+"([^"]+)"', body).group(1),
            "hashes": sorted(re.findall(r'"((?:h1|zh):[^"]+)"', body)),
        }
    return out


def check_lock_files() -> None:
    bootstrap = lock_entries(REPO / "infra" / "bootstrap" / ".terraform.lock.hcl")
    module_constraints = set()
    for versions in (REPO / "infra" / "modules").glob("*/versions.tf"):
        for name, constraint in re.findall(r'(google(?:-beta)?)\s*=\s*\{[^}]*?version\s*=\s*"([^"]+)"', versions.read_text(), re.S):
            module_constraints.add(constraint)
    if module_constraints != {"~> 8.11"}:
        fail(f"the modules' provider constraints changed: {module_constraints}; update the roots and their lock files")
    for root in ROOTS:
        path = ENVS / root / ".terraform.lock.hcl"
        if not path.is_file():
            fail(f"{root}/.terraform.lock.hcl is missing (root modules commit their lock file)")
            continue
        entries = lock_entries(path)
        for provider in ("registry.terraform.io/hashicorp/google", "registry.terraform.io/hashicorp/google-beta"):
            e = entries.get(provider)
            if e is None:
                fail(f"{root} lock file lacks {provider}")
                continue
            major, minor = (int(x) for x in e["version"].split(".")[:2])
            if e["constraints"] != "~> 8.11" or major != 8 or minor < 11:
                fail(f"{root} lock: {provider} {e['version']} ({e['constraints']}) does not satisfy the modules' ~> 8.11")
            if bootstrap.get(provider, {}).get("version") == e["version"] and bootstrap[provider]["hashes"] != e["hashes"]:
                fail(f"{root} lock: {provider} hashes differ from bootstrap's (different platforms?)")
    if (ENVS / "dev" / ".terraform.lock.hcl").read_bytes() != (ENVS / "prod" / ".terraform.lock.hcl").read_bytes():
        fail("dev and prod lock files must be identical")


# ---- 5. providers ---------------------------------------------------------------------------

def check_providers() -> None:
    text = re.sub(r"#.*", "", (ENVS / "dev" / "providers.tf").read_text())
    blocks = dict(re.findall(r'provider\s+"([\w-]+)"\s*\{(.*?)\n\}', text, re.S))
    if set(blocks) != {"google", "google-beta"}:
        fail(f"providers.tf must configure exactly google and google-beta, found {sorted(blocks)}")
    for name, body in blocks.items():
        if not re.search(r"^\s*project\s*=\s*var\.project_id\s*$", body, re.M):
            fail(f"provider {name} must set project = var.project_id")
        if "alias" in body:
            fail(f"provider {name}: no aliases (the modules use the default configurations)")
    beta = blocks.get("google-beta", "")
    if not re.search(r"^\s*user_project_override\s*=\s*true\s*$", beta, re.M) or not re.search(r"^\s*billing_project\s*=\s*var\.project_id\s*$", beta, re.M):
        fail("provider google-beta needs user_project_override = true and billing_project = var.project_id")


def main() -> int:
    check_roots_differ_only_where_documented()
    check_no_hardcoded_identity()
    check_ignored()
    for root in ROOTS:
        check_example(root)
    check_lock_files()
    check_providers()
    if not failures:
        for root in ROOTS:
            check_example_plans(root)
    if failures:
        for f in failures:
            print("FAIL:", f, file=sys.stderr)
        return 1
    print("PASS: dev and prod differ only in env.tf/backend.tf; no author-specific values; examples, ignores, lock files and providers as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())
