"""Checks over cloudbuild.yaml: its static properties, and its build script run for real.

The build script is the `script` of the one step. Cloud Build runs it with every substitution as
an environment variable (options.automapSubstitutions), so these tests run the same text with
bash, the same variables and a fake `docker` on PATH. The fake records every call and keeps a
small registry in a state file that behaves like the services repository: tags are immutable (a
second push of a tag is refused), a rebuilt image gets a new digest, and a tag can be added to an
existing image without changing its digest. That checks what the build does (which images it
builds, in which order it builds and pushes, what it prints for tfvars, when it stops, how a
re-run recovers) without Docker, Cloud Build or a GCP project.
"""

from __future__ import annotations

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


FAKE_DOCKER = """#!{python}
# Fake docker CLI for tests/tooling/test_cloudbuild.py. State (local images, registry tags,
# build count) lives in FAKE_DOCKER_STATE so it survives between runs of the build.
import hashlib, json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
state_path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {{}}
local = state.setdefault("local", {{}})
registry = state.setdefault("registry", {{}})

def save():
    json.dump(state, open(state_path, "w"))

def flag(name):
    return [args[i + 1] for i, a in enumerate(args) if a == name]

if args[:1] == ["build"]:
    fail = os.environ.get("FAKE_DOCKER_FAIL_BUILD")
    if fail and any(fail in a for a in args):
        sys.exit(1)
    state["builds"] = state.get("builds", 0) + 1
    # Every build makes a new image, so a rebuild has a different digest.
    digest = "sha256:" + hashlib.sha256(f"build-{{state['builds']}}".encode()).hexdigest()
    for tag in flag("--tag"):
        local[tag] = digest
elif args[:1] == ["push"]:
    ref = args[1]
    if ref == os.environ.get("FAKE_DOCKER_FAIL_PUSH"):
        sys.exit(1)
    if ref in registry:
        print(f"{{ref}}: tag is immutable", file=sys.stderr)
        sys.exit(1)
    if ref not in local:
        sys.exit(1)
    registry[ref] = local[ref]
    if ref == os.environ.get("FAKE_DOCKER_PUSH_ELSEWHERE"):
        # Simulates the tag ending up on another image during the run (another writer).
        registry[ref] = "sha256:" + "e" * 64
elif args[:3] == ["buildx", "imagetools", "inspect"]:
    ref = args[3]
    if ref not in registry:
        sys.exit(1)
    print("not-a-digest" if os.environ.get("FAKE_DOCKER_BAD_DIGEST") else registry[ref])
elif args[:3] == ["buildx", "imagetools", "create"]:
    assert "--prefer-index=false" in args, "a carbon copy keeps the digest"
    (tag,) = flag("--tag")
    source = args[-1]
    if tag in registry:
        sys.exit(1)
    digest = source.split("@", 1)[1]
    if digest not in registry.values():
        sys.exit(1)
    registry[tag] = digest
else:
    sys.exit(2)
save()
"""


