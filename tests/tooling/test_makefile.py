"""Behaviour of the Makefile targets that CI relies on.

CI is only useful if `make lint`, `make test` and `make tf-validate` exit non-zero when something
is wrong and zero when nothing is (including "nothing written yet"). These tests run the real
targets against scratch directories through the PY_PATHS, TEST_PATHS and TF_ROOT overrides, so
they exercise the same recipe lines CI runs.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

needs_terraform = pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform is not installed"
)


def run_make(target: str, **overrides: str | Path) -> subprocess.CompletedProcess[str]:
    """Run `make <target>` in the repository root with variable overrides."""
    # Drop variables inherited from an outer `make test`, so the outer run's settings cannot leak
    # into the inner one.
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}
    args = ["make", "--no-print-directory", "-C", str(REPO_ROOT), target]
    args += [f"{name}={value}" for name, value in overrides.items()]
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=300)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- make test ---------------------------------------------------------------------------------


def test_make_test_passes_when_no_tests_exist(tmp_path: Path) -> None:
    # A fresh clone has empty libs/ and services/: pytest exits 5 ("no tests collected"),
    # which the target must treat as a pass.
    result = run_make("test", TEST_PATHS=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no tests collected" in result.stdout


def test_make_test_passes_when_tests_pass(tmp_path: Path) -> None:
    write(tmp_path / "test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n")
    result = run_make("test", TEST_PATHS=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_make_test_fails_when_a_test_fails(tmp_path: Path) -> None:
    write(tmp_path / "test_ok.py", "def test_ok():\n    assert True\n")
    write(tmp_path / "test_broken.py", "def test_broken():\n    assert 1 + 1 == 3\n")
    result = run_make("test", TEST_PATHS=tmp_path)
    assert result.returncode != 0
    assert "1 failed" in result.stdout


def test_make_test_fails_on_a_collection_error(tmp_path: Path) -> None:
    # An import error is not "no tests": it must fail, not be mistaken for exit code 5.
    write(tmp_path / "test_import.py", "import module_that_does_not_exist\n")
    result = run_make("test", TEST_PATHS=tmp_path)
    assert result.returncode != 0
    assert "no tests collected; treating as a pass" not in result.stdout


# --- make lint ---------------------------------------------------------------------------------


def test_make_lint_passes_on_clean_code(tmp_path: Path) -> None:
    write(tmp_path / "clean.py", 'GREETING = "hello"\n')
    result = run_make("lint", PY_PATHS=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_make_lint_fails_on_a_lint_error(tmp_path: Path) -> None:
    write(tmp_path / "unused.py", "import os\n")  # F401: imported but unused
    result = run_make("lint", PY_PATHS=tmp_path)
    assert result.returncode != 0
    assert "F401" in result.stdout


def test_make_lint_fails_on_unformatted_code(tmp_path: Path) -> None:
    write(tmp_path / "ugly.py", "x = {  'a':1 }\n")  # lints clean, but not ruff-formatted
    result = run_make("lint", PY_PATHS=tmp_path)
    assert result.returncode != 0
    assert "would be reformatted" in result.stdout


# --- make tf-validate --------------------------------------------------------------------------

VALID_TF = """\
variable "name" {
  description = "A name."
  type        = string
}

output "greeting" {
  value = "hello ${var.name}"
}
"""

# Correctly formatted, but refers to a variable that is never declared.
INVALID_TF = """\
output "greeting" {
  value = var.undeclared
}
"""


def test_tf_validate_passes_when_there_is_no_terraform(tmp_path: Path) -> None:
    for root in (tmp_path / "missing", tmp_path):  # directory absent, and present but empty
        result = run_make("tf-validate", TF_ROOT=root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "nothing to validate" in result.stdout


@needs_terraform
def test_tf_validate_passes_on_valid_terraform(tmp_path: Path) -> None:
    write(tmp_path / "modules" / "ok" / "main.tf", VALID_TF)
    result = run_make("tf-validate", TF_ROOT=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Success! The configuration is valid." in result.stdout


@needs_terraform
def test_tf_validate_checks_every_directory(tmp_path: Path) -> None:
    # The valid directory sorts first; the invalid one must still be reached and fail the run.
    write(tmp_path / "a-valid" / "main.tf", VALID_TF)
    write(tmp_path / "b-invalid" / "main.tf", INVALID_TF)
    result = run_make("tf-validate", TF_ROOT=tmp_path)
    assert result.returncode != 0
    assert "undeclared" in result.stderr


@needs_terraform
def test_tf_validate_fails_on_unformatted_terraform(tmp_path: Path) -> None:
    write(tmp_path / "mod" / "main.tf", 'output "x" {\nvalue="y"\n}\n')
    result = run_make("tf-validate", TF_ROOT=tmp_path)
    assert result.returncode != 0
    assert "main.tf" in result.stdout  # terraform fmt -check lists the file it would change
