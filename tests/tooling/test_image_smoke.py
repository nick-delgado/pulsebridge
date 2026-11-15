"""Smoke test of the built service images: `make images && make images-smoke`.

Skipped unless PULSEBRIDGE_IMAGE_SMOKE=1 (`make images-smoke` sets it), because it needs a Docker
daemon and images built by `make images`; `make test` and CI run only the static checks in
test_container_images.py and test_cloudbuild.py.

For each image it checks, against the real container:

- it runs as a non-root UID and contains no source tree, tests, git metadata or pip;
- with harmless fake configuration it starts, listens on the PORT it is given (not 8080), and
  answers its own route the way its README says, without calling any Google API;
- with bad configuration it refuses to start and says why (v2-to-fhir: "a bad store ID stops the
  service starting"), so a bad deploy fails at once instead of failing every request.

A job image (seed, a Cloud Run job: JOB_CASES) serves nothing, so instead: `--help` exits 0; a
dry run (`--dry-run --days 1`, synthetic PATIENT_ID and zone) prints the counts report, exits
0, makes no network call (an audit hook records every socket operation) and leaves no file
behind; without arguments it fails; with bad configuration it exits 2 and says why.

All configuration is synthetic. The containers get no credentials: Application Default
Credentials come from a throwaway authorized_user file with placeholder values that no request
ever uses (the services build their client at start-up and would refuse to start without one),
and the metadata server address points at a closed port.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PULSEBRIDGE_IMAGE_SMOKE") != "1",
    reason="image smoke test: run `make images && make images-smoke` (needs Docker)",
)

DOCKER = os.environ.get("DOCKER", "docker")
# The CA bundle given to `make images` as the pip_cert build secret, if any.
PIP_CERT = os.environ.get("IMAGE_PIP_CERT", "")
PREFIX = os.environ.get("IMAGE_PREFIX", "pulsebridge")
TAG = os.environ.get("IMAGE_TAG", "local")
SMOKE_PORT = "9123"  # deliberately not the 8080 default: proves PORT is honoured
START_TIMEOUT = 30.0

# Placeholder Application Default Credentials (google-auth's authorized_user format). Loading
# them makes no network call; no test request reaches a Google API, so they are never used.
FAKE_ADC = {
    "type": "authorized_user",
    "client_id": "smoke-test-placeholder",
    "client_secret": "smoke-test-placeholder",
    "refresh_token": "smoke-test-placeholder",
}

COMMON = {
    "GCP_PROJECT_ID": "smoke-project",
    "HEALTHCARE_LOCATION": "us-central1",
    "HEALTHCARE_DATASET_ID": "smoke-dataset",
    "PATIENT_ID": "smoke-patient",
}


# Run inside the image (`python -c <script> <size> <sha256>`): print every regular file with
# exactly that size and SHA-256, i.e. a copy of the given file anywhere in the image.
SAME_FILE_SCAN = """
import hashlib, os, sys

