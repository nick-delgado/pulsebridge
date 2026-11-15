"""Static checks over the service images: every services/*/Dockerfile and the root .dockerignore.

These run in `make test` (no Docker needed). They check what the T24 acceptance criteria and
services/README.md ("Container images") promise about each image, by reading the Dockerfile and
comparing it with the service's own pyproject.toml and README, so a change on either side that
breaks the promise fails here:

- the base image is pinned by digest, the same in every stage and every Dockerfile;
- the image runs as a numeric, non-root user;
- a service image (a Cloud Run service, an HTTP server) starts with the gunicorn command from
  the service's README (exec form) and honours PORT; a job image (a Cloud Run job, a command
  that runs to completion, listed in JOB_IMAGES) has the README's console script as its
  ENTRYPOINT, takes its arguments from the job, and has no PORT and no gunicorn settings;
- it copies and installs exactly the local libraries the service declares (and what those
  declare), libraries first;
- every service with a pyproject.toml has a Dockerfile, or is on a justified allow-list;
- the build context (.dockerignore) leaves out the git metadata, environments, state and keys.

`make images-smoke` (tests/tooling/test_image_smoke.py) checks the built images themselves.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES = REPO_ROOT / "services"
LIBS = REPO_ROOT / "libs"

# Services that have a pyproject.toml but no image yet. Each entry needs a reason; a service that
# is neither here nor has a Dockerfile fails test_every_service_has_a_dockerfile, so a new service
# cannot ship without an image by accident. When a task adds the Dockerfile it can leave the entry
# (the Dockerfile is then checked like any other) or remove it.
NO_IMAGE_YET = {
    "fhir-proxy": (
        "T14/T15 (FHIR proxy) not built yet. T14 adds services/fhir-proxy/Dockerfile following "
        "services/README.md ('Adding an image'); the proxy is a Cloud Run service (T22)."
    ),
}

# Images that run as a Cloud Run JOB, not a Cloud Run service: a command that runs to completion
# and serves nothing (services/README.md, "Job images"). Every other image is a service, and its
# README's "Running" section must hold a gunicorn command; a job's must not, and its command is
# read from that section instead. Each entry needs a reason.
JOB_IMAGES = {
    "seed": (
        "The synthetic data seeder (T09) runs as a Cloud Run job (google_cloud_run_v2_job, T20): "
        "one execution per seeding or hourly top-up, its arguments set by the job."
    ),
}

DIGEST_REF = re.compile(r"@sha256:[0-9a-f]{64}$")


# --- a small Dockerfile reader ------------------------------------------------------------------


@dataclass(frozen=True)
class Instruction:
    keyword: str  # upper case, e.g. "COPY"
    args: str  # the rest of the logical line, continuation lines joined
    stage: int  # index of the FROM this instruction belongs to (-1 before the first FROM)


def read_dockerfile(path: Path) -> list[Instruction]:
    """Parse a Dockerfile into instructions: comments dropped, `\\` continuations joined.

    Enough of the Dockerfile grammar for these checks (no heredocs, no custom escape character;
    test_dockerfile_uses_no_unsupported_syntax keeps it that way)."""
    instructions: list[Instruction] = []
    stage = -1
    pending = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if pending and line.startswith("#"):
            continue  # Docker drops comment lines inside a continued instruction
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        logical = (pending + line).strip()
        pending = ""
        keyword, _, args = logical.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            stage += 1
        instructions.append(Instruction(keyword, args.strip(), stage))
    assert not pending, f"{path}: file ends inside a continued instruction"
    return instructions


def final_stage(instructions: list[Instruction]) -> list[Instruction]:
    last = max(i.stage for i in instructions)
    return [i for i in instructions if i.stage == last]


def from_image(instruction: Instruction) -> str:
    """The image reference of a FROM, with `--platform` flags and `AS name` removed."""
    words = [w for w in instruction.args.split() if not w.startswith("--")]
    return words[0]


def stage_names(instructions: list[Instruction]) -> set[str]:
    names = set()
    for i in instructions:
        if i.keyword == "FROM":
            words = i.args.split()
            if len(words) >= 3 and words[-2].upper() == "AS":
                names.add(words[-1])
    return names


def exec_form(instruction: Instruction) -> list[str]:
    """The JSON array of an exec-form CMD/ENTRYPOINT/RUN; fails for shell form."""
    try:
        value = json.loads(instruction.args)
    except json.JSONDecodeError:
        pytest.fail(f"{instruction.keyword} is not exec form (a JSON array): {instruction.args}")
    assert isinstance(value, list) and all(isinstance(v, str) for v in value), instruction.args
    return value


def env_of(instructions: list[Instruction]) -> dict[str, str]:
    """ENV values set in these instructions (`ENV A=1 B=2` form)."""
    env: dict[str, str] = {}
    for i in instructions:
        if i.keyword == "ENV":
            for word in shlex.split(i.args):
                name, sep, value = word.partition("=")
                assert sep, f"use the ENV NAME=value form: {i.args}"
                env[name] = value
    return env


def copy_sources(instructions: list[Instruction]) -> list[str]:
    """Build-context sources of every COPY (not COPY --from=<stage>), normalised."""
    sources = []
    for i in instructions:
        if i.keyword != "COPY":
            continue
        words = i.args.split()
        if any(w.startswith("--from=") for w in words):
            continue
        words = [w for w in words if not w.startswith("--")]
        sources += [w.rstrip("/") for w in words[:-1]]
    return sources


# --- what each service declares -----------------------------------------------------------------


def normalise(name: str) -> str:
    """PEP 503 normalised distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str:
    return normalise(re.split(r"[\s\[<>=!~;@(]", requirement.strip(), maxsplit=1)[0])