def run_build(
    workdir: Path, tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run the step's script in `workdir` (Cloud Build's /workspace) with the fake docker.

    The fake registry persists in tmp_path, so a second call is a re-run against what the first
    one pushed."""
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
        "FAKE_DOCKER_STATE": str(tmp_path / "docker-state.json"),
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


def registry_state(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "docker-state.json"
    return json.loads(path.read_text()).get("registry", {}) if path.exists() else {}


def seed_registry(tmp_path: Path, tags: dict[str, str]) -> None:
    (tmp_path / "docker-state.json").write_text(json.dumps({"registry": tags, "local": {}}))


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


def tfvars_digests(stdout: str) -> dict[str, str]:
    return {line.split(" = ")[0]: line.split("@")[1].rstrip('"') for line in tfvars_block(stdout)}


def builds(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:1] == ["build"]]


def pushes(calls: list[list[str]]) -> list[str]:
    return [c[1] for c in calls if c[:1] == ["push"]]


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
        assert set(flag_values(call, "--tag")) == {
            f"{REGISTRY}/{service}:v1.2.3",
            f"{REGISTRY}/{service}:{COMMIT}",
        }
        assert flag_values(call, "--platform") == ["linux/amd64"]
        assert "--provenance=false" in call, "a plain image manifest, no attestation index"
        assert flag_values(call, "--build-arg") == [
            "PYTHON_REGISTRY=europe-west1-docker.pkg.dev/example-project/hub-mirror"
        ]
        assert f"org.opencontainers.image.revision={COMMIT}" in flag_values(call, "--label")

    # The commit tag goes first: a re-run finds an image by its commit tag.
    assert pushes(calls) == [
        f"{REGISTRY}/alpha:{COMMIT}",
        f"{REGISTRY}/alpha:v1.2.3",
        f"{REGISTRY}/beta-svc:{COMMIT}",
        f"{REGISTRY}/beta-svc:v1.2.3",
    ]
    # Every image is built before anything is pushed.
    kinds = [c[0] for c in calls]
    assert max(i for i, k in enumerate(kinds) if k == "build") < kinds.index("push")


def test_prints_the_digests_ready_for_tfvars(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True, "beta-svc": True})
    result, _ = run_build(tree, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    stored = registry_state(tmp_path)
    # Variable names: the service name with hyphens as underscores, plus `_image`. The value is
    # the image by digest (no tag), as the registry stores it: what Cloud Run should deploy.
    assert tfvars_block(result.stdout) == [
        f'alpha_image = "{REGISTRY}/alpha@{stored[f"{REGISTRY}/alpha:v1.2.3"]}"',
        f'beta_svc_image = "{REGISTRY}/beta-svc@{stored[f"{REGISTRY}/beta-svc:v1.2.3"]}"',
    ]
    assert stored[f"{REGISTRY}/alpha:v1.2.3"] != stored[f"{REGISTRY}/beta-svc:v1.2.3"]


def test_builds_exactly_the_repository_dockerfiles(tmp_path: Path) -> None:
    # Run against the real repository tree: every services/*/Dockerfile, nothing else.
    result, calls = run_build(REPO_ROOT, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = sorted(
        p.relative_to(REPO_ROOT).as_posix() for p in REPO_ROOT.glob("services/*/Dockerfile")
    )
    assert sorted(flag_values(c, "--file")[0] for c in builds(calls)) == expected
    names = [line.split(" = ")[0] for line in tfvars_block(result.stdout)]
    assert {
        "v2_to_fhir_image",
        "health_sync_image",
        "cuff_sim_image",
        "seed_image",
        "fhir_proxy_image",
    } <= set(names)
    # One line per image, the job image (seed, a Cloud Run job) included.
    assert len(names) == len(expected)


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
    assert calls == [], "nothing is looked up, built or pushed"


@pytest.mark.parametrize("bad", ["Beta", "beta_svc", "beta--svc", "-beta", "beta.svc"])
def test_a_bad_service_name_fails_before_any_build(tmp_path: Path, bad: str) -> None:
    # The name becomes an image name and a tfvars variable name (`-` as `_`).
    tree = make_tree(tmp_path / "repo", {"alpha": True, bad: True})
    result, calls = run_build(tree, tmp_path)
    assert result.returncode != 0
    assert bad in result.stderr
    assert calls == []


def test_a_failed_build_of_the_last_service_pushes_nothing(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True, "beta": True, "gamma": True})
    result, calls = run_build(tree, tmp_path, FAKE_DOCKER_FAIL_BUILD="services/gamma/")
    assert result.returncode != 0
    assert len(builds(calls)) == 3
    assert pushes(calls) == [], "nothing is pushed, so the same tag can be built again"
    assert registry_state(tmp_path) == {}
    assert "BEGIN tfvars" not in result.stdout

    again, _ = run_build(tree, tmp_path)
    assert again.returncode == 0, again.stdout + again.stderr


def test_a_push_failure_midway_is_finished_by_a_re_run(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True, "beta": True})
    first, _ = run_build(tree, tmp_path, FAKE_DOCKER_FAIL_PUSH=f"{REGISTRY}/beta:{COMMIT}")
    assert first.returncode != 0
    assert "BEGIN tfvars" not in first.stdout
    alpha_digest = registry_state(tmp_path)[f"{REGISTRY}/alpha:{COMMIT}"]
    assert f"{REGISTRY}/beta:{COMMIT}" not in registry_state(tmp_path)

    # The same tag again: alpha is not rebuilt (its tags could not be pushed again), beta is.
    second, calls = run_build(tree, tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert [flag_values(c, "--file") for c in builds(calls)] == [["services/beta/Dockerfile"]]
    assert pushes(calls) == [f"{REGISTRY}/beta:{COMMIT}", f"{REGISTRY}/beta:v1.2.3"]
    assert "alpha" in second.stdout and "reusing" in second.stdout
    digests = tfvars_digests(second.stdout)
    assert digests["alpha_image"] == alpha_digest
    assert digests["beta_image"] == registry_state(tmp_path)[f"{REGISTRY}/beta:v1.2.3"]


def test_a_failure_between_the_two_pushes_is_finished_by_a_re_run(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True})
    first, _ = run_build(tree, tmp_path, FAKE_DOCKER_FAIL_PUSH=f"{REGISTRY}/alpha:v1.2.3")
    assert first.returncode != 0
    commit_digest = registry_state(tmp_path)[f"{REGISTRY}/alpha:{COMMIT}"]

    second, calls = run_build(tree, tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert builds(calls) == [] and pushes(calls) == []
    assert registry_state(tmp_path)[f"{REGISTRY}/alpha:v1.2.3"] == commit_digest
    assert tfvars_digests(second.stdout) == {"alpha_image": commit_digest}


def test_a_new_tag_on_a_released_commit_reuses_its_images(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "repo", {"alpha": True, "beta": True})
    first, _ = run_build(tree, tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    second, calls = run_build(tree, tmp_path, TAG_NAME="v1.2.4")
    assert second.returncode == 0, second.stdout + second.stderr
    assert builds(calls) == [], "the commit's images already exist"
    assert tfvars_digests(second.stdout) == tfvars_digests(first.stdout)
    stored = registry_state(tmp_path)
    assert stored[f"{REGISTRY}/beta:v1.2.4"] == stored[f"{REGISTRY}/beta:v1.2.3"]


def test_a_release_tag_without_its_commit_tag_is_refused(tmp_path: Path) -> None:
    # The commit tag is always pushed first, so this tag was made by something else.
    seed_registry(tmp_path, {f"{REGISTRY}/alpha:v1.2.3": "sha256:" + "a" * 64})
    tree = make_tree(tmp_path / "repo", {"alpha": True})
    result, calls = run_build(tree, tmp_path)
    assert result.returncode != 0
    assert builds(calls) == [] and pushes(calls) == []


def test_tags_naming_different_images_are_refused(tmp_path: Path) -> None:
    seed_registry(
        tmp_path,
        {
            f"{REGISTRY}/alpha:v1.2.3": "sha256:" + "a" * 64,
            f"{REGISTRY}/alpha:{COMMIT}": "sha256:" + "b" * 64,
        },
    )
    tree = make_tree(tmp_path / "repo", {"alpha": True})
    result, calls = run_build(tree, tmp_path)
    assert result.returncode != 0
    assert "different images" in result.stderr
    assert builds(calls) == []


def test_the_report_checks_both_tags_name_one_image(tmp_path: Path) -> None:
    # Checked again after the pushes, not only in the plan: the block must never pair a release
    # tag with a digest the commit tag does not name.
    tree = make_tree(tmp_path / "repo", {"alpha": True})
    result, _ = run_build(tree, tmp_path, FAKE_DOCKER_PUSH_ELSEWHERE=f"{REGISTRY}/alpha:v1.2.3")
    assert result.returncode != 0
    assert "different images" in result.stderr
    assert "BEGIN tfvars" not in result.stdout


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