size, digest = int(sys.argv[1]), sys.argv[2]
for root, dirs, files in os.walk("/"):
    if root.startswith(("/proc", "/sys", "/dev")):
        dirs[:] = []
        continue
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) != size:
            continue
        try:
            with open(path, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() == digest:
                    print(path)
        except OSError:
            print("unreadable: " + path)  # never skipped: it could be the copy
"""

# Run inside the image (`python -c`): print every path that should not be in a service image.
# The kernel's pseudo filesystems are skipped.
STRAY_FILE_SCAN = """
import os

STRAY_NAMES = {".git", ".env", ".venv", "node_modules", "pip"}
STRAY_SUFFIXES = (".pem", ".key", ".crt", ".p12", ".tfstate", ".tfvars", ".tfplan")


def is_credential_json(name):
    return name.endswith(".json") and (
        "credentials" in name or "sa-key" in name or name.startswith("client_secret")
    )


for root, dirs, files in os.walk("/"):
    if root.startswith(("/proc", "/sys", "/dev")):
        dirs[:] = []
        continue
    for name in dirs + files:
        path = os.path.join(root, name)
        if name in STRAY_NAMES or name.endswith(STRAY_SUFFIXES) or is_credential_json(name):
            print(path)
        elif name == "tests" and "/site-packages/" not in path:
            print(path)
        elif root == "/" and name in ("services", "libs", "src"):
            print(path)
"""


# Run inside a service image (`python -c <script> <port> <command...>`): start the image's own
# command as a child and try to connect to 127.0.0.1:<port> every 10 ms, from start until the
# command exits (or, once the port has opened, stop it). Prints when the port first accepted a
# connection (seconds, or null) and the command's exit status. Probing inside the container
# matters: a port published to the host is accepted by Docker's proxy whether or not anything
# listens in the container.
LISTEN_PROBE = """
import json, socket, subprocess, sys, time

port, command = int(sys.argv[1]), sys.argv[2:]
child = subprocess.Popen(command)
start = time.monotonic()
opened = None
while child.poll() is None and time.monotonic() - start < 25:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.05).close()
        opened = round(time.monotonic() - start, 3)
        break
    except OSError:
        time.sleep(0.01)
if child.poll() is None:
    child.terminate()
try:
    status = child.wait(timeout=10)
except subprocess.TimeoutExpired:
    child.kill()
    status = child.wait()
print("LISTEN-PROBE " + json.dumps({"opened_at": opened, "exit": status}))
"""


@dataclass(frozen=True)
class JobCase:
    """A Cloud Run job image: its arguments come from the job, so each run passes its own."""

    service: str
    program: str  # the image's ENTRYPOINT, the README's console script
    env: dict[str, str]
    dry_run_args: list[str]
    bad_env: dict[str, str]
    bad_message: str


JOB_CASES = {
    # A dry run needs only PATIENT_ID (synthetic: SYN-) and TIME_ZONE, and makes no request
    # (services/seed/README.md, "Running it").
    "seed": JobCase(
        service="seed",
        program="pulsebridge-seed",
        env={"PATIENT_ID": "SYN-SMOKE-0001", "TIME_ZONE": "America/New_York"},
        dry_run_args=["--dry-run", "--days", "1"],
        bad_env={"PATIENT_ID": "smoke-patient"},
        bad_message="PATIENT_ID must start with SYN-",
    ),
}

# Run inside a job image (`python -c <script> <args...>`): run the image's console script (the
# same entry point the ENTRYPOINT runs) with an audit hook that records every socket operation
# (CPython audit events "socket.*": getaddrinfo, connect, ...), even one whose error the code
# swallows. Prints the events as the last stderr line and exits with the command's code.
NETWORK_AUDIT = """
import json, sys
from importlib.metadata import entry_points

events = []
sys.addaudithook(lambda event, args: events.append(event) if event.startswith("socket.") else None)
program = sys.argv[1]
sys.argv = [program, *sys.argv[2:]]
if program == "--self-check":
    import socket
    try:
        socket.getaddrinfo("smoke.invalid", 443)
    except OSError:
        pass
    code = 0
else:
    (script,) = entry_points(group="console_scripts", name=program)
    try:
        code = script.load()()
    except SystemExit as exc:
        code = exc.code