def pyproject(path: Path) -> dict:
    return tomllib.loads((path / "pyproject.toml").read_text())


def local_libraries() -> dict[str, Path]:
    """Distribution name -> directory, for every library under libs/."""
    return {
        normalise(pyproject(d)["project"]["name"]): d
        for d in sorted(LIBS.iterdir())
        if (d / "pyproject.toml").is_file()
    }


def needed_libraries(service_dir: Path) -> set[str]:
    """libs/ directories (relative paths) the service needs: its own local dependencies and
    theirs, transitively."""
    libs = local_libraries()
    needed: set[str] = set()
    todo = [requirement_name(r) for r in pyproject(service_dir)["project"].get("dependencies", [])]
    while todo:
        name = todo.pop()
        if name in libs and name not in needed:
            needed.add(name)
            todo += [
                requirement_name(r)
                for r in pyproject(libs[name])["project"].get("dependencies", [])
            ]
    return {libs[name].relative_to(REPO_ROOT).as_posix() for name in needed}


def readme_running_section(service_dir: Path) -> str:
    """The README's section whose heading starts with "## Running", up to the next heading."""
    text = (service_dir / "README.md").read_text()
    section = text.split("\n## Running", 1)
    assert len(section) == 2, f"{service_dir.name}/README.md has no '## Running' section"
    return section[1].split("\n## ", 1)[0]


def readme_gunicorn_commands(service_dir: Path) -> list[str]:
    section = readme_running_section(service_dir)
    return [line.strip() for line in section.splitlines() if line.strip().startswith("gunicorn ")]


def readme_gunicorn_argv(service_dir: Path) -> list[str]:
    """The gunicorn command in the service README's "Running" section, as an argv."""
    commands = readme_gunicorn_commands(service_dir)
    assert len(commands) == 1, f"expected one gunicorn command in {service_dir.name}'s README"
    return shlex.split(commands[0])


def readme_job_program(service_dir: Path) -> str:
    """The program a job's README runs: the first word of every command in the first code block
    of its "Running" section. They must all be the same program."""
    section = readme_running_section(service_dir)
    blocks = re.findall(r"```(?:bash|sh|console)?\n(.*?)```", section, re.DOTALL)
    assert blocks, f"{service_dir.name}/README.md: no code block in its 'Running' section"
    programs = {
        shlex.split(line, comments=True)[0]
        for line in blocks[0].splitlines()
        if shlex.split(line, comments=True)
    }
    assert len(programs) == 1, f"{service_dir.name}'s README runs more than one program: {programs}"
    return programs.pop()


# --- the services -------------------------------------------------------------------------------


def service_dirs() -> list[Path]:
    return sorted(d for d in SERVICES.iterdir() if (d / "pyproject.toml").is_file())


def dockerfile_dirs() -> list[Path]:
    return sorted(p.parent for p in SERVICES.glob("*/Dockerfile"))


