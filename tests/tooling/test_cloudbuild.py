"""Checks over cloudbuild.yaml: its static properties, and its build script run for real.

The build script is the `script` of the one step. Cloud Build runs it with every substitution as
an environment variable (options.automapSubstitutions), so these tests run the same text with
bash, the same variables and a fake `docker` on PATH that records every call and answers the
digest query. That checks what the build does (which images it builds, how it tags and pushes
them, what it prints for tfvars, when it stops) without Docker, Cloud Build or a GCP project.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUDBUILD = REPO_ROOT / "cloudbuild.yaml"
PROJECT_BASE_VARIABLES = REPO_ROOT / "infra" / "modules" / "project-base" / "variables.tf"

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BUILD_ENV = {
    # Cloud Build's built-in substitutions, then this file's own. Synthetic values: nothing here
    # is a real project.
    "PROJECT_ID": "example-project",
    "TAG_NAME": "v1.2.3",
    "COMMIT_SHA": COMMIT,
    "_REGION": "europe-west1",
    "_REPOSITORY": "images-repo",
    "_DOCKERHUB_MIRROR": "hub-mirror",
}
REGISTRY = "europe-west1-docker.pkg.dev/example-project/images-repo"


def config() -> dict:
    return yaml.safe_load(CLOUDBUILD.read_text())


def script() -> str:
    steps = config()["steps"]
    assert len(steps) == 1, "one step: build, push and report"
    return steps[0]["script"]


def fake_digest(ref: str) -> str:
    return "sha256:" + hashlib.sha256(ref.encode()).hexdigest()


FAKE_DOCKER = """#!{python}
# Fake docker CLI for tests/tooling/test_cloudbuild.py: records each call, answers the digest query.
import hashlib, json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
fail = os.environ.get("FAKE_DOCKER_FAIL_BUILD")
if args[:1] == ["build"] and fail and any(fail in a for a in args):
    sys.exit(1)
if args[:3] == ["buildx", "imagetools", "inspect"]:
    if os.environ.get("FAKE_DOCKER_BAD_DIGEST"):
        print("not-a-digest")
    else:
        print("sha256:" + hashlib.sha256(args[3].encode()).hexdigest())