print("SOCKET-EVENTS " + json.dumps(sorted(set(events))), file=sys.stderr)
sys.exit(code)
"""


@dataclass(frozen=True)
class Case:
    service: str
    env: dict[str, str]
    method: str
    path: str
    body: bytes | None
    status: int
    bad_env: dict[str, str] = field(default_factory=dict)
    bad_message: str = ""


CASES = {
    # No health route: an empty push envelope is answered 400 ("rejected-envelope") without any
    # network call. An unknown route would be 404, a wrong method 405.
    "v2-to-fhir": Case(
        service="v2-to-fhir",
        env={**COMMON, "FHIR_STORE_ID": "smoke-fhir", "HL7V2_STORE_ID": "smoke-hl7v2"},
        method="POST",
        path="/pubsub/hl7v2",
        body=b"{}",
        status=400,
        bad_env={"FHIR_STORE_ID": "bad store"},
        bad_message="configuration does not form valid resource names",
    ),
    # /status reports `not-configured` when the Google Health secret files are not mounted, which
    # it decides without a token request (README, "GET /status").
    "health-sync": Case(
        service="health-sync",
        env={
            **COMMON,
            "FHIR_STORE_ID": "smoke-fhir",
            "TIME_ZONE": "America/New_York",
            "GOOGLE_HEALTH_CLIENT_ID_FILE": "/secrets/client-id/value",
            "GOOGLE_HEALTH_CLIENT_SECRET_FILE": "/secrets/client-secret/value",
            "GOOGLE_HEALTH_REFRESH_TOKEN_FILE": "/secrets/refresh-token/value",
        },
        method="GET",
        path="/status",
        body=None,
        status=200,
        bad_env={"TIME_ZONE": "UTC-5"},
        bad_message="TIME_ZONE must be an IANA zone name",
    ),
    # /cuff/samples lists T07's malformed samples: it proves the package data was installed.
    "cuff-sim": Case(
        service="cuff-sim",
        env={
            **COMMON,
            "HL7V2_STORE_ID": "smoke-hl7v2",
            "AUDIT_FHIR_STORE_ID": "smoke-audit",
            "MLLP_HOST": "10.0.0.2",
            "PATIENT_ASSIGNING_AUTHORITY": "SMOKE",
            "PATIENT_FAMILY_NAME": "Smoke",
            "PATIENT_GIVEN_NAME": "Test",
        },
        method="GET",
        path="/cuff/samples",
        body=None,
        status=200,
        bad_env={"AUDIT_FHIR_STORE_ID": "smoke-hl7v2"},
        bad_message="AUDIT_FHIR_STORE_ID must differ from HL7V2_STORE_ID",
    ),
}


def docker(*args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [DOCKER, *args], capture_output=True, text=True, timeout=timeout, check=check
    )


def image(service: str) -> str:
    return f"{PREFIX}/{service}:{TAG}"


def run_in(service: str, program: str, *args: str, user: str | None = None, **kw):
    """Run `program` in the image instead of its command. --entrypoint replaces both the
    ENTRYPOINT (a job image's) and the CMD (a service image's), so this works for either."""
    user_args = ["--user", user] if user is not None else []
    return docker("run", "--rm", *user_args, "--entrypoint", program, image(service), *args, **kw)


ALL_IMAGES = sorted([*CASES, *JOB_CASES])


@pytest.fixture(scope="module")
def adc_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    assert shutil.which(DOCKER), f"{DOCKER} not found"
    d = tmp_path_factory.mktemp("adc")
    (d / "adc.json").write_text(json.dumps(FAKE_ADC))
    d.chmod(0o755)
    (d / "adc.json").chmod(0o644)  # readable by the image's non-root user
    return d


def run_args(adc_dir: Path, env: dict[str, str]) -> list[str]:
    args = ["--volume", f"{adc_dir}:/smoke:ro"]
    full = {
        **env,
        "GOOGLE_APPLICATION_CREDENTIALS": "/smoke/adc.json",
        "GCE_METADATA_HOST": "127.0.0.1:9",
        "GCE_METADATA_IP": "127.0.0.1:9",
    }
    for name, value in full.items():
        args += ["--env", f"{name}={value}"]
    return args


@pytest.fixture(params=list(CASES))
def case(request: pytest.FixtureRequest) -> Case:
    """A service image (gunicorn)."""
    return CASES[request.param]


@pytest.fixture(params=list(JOB_CASES))
def job(request: pytest.FixtureRequest) -> JobCase:
    return JOB_CASES[request.param]


@pytest.fixture(params=ALL_IMAGES)
def any_image(request: pytest.FixtureRequest) -> str:
    """Every image, service or job, for the checks that apply to both."""
    return request.param


@pytest.fixture
def running(case: Case, adc_dir: Path) -> Iterator[tuple[str, str]]:
    """Start the image with good fake configuration; yield (container id, host base URL)."""
    started = docker(
        "run",
        "--detach",
        "--publish",
        f"127.0.0.1::{SMOKE_PORT}",
        "--env",
        f"PORT={SMOKE_PORT}",
        *run_args(adc_dir, case.env),
        image(case.service),
    )
    container = started.stdout.strip()
    try:
        port = docker("port", container, f"{SMOKE_PORT}/tcp").stdout.split(":")[-1].strip()
        yield container, f"http://127.0.0.1:{port}"
    finally:
        docker("rm", "--force", container, check=False)


def request(base: str, case: Case) -> tuple[int, bytes]:
    url = base + case.path
    if not url.startswith("http://127.0.0.1:"):  # only the published port on this machine
        raise ValueError(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never a proxy
    req = urllib.request.Request(  # noqa: S310 - the URL is checked above
        url,
        data=case.body,
        method=case.method,
        headers={"Content-Type": "application/json"} if case.body is not None else {},
    )
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_for_answer(container: str, base: str, case: Case) -> tuple[int, bytes]:
    deadline = time.monotonic() + START_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        state = docker("inspect", "--format", "{{.State.Running}}", container).stdout.strip()
        if state != "true":
            logs = docker("logs", container, check=False)
            pytest.fail(f"{case.service} exited during start-up:\n{logs.stdout}{logs.stderr}")
        try:
            return request(base, case)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = exc
            time.sleep(0.5)
    pytest.fail(f"{case.service} did not answer on PORT {SMOKE_PORT}: {last_error}")


def test_starts_listens_on_port_and_answers(case: Case, running: tuple[str, str]) -> None:
    container, base = running
    status, body = wait_for_answer(container, base, case)
    assert status == case.status, body
    if case.service == "health-sync":
        assert json.loads(body)["googleHealth"] == "not-configured"
    if case.service == "cuff-sim":
        samples = json.loads(body)
        assert samples and all({"name", "label", "class", "transports"} <= set(s) for s in samples)
    logs = docker("logs", container)
    output = logs.stdout + logs.stderr
    assert f"Listening at: http://0.0.0.0:{SMOKE_PORT}" in output, output
    assert "Control server error" not in output, output


def test_bad_configuration_stops_start_up(case: Case, adc_dir: Path) -> None:
    # A service that wrongly starts would run until removed, so the container is named and
    # always removed, and "still running after the timeout" is reported as the failure.
    name = f"pulsebridge-smoke-bad-{case.service}-{os.getpid()}"
    try:
        result = docker(
            "run",
            "--name",
            name,
            *run_args(adc_dir, {**case.env, **case.bad_env}),
            image(case.service),
            check=False,
            timeout=START_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case.service} kept running with bad configuration {case.bad_env}")
    finally:
        docker("rm", "--force", name, check=False)
    assert result.returncode != 0, "the service started with bad configuration"
    assert case.bad_message in result.stdout + result.stderr


def test_runs_as_non_root(any_image: str) -> None:
    uid = run_in(any_image, "id", "-u").stdout.strip()
    assert uid == "10001", uid
    user = docker("image", "inspect", "--format", "{{.Config.User}}", image(any_image))
    assert user.stdout.strip() == "10001:10001"


def test_image_holds_no_stray_files(any_image: str) -> None:
    result = run_in(any_image, "python", "-c", STRAY_FILE_SCAN)
    stray = [line for line in result.stdout.splitlines() if line]
    # Paths that belong to Debian or Python and are not ours: the system CA bundle, Debian's
    # own Python files, CPython's test package and certifi's public CA list.
    allowed = (
        "/etc/ssl/",
        "/usr/lib/ssl/",
        "/usr/share/",
        "/usr/lib/python3/",
        "/usr/local/lib/python3.13/test",
        "/opt/venv/lib/python3.13/site-packages/certifi/cacert.pem",
    )
    stray = [p for p in stray if not p.startswith(allowed)]
    assert not stray, stray
    pip = run_in(any_image, "python", "-m", "pip", "--version", check=False)
    assert pip.returncode != 0, "pip is removed from the runtime image"


def test_package_data_and_entry_points_are_installed(case: Case) -> None:
    if case.service == "cuff-sim":
        code = (
            "from pulsebridge_hl7v2.samples import all_samples; s = all_samples(); "
            "assert s, 'no samples'; print(len(s))"
        )
        result = docker("run", "--rm", image(case.service), "python", "-c", code, check=False)
        assert result.returncode == 0, result.stderr
        assert int(result.stdout.strip()) > 0
    if case.service == "health-sync":
        result = docker(
            "run", "--rm", image(case.service), "health-sync-backfill", "--help", check=False
        )
        assert result.returncode == 0, result.stderr
    if case.service == "v2-to-fhir":
        code = "import v2_to_fhir.app, pulsebridge_fhir, pulsebridge_gcp, pulsebridge_hl7v2"
        result = docker("run", "--rm", image(case.service), "python", "-c", code, check=False)
        assert result.returncode == 0, result.stderr


def test_every_image_has_a_smoke_case() -> None:
    root = Path(__file__).resolve().parents[2]
    services = sorted(p.parent.name for p in (root / "services").glob("*/Dockerfile"))
    assert services == ALL_IMAGES, "add a Case (service) or a JobCase (job) for each new image"
    assert not set(CASES) & set(JOB_CASES), "an image is a service or a job, not both"


def test_code_is_owned_by_root_and_read_only_to_the_app_user(any_image: str) -> None:
    # Runs as the image's own user: nothing under /opt/venv may be owned by anyone but root or
    # be writable by that user, so the running service cannot change its own code.
    result = docker(
        "run",
        "--rm",
        "--entrypoint",
        "find",
        image(any_image),
        "/opt/venv",
        "(",
        "!",
        "-user",
        "0",
        "-o",
        "!",
        "-group",
        "0",
        "-o",
        "-writable",
        ")",
        "-print",
    )
    assert result.stdout.strip() == "", result.stdout[:2000]


@pytest.mark.skipif(not PIP_CERT, reason="IMAGE_PIP_CERT not set: the images used no build secret")
def test_the_pip_build_secret_is_not_in_the_image(any_image: str) -> None:
    # The CA bundle reaches the build only as a secret mount; no copy of it may be in a layer.
    # The scan runs as root: a copy made with `cp` from the mount is root-only (mode 0400) and
    # would be invisible to the image's own user.
    data = Path(PIP_CERT).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    result = run_in(any_image, "python", "-c", SAME_FILE_SCAN, str(len(data)), digest, user="0")
    assert result.stdout.strip() == "", f"copies of {PIP_CERT} in the image: {result.stdout}"


def test_effective_gunicorn_settings(case: Case) -> None:
    # gunicorn's own view of the settings, from the image's CMD and environment: the README's
    # arguments, PORT's default bind, and the two GUNICORN_CMD_ARGS settings.
    cmd = json.loads(
        docker("image", "inspect", "--format", "{{json .Config.Cmd}}", image(case.service)).stdout
    )
    result = docker(
        "run", "--rm", image(case.service), cmd[0], "--print-config", *cmd[1:], check=False
    )
    settings = {}
    for line in result.stdout.splitlines():
        name, sep, value = line.partition("=")
        if sep:
            settings[name.strip()] = value.strip()
    assert settings.get("bind") == "['0.0.0.0:8080']", result.stdout[-2000:]
    assert settings.get("workers") == "1"
    assert settings.get("threads") == "4"
    assert settings.get("timeout") == "0"
    assert settings.get("graceful_timeout") == "8", "Cloud Run kills 10 s after SIGTERM"
    assert settings.get("control_socket_disable") == "True"
    assert settings.get("preload_app") == "True", "the app loads before the port opens"


# --- job images -------------------------------------------------------------------------------


def job_env(env: dict[str, str]) -> list[str]:
    """Only the job's own synthetic variables: no credentials, nothing from this session."""
    args: list[str] = []
    for name, value in env.items():
        args += ["--env", f"{name}={value}"]
    return args