IMAGE_SERVICES = [d.name for d in dockerfile_dirs()]
# Cloud Run services (gunicorn) and Cloud Run jobs (a console script), by JOB_IMAGES.
WEB_IMAGES = [name for name in IMAGE_SERVICES if name not in JOB_IMAGES]
JOB_IMAGE_NAMES = [name for name in IMAGE_SERVICES if name in JOB_IMAGES]


@pytest.fixture(params=IMAGE_SERVICES)
def service(request: pytest.FixtureRequest) -> Path:
    """Every image, service or job: the checks that apply to both."""
    return SERVICES / request.param


@pytest.fixture(params=WEB_IMAGES)
def web_service(request: pytest.FixtureRequest) -> Path:
    return SERVICES / request.param


@pytest.fixture(params=JOB_IMAGE_NAMES)
def job(request: pytest.FixtureRequest) -> Path:
    return SERVICES / request.param


def test_there_are_images_to_check() -> None:
    # Guards the parametrised tests below against silently checking nothing.
    assert {"v2-to-fhir", "health-sync", "cuff-sim"} <= set(WEB_IMAGES)
    assert {"seed"} <= set(JOB_IMAGE_NAMES)


def test_job_images_are_justified_and_exist() -> None:
    for name, reason in JOB_IMAGES.items():
        assert len(reason) > 40, f"JOB_IMAGES[{name!r}] needs a real reason"
        assert (SERVICES / name / "Dockerfile").is_file(), f"JOB_IMAGES[{name!r}] has no Dockerfile"
        assert name not in NO_IMAGE_YET, name


def test_every_image_is_either_a_service_or_a_job() -> None:
    # The kind comes from JOB_IMAGES and the README, never from the Dockerfile being checked: a
    # job image given a gunicorn CMD is still checked as a job (and fails), and a service left
    # out of JOB_IMAGES by mistake is checked as a service.
    for d in dockerfile_dirs():
        gunicorn = readme_gunicorn_commands(d)
        declared = {requirement_name(r) for r in pyproject(d)["project"].get("dependencies", [])}
        if d.name in JOB_IMAGES:
            assert not gunicorn, f"{d.name} is a job, but its README runs gunicorn"
            assert "gunicorn" not in declared, f"{d.name} is a job: it does not need gunicorn"
        else:
            assert len(gunicorn) == 1, (
                f"{d.name}: its README has no gunicorn command. A Cloud Run job goes in "
                "JOB_IMAGES with its README's command."
            )


def test_every_service_has_a_dockerfile() -> None:
    missing = [
        d.name
        for d in service_dirs()
        if not (d / "Dockerfile").is_file() and d.name not in NO_IMAGE_YET
    ]
    assert not missing, (
        f"services without a Dockerfile: {missing}. Add one (services/README.md, 'Adding an "
        "image') or, with a reason, add the service to NO_IMAGE_YET in this file."
    )


def test_allow_list_entries_are_justified() -> None:
    for name, reason in NO_IMAGE_YET.items():
        assert re.fullmatch(r"[a-z0-9-]+", name), name
        assert len(reason) > 40, f"NO_IMAGE_YET[{name!r}] needs a real reason"


def test_every_dockerfile_belongs_to_a_service() -> None:
    for d in dockerfile_dirs():
        assert (d / "pyproject.toml").is_file(), f"{d.name}/Dockerfile has no pyproject.toml"


def test_dockerfile_uses_no_unsupported_syntax(service: Path) -> None:
    # The reader above does not handle heredocs or a changed escape character, and a `syntax=`
    # directive would pull an unpinned BuildKit frontend image from Docker Hub.
    text = (service / "Dockerfile").read_text()
    assert "<<" not in text, "heredocs are not used in the service Dockerfiles"
    assert not re.search(r"^#\s*(syntax|escape)\s*=", text, re.MULTILINE | re.IGNORECASE)


