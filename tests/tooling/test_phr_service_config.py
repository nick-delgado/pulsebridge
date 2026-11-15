"""Module phr's environment maps, parsed by the services' REAL configuration code.

`infra/modules/phr/tests/real_data.tftest.hcl` asserts that each Cloud Run service gets exactly
one environment map (`fhir_proxy_environment`, `cuff_sim_environment`). This test reads those
same maps out of the test file and feeds them to `fhir_proxy.config.ServiceConfig.from_env` and
`cuff_sim.config.ServiceConfig.from_env`. Together the two tests make a chain: the module
renders map M (terraform test), and the service starts with M (here). A renamed, misspelled or
malformed variable then fails in `make test`, not at the first deploy.

Why here and not as a `tests/*.py` beside the module: `make tf-test` runs those with the
system python3, where the services and their dependencies are not installed; the venv used by
`make test` has them. Why the maps come from the .tftest.hcl file rather than from
`terraform console`: rendering needs an initialised module and the Terraform binary in this
layer, and with no state the services' URLs are unknown; the tftest already pins the module's
output to these exact maps against a mocked provider.

`from_env` ignores variables it does not know, so a misspelled OPTIONAL variable would be
silently dropped: every key must also be one the service declares.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cuff_sim import config as cuff_config
from fhir_proxy import config as proxy_config

REPO_ROOT = Path(__file__).resolve().parents[2]
TFTEST = REPO_ROOT / "infra" / "modules" / "phr" / "tests" / "real_data.tftest.hcl"


def expected_env(run: str, resource: str) -> dict[str, str]:
    """The `{ for e in <resource>...env : e.name => e.value } == { ... }` map of one run."""
    text = TFTEST.read_text(encoding="utf-8")
    block = re.search(rf'run "{run}" \{{(.*?)\n\}}', text, re.S)
    assert block, f"run {run!r} not found in {TFTEST}"
    lhs = (
        rf"\{{ for e in google_cloud_run_v2_service\.{resource}\.template\[0\]\.containers\[0\]"
        r"\.env : e\.name => e\.value \} == \{(.*?)\n    \}"
    )
    found = re.search(lhs, block.group(1), re.S)
    assert found, f"run {run!r} no longer compares {resource}'s whole environment"
    env = dict(re.findall(r'^\s*([A-Z0-9_]+)\s*=\s*"([^"]*)"\s*$', found.group(1), re.M))
    assert len(env) >= 10, env
    return env


@pytest.fixture(scope="module")
def proxy_env() -> dict[str, str]:
    return expected_env("fhir_proxy_environment", "fhir_proxy")


@pytest.fixture(scope="module")
def cuff_env() -> dict[str, str]:
    return expected_env("cuff_sim_environment", "cuff_sim")


def test_fhir_proxy_starts_with_the_modules_environment(proxy_env: dict[str, str]) -> None:
    unknown = set(proxy_env) - set(proxy_config.VARIABLES)
    assert not unknown, f"fhir-proxy does not read {sorted(unknown)}"
    cfg = proxy_config.ServiceConfig.from_env(proxy_env)
    # What the module means to set, as the service understands it.
    assert cfg.path_prefix == "/api"
    assert cfg.public_base_url.endswith("/api")
    assert cfg.allowed_origins == (cfg.public_base_url.removesuffix("/api"),)
    assert cfg.request_timeout == 70
    assert cfg.cuff_forward_timeout == 55 and cfg.request_budget == 50
    assert cfg.trusted_proxy_hops == 1
    assert cfg.sample_patient is not None  # the PID values cuff-sim sends
    assert cfg.health_sync_url is not None
    assert cfg.password_file == "/secrets/reviewer-password/value"  # noqa: S105 - a path


def test_cuff_sim_starts_with_the_modules_environment(cuff_env: dict[str, str]) -> None:
    unknown = set(cuff_env) - set(cuff_config.VARIABLES)
    assert not unknown, f"cuff-sim does not read {sorted(unknown)}"
    cfg = cuff_config.ServiceConfig.from_env(cuff_env)
    assert cfg.https_timeout == 6 and cfg.mllp_timeout == 5
    assert cfg.mllp_port == 2575


def test_both_services_agree_on_the_patient_and_stores(
    proxy_env: dict[str, str], cuff_env: dict[str, str]
) -> None:
    # The proxy rebuilds a sent sample's bytes only with cuff-sim's exact PID values, and both
    # must address the same dataset, HL7v2 store and audit store.
    shared = [
        "GCP_PROJECT_ID",
        "HEALTHCARE_LOCATION",
        "HEALTHCARE_DATASET_ID",
        "HL7V2_STORE_ID",
        "AUDIT_FHIR_STORE_ID",
        "PATIENT_ID",
        "PATIENT_ASSIGNING_AUTHORITY",
        "PATIENT_FAMILY_NAME",
        "PATIENT_GIVEN_NAME",
    ]
    assert {k: proxy_env[k] for k in shared} == {k: cuff_env[k] for k in shared}


def test_the_check_sees_a_broken_map(proxy_env: dict[str, str]) -> None:
    # The parser really refuses what the module could get wrong (so the tests above can fail).
    bad = {**proxy_env, "REQUEST_TIMEOUT_SECONDS": "60"}  # 55 + 10 > 60
    with pytest.raises(proxy_config.ConfigError):
        proxy_config.ServiceConfig.from_env(bad)
    renamed = {k: v for k, v in proxy_env.items() if k != "PUBLIC_BASE_URL"}
    renamed["PUBLIC_URL"] = proxy_env["PUBLIC_BASE_URL"]
    with pytest.raises(proxy_config.ConfigError):
        proxy_config.ServiceConfig.from_env(renamed)
