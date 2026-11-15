"""The hashed lock file, the pins it must agree with, connect.sh's install flags, and the
fingerprint that decides when connect.sh reinstalls."""

import importlib.util
import re
import shutil
import tomllib
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1]
REPO = TOOL.parents[1]
LOCK = TOOL / "requirements.lock"
REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


def _lock_entries() -> dict[str, list[str]]:
    """name -> the lines of its entry (requirement line and continuation lines)."""
    entries: dict[str, list[str]] = {}
    current = None
    for line in LOCK.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = REQUIREMENT.match(line)
        if match:
            current = match.group(1).lower()
            assert current not in entries, f"{current} appears twice"
            entries[current] = [line]
        else:
            assert current is not None and line.startswith("    "), f"stray line: {line!r}"
            entries[current].append(line)
    return entries


def _pins(requirements: list[str]) -> dict[str, str]:
    pins = {}
    for requirement in requirements:
        match = REQUIREMENT.match(requirement)
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def test_every_requirement_is_pinned_exactly_and_hashed():
    entries = _lock_entries()
    assert len(entries) >= 20
    for name, lines in entries.items():
        hashes = [ln for ln in lines if re.search(r"--hash=sha256:[0-9a-f]{64}\b", ln)]
        assert hashes, f"{name} has no sha256 hash"
        assert "==" in lines[0], f"{name} is not pinned exactly"


def test_the_direct_pins_equal_pyproject_and_the_build_backend():
    locked = {name: REQUIREMENT.match(lines[0]).group(2) for name, lines in _lock_entries().items()}
    project = tomllib.loads((TOOL / "pyproject.toml").read_text())
    direct = _pins(project["project"]["dependencies"])
    assert direct == {
        "google-cloud-secret-manager": locked["google-cloud-secret-manager"],
        "google-auth": locked["google-auth"],
        "requests": locked["requests"],
    }
    # The build backend of this tool and of both libraries is the locked one.
    for pyproject in (
        TOOL / "pyproject.toml",
        REPO / "libs/pulsebridge_fhir/pyproject.toml",
        REPO / "libs/pulsebridge_gcp/pyproject.toml",
    ):
        backend = _pins(tomllib.loads(pyproject.read_text())["build-system"]["requires"])
        assert backend == {"hatchling": locked["hatchling"]}, pyproject
    # requirements.in, the lock's input, names the same pins.
    inputs = [ln for ln in (TOOL / "requirements.in").read_text().splitlines() if "==" in ln]
    assert _pins(inputs) == {**direct, "hatchling": locked["hatchling"]}


def test_connect_sh_installs_only_from_the_hashed_lock():
    script = (TOOL / "connect.sh").read_text()
    installs = [
        ln for ln in script.splitlines() if "pip install" in ln or ln.strip().startswith("--")
    ]
    joined = " ".join(installs)
    assert "--require-hashes" in joined and "--only-binary :all:" in joined
    assert '-r "$here/requirements.lock"' in joined
    assert joined.count("--no-deps") == 2
    assert "--no-build-isolation" in joined
    assert "--upgrade" not in script  # no unhashed pip upgrade


def _load_fingerprint():
    spec = importlib.util.spec_from_file_location("fingerprint", TOOL / "fingerprint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fingerprint


def _checkout(root: Path) -> Path:
    for package in (
        "scripts/connect-google-health",
        "libs/pulsebridge_fhir",
        "libs/pulsebridge_gcp",
    ):
        shutil.copytree(
            REPO / package,
            root / package,
            ignore=shutil.ignore_patterns("__pycache__", "tests"),
        )
    return root


PACKAGES = ["scripts/connect-google-health", "libs/pulsebridge_fhir", "libs/pulsebridge_gcp"]


def test_fingerprint_changes_with_a_file_even_in_a_checkout_under_a_tests_directory(tmp_path):
    fingerprint = _load_fingerprint()
    repo = _checkout(tmp_path / "tests" / "checkout")
    before = fingerprint(repo, PACKAGES)
    lock = repo / "scripts/connect-google-health/requirements.lock"
    lock.write_text(lock.read_text() + "\n")
    after_lock = fingerprint(repo, PACKAGES)
    module = repo / "libs/pulsebridge_gcp/pulsebridge_gcp/client.py"
    module.write_text(module.read_text() + "\n")
    after_module = fingerprint(repo, PACKAGES)
    assert len({before, after_lock, after_module}) == 3


def test_fingerprint_ignores_tests_inside_the_packages(tmp_path):
    fingerprint = _load_fingerprint()
    repo = _checkout(tmp_path / "checkout")
    before = fingerprint(repo, PACKAGES)
    (repo / "libs/pulsebridge_fhir/tests").mkdir()
    (repo / "libs/pulsebridge_fhir/tests/test_x.py").write_text("x = 1\n")
    assert fingerprint(repo, PACKAGES) == before