def test_base_image_is_pinned_by_digest(service: Path) -> None:
    instructions = read_dockerfile(service / "Dockerfile")
    froms = [from_image(i) for i in instructions if i.keyword == "FROM"]
    assert len(froms) >= 2, "multi-stage: a build stage and a runtime stage"
    for ref in froms:
        assert DIGEST_REF.search(ref), f"not pinned by digest: {ref}"
        assert ":latest" not in ref, ref
        # Only the registry may vary (PYTHON_REGISTRY); the repository, tag and digest are fixed.
        assert ref.startswith("${PYTHON_REGISTRY}/library/python:3.13"), ref
    assert len(set(froms)) == 1, f"every stage uses the same base image: {froms}"


def test_every_dockerfile_uses_the_same_base_image() -> None:
    # One digest to bump (services/README.md, "Bumping the base image").
    refs = {
        from_image(i)
        for d in dockerfile_dirs()
        for i in read_dockerfile(d / "Dockerfile")
        if i.keyword == "FROM"
    }
    assert len(refs) == 1, refs


def test_registry_argument_defaults_to_docker_hub(service: Path) -> None:
    instructions = read_dockerfile(service / "Dockerfile")
    args = [i.args for i in instructions if i.keyword == "ARG" and i.stage == -1]
    assert args == ["PYTHON_REGISTRY=docker.io"], args


def test_no_add_and_no_external_copy(service: Path) -> None:
    instructions = read_dockerfile(service / "Dockerfile")
    # ADD can fetch URLs and unpack archives; COPY does neither. COPY --from must name a stage of
    # this file: an image name there would be an unpinned pull.
    assert not [i for i in instructions if i.keyword == "ADD"], "use COPY, not ADD"
    names = stage_names(instructions)
    for i in instructions:
        for word in i.args.split():
            if i.keyword == "COPY" and word.startswith("--from="):
                assert word.removeprefix("--from=") in names, i.args


def test_runs_as_a_numeric_non_root_user(service: Path) -> None:
    instructions = read_dockerfile(service / "Dockerfile")
    users = [i for i in instructions if i.keyword == "USER"]
    assert users, "no USER: the image would run as root"
    last = users[-1]
    assert last.stage == max(i.stage for i in instructions), "the last USER is in the final stage"
    match = re.fullmatch(r"(\d+)(?::(\d+))?", last.args)
    assert match, f"USER must be numeric (uid[:gid]) so the runtime can check it: {last.args}"
    assert int(match.group(1)) != 0, "USER 0 is root"
    if match.group(2) is not None:
        assert int(match.group(2)) != 0, "group 0 is root's group"
    # Nothing after the last USER switches back or runs as another user.
    after = instructions[instructions.index(last) + 1 :]
    assert not [i for i in after if i.keyword in {"USER", "RUN"}], "no RUN or USER after USER"


def test_starts_with_the_readme_gunicorn_command(web_service: Path) -> None:
    service = web_service
    instructions = final_stage(read_dockerfile(service / "Dockerfile"))
    cmds = [i for i in instructions if i.keyword == "CMD"]
    assert len(cmds) == 1, "exactly one CMD"
    assert not [i for i in instructions if i.keyword == "ENTRYPOINT"], "the CMD is the command"
    cmd = exec_form(cmds[-1])

    readme = readme_gunicorn_argv(service)
    # The README binds with `--bind ":${PORT:-8080}"`, which needs a shell to expand. The image
    # gets the same result without one: it sets PORT=8080, and gunicorn binds 0.0.0.0:$PORT when
    # PORT is set and no --bind is given. Every other argument must be the README's, in order,
    # including the worker count (one worker: the services keep per-process state).
    bind_at = readme.index("--bind")
    assert readme[bind_at + 1] == ":${PORT:-8080}", readme
    expected = readme[:bind_at] + readme[bind_at + 2 :]
    assert cmd == expected, f"CMD {cmd} is not the README's command {readme}"
    assert "--bind" not in cmd and not any(a.startswith(("-b", "--bind=")) for a in cmd)
    assert cmd[cmd.index("--workers") + 1] == "1"

    env = env_of(instructions)
    assert env.get("PORT") == "8080", "PORT must default to the README's 8080"
    assert env.get("PYTHONUNBUFFERED") == "1"
    assert env.get("PATH", "").startswith("/opt/venv/bin:"), "gunicorn comes from the venv"
    # GUNICORN_CMD_ARGS is read by gunicorn as extra arguments: it must not override the bind,
    # workers or anything else the README sets.
    # It holds exactly two settings the README leaves at gunicorn's defaults (see the Dockerfile).
    extra = shlex.split(env.get("GUNICORN_CMD_ARGS", ""))
    assert extra == ["--no-control-socket", "--graceful-timeout", "8"], extra