def test_job_entrypoint_is_the_command_and_there_is_no_cmd(job: JobCase) -> None:
    config = json.loads(
        docker("image", "inspect", "--format", "{{json .Config}}", image(job.service)).stdout
    )
    assert config["Entrypoint"] == [job.program]
    # The base image's CMD ("python3") is cleared by ENTRYPOINT: the arguments are the job's.
    assert not config.get("Cmd"), config.get("Cmd")
    assert not config.get("ExposedPorts"), "a job listens on nothing"
    assert not [e for e in config.get("Env") or [] if e.startswith(("PORT=", "GUNICORN"))]


def test_job_help_exits_zero(job: JobCase) -> None:
    result = docker("run", "--rm", "--network", "none", image(job.service), "--help", check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"usage: {job.program}"), result.stdout[:500]


def test_job_without_arguments_fails(job: JobCase) -> None:
    # No CMD: a job created without args must fail visibly, not succeed doing nothing.
    result = docker(
        "run", "--rm", "--network", "none", *job_env(job.env), image(job.service), check=False
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "required" in result.stderr
    # ... and logs its structured seed-run line, which the job's alert reads (T35).
    assert ("WARNING", "not-started") in [(r["severity"], r["outcome"]) for r in seed_runs(result)]


def seed_runs(result: subprocess.CompletedProcess) -> list[dict]:
    lines = []
    for line in result.stderr.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("event") == "seed-run":
            lines.append(entry)
    return lines


def test_job_sigterm_logs_terminated_and_exits_promptly(job: JobCase) -> None:
    # A year-long dry run takes several seconds of generating; SIGTERM one second in must end
    # it within a few seconds (Cloud Run sends SIGKILL 10 s after SIGTERM) with a seed-run line.
    name = f"pulsebridge-smoke-term-{job.service}-{os.getpid()}"
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            *job_env(job.env),
            image(job.service),
            "--dry-run",
            "--days",
            "366",
        )
        time.sleep(1.0)
        running = docker("inspect", "--format", "{{.State.Running}}", name).stdout.strip()
        assert running == "true", "the dry run ended before SIGTERM was sent"
        sent = time.monotonic()
        docker("kill", "--signal", "TERM", name)
        code = docker("wait", name, timeout=15).stdout.strip()
        elapsed = time.monotonic() - sent
        logs = docker("logs", name, check=False)
    finally:
        docker("rm", "--force", name, check=False)
    assert code == "143", logs.stderr[-2000:]
    assert elapsed < 5, f"took {elapsed:.1f} s to stop after SIGTERM"
    runs = seed_runs(logs)
    assert [(r["severity"], r["outcome"], r["reason"]) for r in runs] == [
        ("WARNING", "terminated", "sigterm")
    ]


