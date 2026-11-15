"""Behaviour of `make tf-lint` and `make tf-scan` (T26), which terraform-checks.yml runs on every
pull request. A lint or scan that cannot fail would let CI pass anything, so each target runs
against scratch Terraform: clean code must pass, a real mistake must fail for the right reason.

The first run downloads tflint, its Google ruleset and Trivy (pinned and checksummed) into
.venv/tools; later runs reuse them. Needs the network once, and Linux x86_64.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    (platform.system(), platform.machine()) != ("Linux", "x86_64"),
    reason="the Makefile pins Linux x86_64 binaries of tflint and Trivy",
)

PROVIDERS = """\
terraform {
  required_version = ">= 1.11"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.11"
    }
  }
}
"""

FIREWALL = """\
resource "google_compute_firewall" "ssh" {
  name          = "example-ssh"
  network       = "example-vpc"
  source_ranges = [%s]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}
"""


def run_make(target: str, tf_root: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}
    return subprocess.run(
        ["make", "--no-print-directory", "-C", str(REPO_ROOT), target, f"TF_ROOT={tf_root}"],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def module(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.tf").write_text(body)
    return root


def test_tf_lint_passes_clean_terraform(tmp_path: Path) -> None:
    root = module(tmp_path / "ok", PROVIDERS + FIREWALL % '"10.0.0.0/24"')
    result = run_make("tf-lint", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert root.exists()


def test_tf_lint_fails_without_a_required_version(tmp_path: Path) -> None:
    module(tmp_path / "bad", FIREWALL % '"10.0.0.0/24"')
    result = run_make("tf-lint", tmp_path)
    assert result.returncode != 0
    assert "terraform_required_version" in result.stdout


def test_tf_lint_uses_the_google_ruleset(tmp_path: Path) -> None:
    # Only the Google ruleset knows machine types; terraform validate accepts any string.
    module(
        tmp_path / "bad",
        PROVIDERS
        + """
resource "google_compute_instance" "vm" {
  name         = "example-vm"
  machine_type = "e2-nonexistent"
  zone         = "us-central1-a"
  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
    }
  }
  network_interface {
    network = "example-vpc"
  }
}
""",
    )
    result = run_make("tf-lint", tmp_path)
    assert result.returncode != 0
    assert "google_compute_instance_invalid_machine_type" in result.stdout


def test_tf_lint_checks_subdirectories(tmp_path: Path) -> None:
    module(tmp_path / "ok", PROVIDERS)
    module(tmp_path / "nested" / "deeper", 'variable "x" {}\n')  # untyped, no description
    result = run_make("tf-lint", tmp_path)
    assert result.returncode != 0
    assert "deeper" in result.stdout


def test_tf_scan_passes_a_restricted_rule(tmp_path: Path) -> None:
    module(tmp_path / "ok", PROVIDERS + FIREWALL % '"10.0.0.0/24"')
    result = run_make("tf-scan", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_tf_scan_fails_on_ssh_open_to_the_internet(tmp_path: Path) -> None:
    module(tmp_path / "bad", PROVIDERS + FIREWALL % '"0.0.0.0/0"')
    result = run_make("tf-scan", tmp_path)
    assert result.returncode != 0
    assert "GCP-0027" in result.stdout  # "Firewall rule allows unrestricted ingress"