def test_job_runs_the_readme_command_with_the_jobs_arguments(job: Path) -> None:
    instructions = final_stage(read_dockerfile(job / "Dockerfile"))
    program = readme_job_program(job)
    scripts = pyproject(job)["project"].get("scripts", {})
    assert program in scripts, f"{program} (README) is not a console script of {job.name}"

    # ENTRYPOINT is the console script alone, in exec form. Cloud Run's container `args`
    # replace the image's CMD and keep its ENTRYPOINT (`command` would replace it), so the job
    # supplies the arguments (services/README.md, "Job images").
    entrypoints = [i for i in instructions if i.keyword == "ENTRYPOINT"]
    assert len(entrypoints) == 1, "exactly one ENTRYPOINT: the job's command"
    assert exec_form(entrypoints[0]) == [program]
    # No CMD: without args the command itself runs (and asks for its arguments) rather than a
    # default that would make a mis-wired job succeed without doing anything.
    assert not [i for i in instructions if i.keyword == "CMD"], "a job image has no CMD"

    env = env_of(instructions)
    assert env.get("PYTHONUNBUFFERED") == "1"
    assert env.get("PATH", "").startswith("/opt/venv/bin:"), "the command comes from the venv"
    # A job serves nothing: no port and no web server settings.
    assert "PORT" not in env and "GUNICORN_CMD_ARGS" not in env, env
    assert not [i for i in instructions if i.keyword == "EXPOSE"], "a job listens on nothing"
    assert not [i for i in instructions if "gunicorn" in i.args.lower()], "a job runs no gunicorn"


def test_copies_exactly_the_libraries_the_service_declares(service: Path) -> None:
    instructions = read_dockerfile(service / "Dockerfile")
    service_path = service.relative_to(REPO_ROOT).as_posix()
    expected_libs = needed_libraries(service)
    assert expected_libs, f"{service.name} declares no local library; check needed_libraries"

    sources = copy_sources(instructions)
    assert sorted(sources) == sorted(expected_libs | {service_path}), (
        f"COPY sources {sources} != the service plus its local libraries {sorted(expected_libs)}"
    )
    # The service is copied after its libraries (so a service-only change reuses the cached
    # library layers) and everything comes from the build stage, not the runtime stage.
    assert sources[-1] == service_path
    for i in final_stage(instructions):
        if i.keyword == "COPY":
            assert "--from=" in i.args, f"the runtime stage copies only from the build: {i.args}"
            # The venv stays owned by root and read-only to the app user, so the service cannot
            # change its own code. --chown or --chmod would change that.
            assert "--chown" not in i.args and "--chmod" not in i.args, i.args


def test_installs_the_libraries_before_the_service(service: Path) -> None:
    instructions = read_dockerfile(service / "Dockerfile")
    service_path = service.relative_to(REPO_ROOT).as_posix()
    installs = [i.args for i in instructions if i.keyword == "RUN" and "pip" in i.args]
    text = " ".join(installs)
    assert f"install ./{service_path}" in text, "the service is installed from its directory"
    assert "-e " not in text and "--editable" not in text, "non-editable installs only"
    service_at = text.index(f"./{service_path}")
    for lib in needed_libraries(service):
        assert f"./{lib}" in text, f"{lib} is copied but not installed"
        assert text.index(f"./{lib}") < service_at, f"{lib} must be installed before the service"


def test_no_secrets_in_the_image_definition(service: Path) -> None:
    instructions = read_dockerfile(service / "Dockerfile")
    secretish = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|_KEY\b|^KEY", re.IGNORECASE)
    for i in instructions:
        if i.keyword in {"ENV", "ARG", "LABEL"}:
            for word in shlex.split(i.args):
                name = word.partition("=")[0]
                assert not secretish.search(name), f"{i.keyword} {name}: secrets never go in"
    sources = copy_sources(instructions)
    assert not [s for s in sources if s in {".", "./", "*"}], "never copy the whole context"
    # Build secrets are mounted for one RUN only (never stored in a layer), and only pip's CA.
    for i in instructions:
        for mount in re.findall(r"--mount=(\S+)", i.args):
            if "type=secret" in mount:
                assert mount == "type=secret,id=pip_cert", mount