"""


def run_build(
    workdir: Path, tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run the step's script in `workdir` (Cloud Build's /workspace) with a fake docker."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER.format(python=sys.executable))
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    script_file = tmp_path / "step.sh"
    script_file.write_text(script())
    log = tmp_path / "docker-calls.jsonl"
    log.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        **BUILD_ENV,
        **overrides,
    }
    result = subprocess.run(
        ["bash", str(script_file)],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def make_tree(root: Path, services: dict[str, bool]) -> Path:
    """A scratch repository: services/<name>/ with or without a Dockerfile."""
    for name, has_dockerfile in services.items():
        d = root / "services" / name
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        if has_dockerfile:
            (d / "Dockerfile").write_text("FROM scratch\n")
    return root


def tfvars_block(stdout: str) -> list[str]:
    match = re.search(r"----- BEGIN tfvars[^\n]*\n(.*?)----- END tfvars -----", stdout, re.DOTALL)
    assert match, f"no tfvars block in the build output:\n{stdout}"
    return match.group(1).splitlines()


def builds(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:1] == ["build"]]


def flag_values(call: list[str], flag: str) -> list[str]:
    return [call[i + 1] for i, a in enumerate(call) if a == flag]


# --- static properties --------------------------------------------------------------------------


def test_builder_images_are_pinned_by_digest() -> None:
    for step in config()["steps"]:
        name = step["name"]
        assert re.search(r"@sha256:[0-9a-f]{64}$", name), f"builder not pinned: {name}"
        assert ":latest" not in name


def test_substitutions_default_to_the_project_base_module_defaults() -> None:
    # The defaults are the Terraform module's own defaults (nothing deployment-specific), so a
    # stranger's project built with project-base's defaults needs no overrides.
    subs = config()["substitutions"]
    assert set(subs) == {"_REGION", "_REPOSITORY", "_DOCKERHUB_MIRROR"}
    tf = PROJECT_BASE_VARIABLES.read_text()

    def tf_default(variable: str) -> str:
        match = re.search(rf'variable "{variable}" {{.*?default\s*=\s*"([^"]*)"', tf, re.DOTALL)
        assert match, variable
        return match.group(1)

    assert subs["_REGION"] == tf_default("region")
    assert subs["_REPOSITORY"] == tf_default("docker_repository_id")
    assert subs["_DOCKERHUB_MIRROR"] == tf_default("dockerhub_mirror_repository_id")


def test_options_map_substitutions_and_log_to_cloud_logging() -> None:
    options = config()["options"]
    assert options["automapSubstitutions"] is True, "the script reads substitutions from env"
    assert options["logging"] == "CLOUD_LOGGING_ONLY", "needed with a user-specified account"


def test_no_hardcoded_project_and_no_secrets() -> None:
    text = CLOUDBUILD.read_text()
    data = config()
    assert "availableSecrets" not in data and "secretEnv" not in json.dumps(data)
    # Every Artifact Registry path is built from PROJECT_ID, never a literal project.
    for match in re.finditer(r"docker\.pkg\.dev/([^/\s\"]+)", text):
        assert match.group(1) == "${PROJECT_ID}", f"hardcoded project: {match.group(0)}"
    assert "projects/" not in text
    # The images it pushes are the ones it built, with nothing listed for a second push.
    assert "images" not in data, "pushes happen in the script (immutable tags: push once)"


# --- the build script, run ----------------------------------------------------------------------


def test_builds_tags_and_pushes_every_dockerfile(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True, "beta-svc": True, "no-image": False})
    result, calls = run_build(tree, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    built = builds(calls)
    assert [flag_values(c, "--file") for c in built] == [
        ["services/alpha/Dockerfile"],
        ["services/beta-svc/Dockerfile"],
    ]
    for call, service in zip(built, ["alpha", "beta-svc"], strict=True):
        assert call[-1] == ".", "the context is the repository root"
        assert flag_values(call, "--tag") == [
            f"{REGISTRY}/{service}:v1.2.3",
            f"{REGISTRY}/{service}:{COMMIT}",
        ]
        assert flag_values(call, "--platform") == ["linux/amd64"]
        assert flag_values(call, "--build-arg") == [
            "PYTHON_REGISTRY=europe-west1-docker.pkg.dev/example-project/hub-mirror"
        ]
        assert f"org.opencontainers.image.revision={COMMIT}" in flag_values(call, "--label")

    pushes = [c[1] for c in calls if c[:1] == ["push"]]
    assert pushes == [
        f"{REGISTRY}/alpha:v1.2.3",
        f"{REGISTRY}/alpha:{COMMIT}",
        f"{REGISTRY}/beta-svc:v1.2.3",
        f"{REGISTRY}/beta-svc:{COMMIT}",
    ]
    # Each image is built before it is pushed, and its digest is read after the push.
    order = [c[0] if c[0] != "buildx" else "digest" for c in calls]
    assert order == ["build", "push", "push", "digest"] * 2


def test_prints_the_digests_ready_for_tfvars(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True, "beta-svc": True})
    result, _ = run_build(tree, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    # Variable names: the service name with hyphens as underscores, plus `_image`. The value is
    # the image by digest (no tag), which is what Cloud Run should deploy.
    assert tfvars_block(result.stdout) == [
        f'alpha_image = "{REGISTRY}/alpha@{fake_digest(f"{REGISTRY}/alpha:v1.2.3")}"',
        f'beta_svc_image = "{REGISTRY}/beta-svc@{fake_digest(f"{REGISTRY}/beta-svc:v1.2.3")}"',
    ]


def test_builds_exactly_the_repository_dockerfiles(tmp_path: Path) -> None:
    # Run against the real repository tree: every services/*/Dockerfile, nothing else.
    result, calls = run_build(REPO_ROOT, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = sorted(
        p.relative_to(REPO_ROOT).as_posix() for p in REPO_ROOT.glob("services/*/Dockerfile")
    )
    assert sorted(flag_values(c, "--file")[0] for c in builds(calls)) == expected
    names = [line.split(" = ")[0] for line in tfvars_block(result.stdout)]
    assert {"v2_to_fhir_image", "health_sync_image", "cuff_sim_image"} <= set(names)


def test_without_a_mirror_the_base_image_comes_from_docker_hub(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True})
    result, calls = run_build(tree, tmp_path, _DOCKERHUB_MIRROR="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert flag_values(builds(calls)[0], "--build-arg") == ["PYTHON_REGISTRY=docker.io"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"TAG_NAME": ""}, "TAG_NAME"),  # a manual build without a tag
        ({"TAG_NAME": "bad tag!"}, "TAG_NAME"),
        ({"COMMIT_SHA": "abc123"}, "COMMIT_SHA"),
        ({"PROJECT_ID": ""}, "PROJECT_ID"),
        ({"_REGION": ""}, "_REGION"),
        ({"_REPOSITORY": ""}, "_REPOSITORY"),
    ],
)
def test_refuses_bad_inputs_before_building(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True})
    result, calls = run_build(tree, tmp_path, **overrides)
    assert result.returncode != 0
    assert message in result.stderr
    assert calls == [], "nothing is built or pushed"


def test_a_failed_build_fails_the_build_and_prints_no_tfvars(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True, "beta": True})
    result, calls = run_build(tree, tmp_path, FAKE_DOCKER_FAIL_BUILD="services/beta/")
    assert result.returncode != 0
    assert "BEGIN tfvars" not in result.stdout, "a partial set of digests is never printed"
    assert [c[1] for c in calls if c[:1] == ["push"]] == [
        f"{REGISTRY}/alpha:v1.2.3",
        f"{REGISTRY}/alpha:{COMMIT}",
    ]


def test_an_unreadable_digest_fails_the_build(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True})
    result, _ = run_build(tree, tmp_path, FAKE_DOCKER_BAD_DIGEST="1")
    assert result.returncode != 0
    assert "BEGIN tfvars" not in result.stdout


def test_no_dockerfiles_fails_the_build(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": False})
    result, calls = run_build(tree, tmp_path)
    assert result.returncode != 0
    assert calls == []
