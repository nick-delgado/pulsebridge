"""The OpenEMR settings (all or none, checked at start, names never values) and the client
document (read on every share, refused with a bounded reason, never echoed)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fhir_proxy.config import VARIABLES, ConfigError, ServiceConfig
from fhir_proxy.share.testing import BP_ID, SITE, make_certificate, make_key

REPO = Path(__file__).resolve().parents[4]
BASE_ENV = {
    "GCP_PROJECT_ID": "example-project",
    "HEALTHCARE_LOCATION": "us-central1",
    "HEALTHCARE_DATASET_ID": "example-dataset",
    "FHIR_STORE_ID": "main",
    "AUDIT_FHIR_STORE_ID": "audit",
    "HL7V2_STORE_ID": "example-store",
    "PATIENT_ID": "PB-SYN-0001",
    "REVIEWER_PASSWORD_FILE": "/secrets/reviewer-password",
    "ALLOWED_ORIGINS": "https://dashboard.example.test",
    "CUFF_SIM_URL": "https://cuff-sim.example.test",
    "PUBLIC_BASE_URL": "https://proxy.example.test",
}
OPENEMR_ENV = {
    "OPENEMR_SITE_ADDRESS": "https://10.10.0.2",
    "OPENEMR_TOKEN_URL": "https://10.10.0.2/oauth2/default/token",
    "OPENEMR_FHIR_BASE_URL": "https://10.10.0.2/apis/default/fhir",
    "OPENEMR_SCOPES": "system/Patient.read system/Patient.write system/Encounter.read "
    "system/Encounter.write system/Observation.read system/Observation.write",
    "OPENEMR_CLIENT_FILE": "/secrets/openemr-client/value",
}


def test_without_the_openemr_variables_sharing_is_simply_off() -> None:
    assert ServiceConfig.from_env(BASE_ENV).openemr is None


def test_the_openemr_settings_as_module_phr_passes_them() -> None:
    settings = ServiceConfig.from_env({**BASE_ENV, **OPENEMR_ENV}).openemr
    assert settings is not None
    assert (settings.site_address, settings.host, settings.port) == (
        "https://10.10.0.2",
        "10.10.0.2",
        443,
    )
    assert settings.token_url == "https://10.10.0.2/oauth2/default/token"  # noqa: S105
    assert settings.timeout == 10
    with_port = ServiceConfig.from_env(
        {
            **BASE_ENV,
            **{k: v.replace("10.10.0.2", "10.10.0.2:9300") for k, v in OPENEMR_ENV.items()},
        }
    ).openemr
    assert with_port is not None and with_port.port == 9300


def test_the_variable_names_are_the_ones_module_phr_sets() -> None:
    tf = (REPO / "infra" / "modules" / "phr" / "fhir_proxy.tf").read_text()
    main = (REPO / "infra" / "modules" / "phr" / "main.tf").read_text()
    passed = set(re.findall(r"^\s*(OPENEMR_[A-Z_]+)\s*=", tf, re.M)) | set(
        re.findall(r'env\s*=\s*"(OPENEMR_[A-Z_]+)"', main)
    )
    assert passed == set(OPENEMR_ENV)
    assert passed <= set(VARIABLES)


@pytest.mark.parametrize("name", list(OPENEMR_ENV))
def test_all_or_none(name: str) -> None:
    env = {**BASE_ENV, **OPENEMR_ENV, name: ""}
    with pytest.raises(ConfigError, match=name):
        ServiceConfig.from_env(env)


@pytest.mark.parametrize(
    ("change", "name"),
    [
        ({"OPENEMR_SITE_ADDRESS": "http://10.10.0.2"}, "OPENEMR_SITE_ADDRESS"),
        ({"OPENEMR_SITE_ADDRESS": "https://openemr.example.test"}, "OPENEMR_SITE_ADDRESS"),
        ({"OPENEMR_SITE_ADDRESS": "https://8.8.8.8"}, "OPENEMR_SITE_ADDRESS"),  # public
        ({"OPENEMR_SITE_ADDRESS": "https://[2606:4700::1]"}, "OPENEMR_SITE_ADDRESS"),
        ({"OPENEMR_SITE_ADDRESS": "https://10.10.0.2/openemr"}, "OPENEMR_SITE_ADDRESS"),
        ({"OPENEMR_SITE_ADDRESS": "https://u:p@10.10.0.2"}, "OPENEMR_SITE_ADDRESS"),
        ({"OPENEMR_SITE_ADDRESS": "https://10.10.0.2?x=1"}, "OPENEMR_SITE_ADDRESS"),
        ({"OPENEMR_TOKEN_URL": "https://10.10.0.3/oauth2/default/token"}, "OPENEMR_TOKEN_URL"),
        ({"OPENEMR_TOKEN_URL": "https://10.10.0.2/oauth2/other/token"}, "OPENEMR_TOKEN_URL"),
        ({"OPENEMR_FHIR_BASE_URL": "https://10.10.0.2/fhir"}, "OPENEMR_FHIR_BASE_URL"),
        ({"OPENEMR_SCOPES": "system/*.*"}, "OPENEMR_SCOPES"),
        ({"OPENEMR_SCOPES": "user/Patient.read"}, "OPENEMR_SCOPES"),
        ({"OPENEMR_SCOPES": "system/Patient.read system/Patient.read"}, "OPENEMR_SCOPES"),
        ({"OPENEMR_SCOPES": "system/Patient."}, "OPENEMR_SCOPES"),
        ({"OPENEMR_SCOPES": "system/Patient.srs"}, "OPENEMR_SCOPES"),
        # Valid scopes that do not allow what the share does: no Patient search, no Encounter.
        ({"OPENEMR_SCOPES": "system/Patient.write system/Observation.write"}, "OPENEMR_SCOPES"),
        ({"OPENEMR_CLIENT_FILE": "relative/value"}, "OPENEMR_CLIENT_FILE"),
        ({"OPENEMR_TIMEOUT_SECONDS": "90"}, "OPENEMR_TIMEOUT_SECONDS"),
    ],
)
def test_bad_values_are_refused_by_name_never_by_value(change: dict, name: str) -> None:
    env = {**BASE_ENV, **OPENEMR_ENV, **change}
    if "OPENEMR_SITE_ADDRESS" in change:
        # The other URLs follow the site, so only the site's own check can refuse it.
        site = change["OPENEMR_SITE_ADDRESS"]
        env["OPENEMR_TOKEN_URL"] = site + "/oauth2/default/token"
        env["OPENEMR_FHIR_BASE_URL"] = site + "/apis/default/fhir"
    with pytest.raises(ConfigError) as error:
        ServiceConfig.from_env(env)
    assert str(error.value).startswith(name), str(error.value)
    for value in change.values():
        assert value not in str(error.value)


def test_the_narrow_smart_v2_scopes_are_enough() -> None:
    narrow = "system/Patient.crs system/Encounter.crs system/Observation.cr"
    settings = ServiceConfig.from_env({**BASE_ENV, **OPENEMR_ENV, "OPENEMR_SCOPES": narrow}).openemr
    assert settings is not None and settings.scopes == tuple(narrow.split())


# ---- the client document, through the route -------------------------------------------------


def not_configured(env, response) -> str:  # noqa: ANN001
    assert response.status_code == 503, response.get_data(as_text=True)
    body = response.get_json()
    assert body["issue"][0]["details"]["coding"][0]["code"] == "share-not-configured"
    assert env.openemr.requests == []
    return body["issue"][0]["diagnostics"]


def test_a_missing_document_is_503_and_the_probe_says_not_registered(env) -> None:
    env.doc_path.unlink()
    assert "has not registered the client" in not_configured(env, env.share())
    assert env.harness.get("/status").get_json()["openemr"]["detail"] == "client-not-registered"


def test_an_empty_document_counts_as_missing(env) -> None:
    env.doc_path.write_text("\n")
    assert "not there yet" in not_configured(env, env.share())


def test_an_unreadable_document(env) -> None:
    env.doc_path.unlink()
    env.doc_path.mkdir()  # reading a directory fails
    assert "cannot be read" in not_configured(env, env.share())


@pytest.mark.parametrize(
    ("overrides", "phrase"),
    [
        ({"format": "pulsebridge.openemr-client.v2"}, "unknown format"),
        ({"client_id": "short"}, "lacks a field"),
        ({"client_id": "has spaces in it, not allowed"}, "lacks a field"),
        ({"kid": None}, "lacks a field"),
        ({"alg": "RS256"}, "lacks a field"),
        ({"token_endpoint_auth_method": "client_secret_basic"}, "lacks a field"),
        ({"private_key": "-----BEGIN NOT A KEY-----"}, "private key is not a usable RSA key"),
        ({"server_certificate_pem": "not a certificate"}, "server certificate is not usable"),
        ({"site_addr_oath": "https://10.10.0.9"}, "another OpenEMR address"),
        ({"token_url": "https://10.10.0.9/oauth2/default/token"}, "another OpenEMR address"),
        ({"fhir_base_url": "https://10.10.0.9/apis/default/fhir"}, "another OpenEMR address"),
        ({"scope": "system/Patient.read"}, "scopes differ from OPENEMR_SCOPES"),
    ],
)
def test_a_document_that_cannot_be_used_is_refused_and_never_echoed(
    env, logs, overrides, phrase
) -> None:  # noqa: ANN001
    doc = env.write_doc(**overrides)
    response = env.share()
    assert phrase in not_configured(env, response)
    text = response.get_data(as_text=True) + logs.text()
    for value in (doc["private_key"], doc["client_id"], doc["server_certificate_pem"], SITE):
        if value:
            assert value not in text


def test_a_document_that_is_not_json(env) -> None:
    env.doc_path.write_text("format: yaml")
    assert "not a JSON object" in not_configured(env, env.share())
    env.doc_path.write_text("[1, 2]")
    assert "not a JSON object" in not_configured(env, env.share())


def test_a_short_rsa_key_is_refused(env) -> None:
    from fhir_proxy.share.testing import client_document

    doc = client_document(env.settings, key=make_key(1024), certificate_pem=env.cert_pem)
    env.doc_path.write_text(json.dumps(doc))
    assert "usable RSA key" in not_configured(env, env.share())


def test_the_document_is_read_on_every_share_so_a_new_registration_takes_effect(env) -> None:
    env.doc_path.unlink()
    not_configured(env, env.share())
    env.write_doc()  # the VM registered the client meanwhile
    assert env.share().status_code == 200


def test_the_document_is_not_read_at_start(tmp_path, client_key, server_cert) -> None:
    from fhir_proxy.share.testing import build_share_env

    env = build_share_env(tmp_path, client_key, server_cert)
    env.doc_path.unlink()  # no document at all: the app still started and serves other routes
    assert env.harness.get("/fhir/metadata").status_code == 200


def test_a_document_for_a_certificate_without_this_ip_still_loads(env) -> None:
    # The document loads (its certificate is a valid PEM); the TLS check happens on connect.
    cert, _ = make_certificate(["10.10.0.2"])
    env.write_doc(server_certificate_pem=cert)
    assert env.share(observation_id=BP_ID).status_code == 200