# --- the build context --------------------------------------------------------------------------


def dockerignore_patterns() -> list[str]:
    lines = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """A .dockerignore pattern as a regex (Go filepath.Match plus `**`, as Docker reads it)."""
    pattern = pattern.strip("/")
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        elif pattern[i] == "[":
            end = pattern.index("]", i)
            out += pattern[i : end + 1]
            i = end + 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out)


def is_excluded(path: str, patterns: list[str]) -> bool:
    """Whether Docker leaves `path` out of the build context: the last pattern that matches the
    path or one of its parent directories decides; a `!` pattern re-includes."""
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    excluded = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        regex = _pattern_regex(pattern.removeprefix("!"))
        if any(regex.fullmatch(c) for c in candidates):
            excluded = not negate
    return excluded


MUST_BE_EXCLUDED = [
    ".git/config",
    ".git",  # a worktree's .git is a file
    ".worktrees/t01-repo-scaffolding/Makefile",
    ".venv/bin/python",
    "web/node_modules/vite/package.json",
    "web/src/main.ts",
    "infra/envs/dev/terraform.tfstate",
    "infra/envs/dev/terraform.tfstate.backup",
    "infra/envs/dev/terraform.tfvars",
    "infra/envs/dev/.terraform/providers/x",
    "infra/bootstrap/plan.tfplan",
    ".env",
    "sa-key.json",
    "client_secret_123.json",
    "key.pem",
    # The same things inside the directories that are sent.
    "services/v2-to-fhir/.env",
    "services/v2-to-fhir/.env.local",
    "services/health-sync/oauth-credentials.json",
    "services/cuff-sim/my-service-account.json",
    "libs/pulsebridge_gcp/sa-key.json",
    "libs/pulsebridge_gcp/client.key",
    "libs/pulsebridge_fhir/cert.pem",
    "services/v2-to-fhir/.venv/bin/python",
    "services/v2-to-fhir/tests/test_app.py",
    "libs/pulsebridge_fhir/tests/test_builders.py",
    "services/v2-to-fhir/v2_to_fhir/__pycache__/app.cpython-313.pyc",
    "libs/pulsebridge_hl7v2/pulsebridge_hl7v2.egg-info/PKG-INFO",
    "libs/pulsebridge_hl7v2/build/lib/x.py",
    "libs/pulsebridge_fhir/examples/heart-rate-observation.json",  # not package data
    # Common secret names, planted beside the package (outside the allow-list) ...
    "services/v2-to-fhir/my-project-0123456789ab.json",  # a service account key's console name
    "services/health-sync/token.json",
    "services/cuff-sim/secrets/client-secret",
    "libs/pulsebridge_gcp/.envrc",
    # ... and inside an allowed package directory, where only the deny-list stops them.
    "services/v2-to-fhir/v2_to_fhir/my-project-0123456789ab.json",
    "services/health-sync/health_sync/token.json",
    "services/health-sync/health_sync/token-cache.json",
    "services/cuff-sim/cuff_sim/secrets/client-secret",
    "services/cuff-sim/cuff_sim/.envrc",
    "services/cuff-sim/cuff_sim/local.env",
    "libs/pulsebridge_gcp/pulsebridge_gcp/.netrc",
    "libs/pulsebridge_gcp/pulsebridge_gcp/.pypirc",
    "libs/pulsebridge_gcp/pulsebridge_gcp/id_rsa",
    "libs/pulsebridge_gcp/pulsebridge_gcp/id_ed25519.pub",
    "libs/pulsebridge_fhir/pulsebridge_fhir/ca.crt",
    "libs/pulsebridge_fhir/pulsebridge_fhir/terraform.tfvars.json",
    "libs/pulsebridge_fhir/pulsebridge_fhir/.git/config",
    "libs/pulsebridge_hl7v2/samples/.env",
    "services/v2-to-fhir/v2_to_fhir/sa-key.json",
    "services/v2-to-fhir/v2_to_fhir/__pycache__/app.cpython-313.pyc",
]