def test_job_dry_run_prints_the_counts_and_leaves_no_files(job: JobCase) -> None:
    name = f"pulsebridge-smoke-job-{job.service}-{os.getpid()}"
    try:
        result = docker(
            "run",
            "--name",
            name,
            "--network",
            "none",
            *job_env(job.env),
            image(job.service),
            *job.dry_run_args,
            check=False,
            timeout=START_TIMEOUT,
        )
        changed = docker("diff", name).stdout.strip()
    finally:
        docker("rm", "--force", name, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["outcome"] == "dry-run"
    assert report["windows_in_range"] == 288  # one UTC day of five-minute windows (D8)
    assert report["windows_generated"] + report["windows_absent"] == 288
    assert report["windows_generated"] > 0
    assert (report["windows_written"], report["bundles_written"]) == (0, 0)
    assert report["safety_check"] == "not-run" and report["stored_read"] == "not-attempted"
    assert job.env["PATIENT_ID"] not in result.stdout + result.stderr  # never logged
    # Nothing written in the container's filesystem (no caches, no report files, no .pyc).
    assert changed == "", changed


def network_audit(job: JobCase, *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    result = docker(
        "run",
        "--rm",
        "--network",
        "none",
        *job_env(job.env),
        "--entrypoint",
        "python",
        image(job.service),
        "-c",
        NETWORK_AUDIT,
        *args,
        check=False,
        timeout=START_TIMEOUT,
    )
    marker = [line for line in result.stderr.splitlines() if line.startswith("SOCKET-EVENTS ")]
    assert marker, result.stderr[-2000:]
    return result, json.loads(marker[-1].removeprefix("SOCKET-EVENTS "))


def test_the_network_audit_sees_a_lookup() -> None:
    # The detector itself is checked, so the next test cannot pass because it sees nothing.
    _, events = network_audit(next(iter(JOB_CASES.values())), "--self-check")
    assert "socket.getaddrinfo" in events, events


def test_job_dry_run_makes_no_network_call(job: JobCase) -> None:
    result, events = network_audit(job, job.program, *job.dry_run_args)
    assert result.returncode == 0, result.stderr[-2000:]
    assert events == [], f"the dry run used the network: {events}"


def test_job_bad_configuration_exits_2_and_says_why(job: JobCase) -> None:
    result = docker(
        "run",
        "--rm",
        "--network",
        "none",
        *job_env({**job.env, **job.bad_env}),
        image(job.service),
        *job.dry_run_args,
        check=False,
        timeout=START_TIMEOUT,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert job.bad_message in result.stderr
    assert job.bad_env["PATIENT_ID"] not in result.stderr  # the variable is named, not its value


# --- start-up failures never listen (service images) ----------------------------------------


def listen_probe(case: Case, adc_dir: Path, env: dict[str, str]) -> dict:
    cmd = json.loads(
        docker("image", "inspect", "--format", "{{json .Config.Cmd}}", image(case.service)).stdout
    )
    result = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--env",
        f"PORT={SMOKE_PORT}",
        *run_args(adc_dir, env),
        "--entrypoint",
        "python",
        image(case.service),
        "-c",
        LISTEN_PROBE,
        SMOKE_PORT,
        *cmd,
        check=False,
        timeout=60,
    )
    marker = [line for line in result.stdout.splitlines() if line.startswith("LISTEN-PROBE ")]
    assert marker, result.stdout[-2000:] + result.stderr[-2000:]
    return {**json.loads(marker[-1].removeprefix("LISTEN-PROBE ")), "output": result.stderr}


def test_good_configuration_opens_the_port(case: Case, adc_dir: Path) -> None:
    # The probe itself is checked: with good configuration it sees the port open.
    probe = listen_probe(case, adc_dir, case.env)
    assert probe["opened_at"] is not None, probe["output"][-2000:]


def test_bad_configuration_exits_without_ever_listening(case: Case, adc_dir: Path) -> None:
    # Cloud Run's default startup probe is a TCP connect to PORT. If gunicorn opened the port
    # before the app failed to load, the probe could pass and a broken revision would serve.
    probe = listen_probe(case, adc_dir, {**case.env, **case.bad_env})
    assert probe["opened_at"] is None, f"the port opened at {probe['opened_at']} s"
    assert probe["exit"] != 0, probe
    assert case.bad_message in probe["output"]