MUST_BE_INCLUDED = [
    "libs/pulsebridge_fhir/pyproject.toml",
    "libs/pulsebridge_fhir/README.md",
    "libs/pulsebridge_fhir/pulsebridge_fhir/__init__.py",
    "libs/pulsebridge_hl7v2/samples/samples.json",
    "libs/pulsebridge_hl7v2/samples/__init__.py",
    "libs/pulsebridge_gcp/pulsebridge_gcp/transport.py",
    "services/v2-to-fhir/pyproject.toml",
    "services/v2-to-fhir/README.md",
    "services/v2-to-fhir/v2_to_fhir/app.py",
    "services/cuff-sim/cuff_sim/app.py",
    "services/health-sync/health_sync/app.py",
    "services/seed/pulsebridge_seed/cli.py",
    "services/seed/README.md",
]


@pytest.mark.parametrize("path", MUST_BE_EXCLUDED)
def test_dockerignore_excludes(path: str) -> None:
    assert is_excluded(path, dockerignore_patterns()), f"{path} would be sent to the build"


@pytest.mark.parametrize("path", MUST_BE_INCLUDED)
def test_dockerignore_keeps_what_the_images_install(path: str) -> None:
    assert not is_excluded(path, dockerignore_patterns()), f"{path} is needed by an image build"


def build_inputs(source: str) -> set[str]:
    """What `pip install ./<source>` reads: pyproject.toml, the metadata readme and the package
    directories the pyproject.toml names (hatchling `packages`, or setuptools `packages`
    mapped through `package-dir`)."""
    data = pyproject(REPO_ROOT / source)
    inputs = {f"{source}/pyproject.toml"}
    readme = data["project"].get("readme")
    if readme:
        inputs.add(f"{source}/{readme}")
    tool = data.get("tool", {})
    hatch = tool.get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    for package in hatch.get("packages", []):
        inputs.add(f"{source}/{package}")
    setuptools = tool.get("setuptools", {})
    package_dir = setuptools.get("package-dir", {})
    for package in setuptools.get("packages", []):
        inputs.add(f"{source}/{package_dir.get(package, package.replace('.', '/'))}")
    assert len(inputs) >= 3, f"{source}: no package directories found in pyproject.toml"
    return inputs


def test_dockerignore_allows_exactly_what_the_images_install() -> None:
    # The allow-list (the `!` lines) is exactly the build inputs of every COPY source: nothing a
    # build needs is missing, and nothing else is sent. A new service or library adds its lines.
    expected: set[str] = set()
    for d in dockerfile_dirs():
        for source in copy_sources(read_dockerfile(d / "Dockerfile")):
            expected |= build_inputs(source)
    allowed = {p.removeprefix("!").rstrip("/") for p in dockerignore_patterns() if p[0] == "!"}
    assert allowed == expected, (
        f"missing from .dockerignore: {sorted(expected - allowed)}; "
        f"not needed by any image: {sorted(allowed - expected)}"
    )
    assert dockerignore_patterns()[0] == "*", "everything is excluded first"


def test_dockerignore_sends_every_file_of_the_allowed_packages() -> None:
    # Every tracked-looking file in an allowed package directory reaches the build (so a package
    # module or package data file, such as the HL7v2 samples, cannot be dropped by a deny rule).
    patterns = dockerignore_patterns()
    for pattern in patterns:
        if not pattern.startswith("!"):
            continue
        root = REPO_ROOT / pattern.removeprefix("!").rstrip("/")
        files = [root] if root.is_file() else [f for f in root.rglob("*") if f.is_file()]
        for f in files:
            rel = f.relative_to(REPO_ROOT).as_posix()
            if "__pycache__" in rel:
                continue
            assert not is_excluded(rel, patterns), f"{rel} is needed by an image build"


def test_dockerignore_matcher_reads_patterns_like_docker() -> None:
    # The matcher is itself checked, so a bug in it cannot make the tests above pass vacuously.
    assert is_excluded("a/b", ["*"])
    assert not is_excluded("libs/x", ["*", "!libs/"])
    assert is_excluded("libs/x/tests/t.py", ["*", "!libs/", "**/tests"])
    assert not is_excluded("libs/x/tests_util.py", ["*", "!libs/", "**/tests"])
    assert is_excluded("a/b/c.pem", ["**/*.pem"])
    assert not is_excluded("a/b/c.pem.txt", ["**/*.pem"])
    assert is_excluded("x.pyc", ["**/*.py[cod]"])
