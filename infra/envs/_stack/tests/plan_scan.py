#!/usr/bin/env python3
"""Check the stack's planned values across all seven modules (task T25).

`terraform test` assertions can read the stack's outputs and its child modules' outputs, but not
the resources inside the child modules. This script reads those resources instead:

1. It runs `terraform test -json -verbose` on tests/stack.tftest.hcl with mocked providers and
   every Google credential source removed from the environment. For each run with
   `command = apply`, `-verbose` prints the resulting state (the `terraform show -json` format)
   as a `test_state` message.
2. For each scenario run below it walks every resource in that state, child modules included,
   and checks the facts the module READMEs' "For T25" sections ask of the wiring.

The test file gives the PRODUCER resources (accounts, secrets, the VM's address, Cloud Run URLs,
store IDs) fixed values. These checks look at the CONSUMERS (a firewall rule, a push
subscription, a Cloud Run environment variable, the VM's metadata), so they test that a value
travelled through the wiring, not that the mock echoes what it was given.

Usage: python3 tests/plan_scan.py   (run from anywhere; exit code 0 = pass)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STACK_DIR = Path(__file__).resolve().parent.parent

PROJECT = "example-project"
IDENTITY_PREFIX = "pb"
PREFIX = "pulsebridge"
ADDRESS = "10.10.0.10"
MLLP_PORT = "2575"
ALERT_EMAIL = "alerts@example.com"
URIS = {
    "v2-to-fhir": "https://pulsebridge-v2-to-fhir-abc123-uc.a.run.app",
    "health-sync": "https://pulsebridge-health-sync-abc123-uc.a.run.app",
    "fhir-proxy": "https://pulsebridge-fhir-proxy-abc123-uc.a.run.app",
    "cuff-sim": "https://pulsebridge-cuff-sim-abc123-uc.a.run.app",
}

# Scenario runs in tests/stack.tftest.hcl and what each one is.
SCENARIOS = {
    "real_ingest_off": {"synthetic": False, "ingest": False, "env": "dev", "patient": "PAT-0001"},
    "real_ingest_on": {"synthetic": False, "ingest": True, "env": "dev", "patient": "PAT-0001"},
    "synthetic_ingest_off": {
        "synthetic": True,
        "ingest": False,
        "env": "dev",
        "patient": "SYN-0001",
    },
    "synthetic_ingest_on": {"synthetic": True, "ingest": True, "env": "dev", "patient": "SYN-0001"},
    "prod_settings": {"synthetic": False, "ingest": False, "env": "prod", "patient": "PAT-0001"},
}

# Every way Google tools find credentials or a project; all removed before Terraform runs.
CREDENTIAL_VARS = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_CLOUD_KEYFILE_JSON",
    "GCLOUD_KEYFILE_JSON",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_PROJECT",
    "GCP_SA_KEY_B64",
    "CLOUDSDK_CORE_PROJECT",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
)


def email(key: str) -> str:
    return f"{IDENTITY_PREFIX}-{key}@{PROJECT}.iam.gserviceaccount.com"


def secret_resource_id(key: str) -> str:
    return f"projects/{PROJECT}/secrets/{IDENTITY_PREFIX}-{key}"


def iter_resources(module: dict):
    """Every resource in a `terraform show -json` style module, child modules included."""
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from iter_resources(child)


def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_strings(v)


def env_of(container: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in container.get("env") or []}


def run_terraform() -> dict[str, dict]:
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}
    home = tempfile.mkdtemp(prefix="stack-plan-scan-home-")
    env.update(
        HOME=home,
        CLOUDSDK_CONFIG=os.path.join(home, "gcloud"),
        GCE_METADATA_HOST="127.0.0.1:9",
        GCE_METADATA_IP="127.0.0.1:9",
        NO_GCE_CHECK="True",
        CHECKPOINT_DISABLE="1",
        TF_IN_AUTOMATION="1",
    )
    try:
        result = subprocess.run(
            [
                "terraform",
                f"-chdir={STACK_DIR}",
                "test",
                "-no-color",
                "-json",
                "-verbose",
                "-filter=tests/stack.tftest.hcl",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    summary = [m for m in messages if m.get("type") == "test_summary"]
    if result.returncode != 0 or not summary or summary[-1]["test_summary"]["status"] != "pass":
        for m in messages:
            if m.get("@level") == "error":
                print(m.get("@message", ""), file=sys.stderr)
        raise SystemExit("FAIL: terraform test did not pass; the plan scan cannot run")
    return {m["@testrun"]: m["test_state"] for m in messages if m.get("type") == "test_state"}


class Scan:
    """The resources of one scenario's state, and the failures found in it."""

    def __init__(self, name: str, state: dict, expect: dict):
        self.name = name
        self.expect = expect
        self.failures: list[str] = []
        self.by_address = {r["address"]: r for r in iter_resources(state["root_module"])}

    def fail(self, message: str) -> None:
        self.failures.append(f"{self.name}: {message}")

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.fail(message)

    def of_type(self, rtype: str) -> list[dict]:
        return [
            r for r in self.by_address.values() if r["type"] == rtype and r["mode"] == "managed"
        ]

    def get(self, address: str) -> dict | None:
        r = self.by_address.get(address)
        if r is None:
            self.fail(f"missing resource {address}")
            return None
        return r["values"]

    def service(self, address: str) -> tuple[dict, dict]:
        values = self.get(address) or {}
        template = (values.get("template") or [{}])[0]
        container = (template.get("containers") or [{}])[0]
        return values, template | {"_env": env_of(container)}


def check_security(s: Scan) -> None:
    """No public principal, no keys, no authoritative IAM, exactly one public service."""
    for address, r in s.by_address.items():
        for text in iter_strings(r["values"]):
            if text in ("allUsers", "allAuthenticatedUsers"):
                s.fail(f"{address} names the public principal {text}")
        rtype = r["type"]
        if rtype == "google_service_account_key":
            s.fail(f"{address}: service account keys are never created")
        if rtype.endswith("_iam_binding") or rtype.endswith("_iam_policy"):
            s.fail(f"{address}: authoritative IAM ({rtype}) would remove other modules' grants")

    project_grants = sorted(
        (r["values"]["role"], r["values"]["member"]) for r in s.of_type("google_project_iam_member")
    )
    s.check(
        project_grants == [("roles/logging.logWriter", f"serviceAccount:{email('edge-vm')}")],
        f"the only project-level grant must be the edge VM's log writer, found {project_grants}",
    )

    services = {r["values"]["name"]: r["values"] for r in s.of_type("google_cloud_run_v2_service")}
    public = sorted(n for n, v in services.items() if v.get("invoker_iam_disabled"))
    s.check(
        public == [f"{PREFIX}-fhir-proxy"],
        f"exactly one public service (fhir-proxy), found {public}",
    )
    ingress_all = sorted(
        n for n, v in services.items() if v.get("ingress") == "INGRESS_TRAFFIC_ALL"
    )
    s.check(
        ingress_all == [f"{PREFIX}-fhir-proxy"],
        f"only fhir-proxy may take internet traffic, found {ingress_all}",
    )


def check_mode_and_ingest(s: Scan) -> None:
    synthetic, ingest = s.expect["synthetic"], s.expect["ingest"]
    names = {r["values"]["name"] for r in s.of_type("google_cloud_run_v2_service")}
    jobs = {r["values"]["name"] for r in s.of_type("google_cloud_run_v2_job")}
    accounts = {r["values"]["account_id"] for r in s.of_type("google_service_account")}

    s.check(
        (f"{PREFIX}-health-sync" in names) == (not synthetic),
        "health-sync exists exactly with real data",
    )
    s.check(
        (f"{PREFIX}-seed" in jobs) == synthetic, "the seeder job exists exactly with synthetic data"
    )
    s.check(
        (f"{IDENTITY_PREFIX}-seeder" in accounts) == synthetic,
        "the seeder account exists exactly with synthetic data",
    )

    schedulers = s.of_type("google_cloud_scheduler_job")
    s.check(
        len(schedulers) == (1 if ingest else 0),
        f"{len(schedulers)} Scheduler job(s); expected {1 if ingest else 0}",
    )
    for job in schedulers:
        target = job["values"]["http_target"][0]
        if synthetic:
            s.check(
                target["uri"].endswith(f"/jobs/{PREFIX}-seed:run"),
                "the seeder trigger must run the seeder job",
            )
            s.check(
                target["oauth_token"][0]["service_account_email"] == email("scheduler-invoker"),
                "seeder trigger identity",
            )
        else:
            s.check(
                target["uri"] == URIS["health-sync"] + "/sync",
                "the sync trigger must POST health-sync's /sync",
            )
            oidc = target["oidc_token"][0]
            s.check(
                oidc["service_account_email"] == email("scheduler-invoker"), "sync trigger identity"
            )
            s.check(oidc["audience"] == URIS["health-sync"], "sync trigger audience")

    sub = s.get("module.healthcare.google_pubsub_subscription.ingest") or {}
    push = sub.get("push_config") or []
    if not ingest:
        s.check(push == [], "with ingest_enabled = false the ingest subscription must not push")
    else:
        s.check(len(push) == 1, "with ingest_enabled = true the ingest subscription must push")
        if push:
            s.check(
                push[0]["push_endpoint"] == URIS["v2-to-fhir"] + "/pubsub/hl7v2",
                "push endpoint = v2-to-fhir URL + /pubsub/hl7v2",
            )
            s.check(
                push[0]["oidc_token"][0]["service_account_email"] == email("pubsub-push"),
                "push identity = pubsub-push",
            )
    invokers = {
        (r["values"]["name"], r["values"]["member"])
        for r in s.of_type("google_cloud_run_v2_service_iam_member")
    }
    s.check(
        (f"{PREFIX}-v2-to-fhir", f"serviceAccount:{email('pubsub-push')}") in invokers,
        "pubsub-push may invoke v2-to-fhir",
    )


def check_patient(s: Scan) -> None:
    """One patient everywhere: the cuff path breaks silently if two services disagree."""
    synthetic, patient = s.expect["synthetic"], s.expect["patient"]
    _, v2 = s.service("module.ingestion.google_cloud_run_v2_service.v2_to_fhir")
    _, cuff = s.service("module.phr.google_cloud_run_v2_service.cuff_sim")
    _, proxy = s.service("module.phr.google_cloud_run_v2_service.fhir_proxy")
    ids = {
        "v2-to-fhir": v2["_env"].get("PATIENT_ID"),
        "cuff-sim": cuff["_env"].get("PATIENT_ID"),
        "fhir-proxy": proxy["_env"].get("PATIENT_ID"),
    }
    if synthetic:
        job = s.get("module.ingestion.google_cloud_run_v2_job.seed[0]") or {}
        container = job["template"][0]["template"][0]["containers"][0] if job else {}
        ids["seeder"] = env_of(container).get("PATIENT_ID")
    else:
        _, hs = s.service("module.ingestion.google_cloud_run_v2_service.health_sync[0]")
        ids["health-sync"] = hs["_env"].get("PATIENT_ID")
    s.check(set(ids.values()) == {patient}, f"every service must get PATIENT_ID {patient}: {ids}")

    authorities = {
        "v2-to-fhir": v2["_env"].get("PATIENT_ASSIGNING_AUTHORITY"),
        "cuff-sim": cuff["_env"].get("PATIENT_ASSIGNING_AUTHORITY"),
        "fhir-proxy": proxy["_env"].get("PATIENT_ASSIGNING_AUTHORITY"),
    }
    s.check(
        set(authorities.values()) == {"EXAMPLE"},
        f"one assigning authority for v2-to-fhir and the cuff path: {authorities}",
    )

    family, given = ("Synthetic", "Demo") if synthetic else ("Examplefamily", "Examplegiven")
    for name, svc in (("cuff-sim", cuff), ("fhir-proxy", proxy)):
        got = (svc["_env"].get("PATIENT_FAMILY_NAME"), svc["_env"].get("PATIENT_GIVEN_NAME"))
        s.check(got == (family, given), f"{name} patient name {got}, expected {(family, given)}")


def check_edge_vm(s: Scan) -> None:
    """project-base, edge-vm and openemr agree on the account, ports, mirror and address."""
    edge = email("edge-vm")
    fw = (
        s.get('module.project_base.google_compute_firewall.allow["allow-serverless-to-edge"]') or {}
    )
    s.check(
        fw.get("target_service_accounts") == [edge],
        "the MLLP/HTTPS firewall rule must target the edge VM's account",
    )
    ports = sorted(p for a in fw.get("allow", []) for p in a.get("ports", []))
    s.check(ports == sorted([MLLP_PORT, "443"]), f"firewall ports {ports}")

    mirror = (
        s.get(
            "module.project_base.google_artifact_registry_repository_iam_member.dockerhub_mirror_edge_vm_reader[0]"
        )
        or {}
    )
    s.check(
        mirror.get("member") == f"serviceAccount:{edge}"
        and mirror.get("role") == "roles/artifactregistry.reader",
        "the edge VM's account must read the Docker Hub mirror",
    )
    s.check(
        mirror.get("repository") == f"{PREFIX}-dockerhub",
        "the grant must be on the mirror repository",
    )

    vm = s.get("module.edge_vm.google_compute_instance.this") or {}
    s.check(
        vm.get("service_account", [{}])[0].get("email") == edge,
        "the VM runs as the edge VM account",
    )
    nic = (vm.get("network_interface") or [{}])[0]
    s.check(nic.get("network_ip") == ADDRESS, "the VM uses its reserved internal address")
    s.check(
        nic.get("subnetwork")
        == f"projects/{PROJECT}/regions/us-central1/subnetworks/{PREFIX}-edge",
        "the VM is on project-base's edge subnet",
    )
    s.check(not nic.get("access_config"), "the VM has no external IP")

    meta = vm.get("metadata") or {}
    for key in ("pulsebridge-hook-openemr", "pulsebridge-hook-openemr-oauth-client"):
        s.check(key in meta, f"VM metadata lacks {key}")
    hook = meta.get("pulsebridge-hook-openemr", "")
    s.check(
        f"https://{ADDRESS}" in hook,
        "the openemr hook must use the VM's reserved address as OpenEMR's site address",
    )
    s.check(
        f"us-central1-docker.pkg.dev/{PROJECT}/{PREFIX}-dockerhub" in hook,
        "the openemr hook must pull through the mirror",
    )

    script = meta.get("startup-script", "")
    s.check(
        "EDGE_HOOK_TIMEOUT_SECONDS='2400'" in script,
        "the hooks need module openemr's 40-minute limit",
    )
    for file_name, secret in (
        ("mariadb-root-password", "db-root-password"),
        ("openemr-db-password", "openemr-db-password"),
        ("openemr-admin-password", "openemr-admin-password"),
    ):
        s.check(
            f"'{file_name}={secret_resource_id(secret)}'" in script,
            f"the VM must copy {secret} to {file_name}",
        )
    for flag in (
        f"--port={MLLP_PORT}",
        f"--hl7_v2_project_id={PROJECT}",
        "--hl7_v2_location_id=us-central1",
        f"--hl7_v2_dataset_id={PREFIX}",
        f"--hl7_v2_store_id={PREFIX}-hl7v2",
    ):
        s.check(flag in script, f"the MLLP adapter command lacks {flag}")

    _, cuff = s.service("module.phr.google_cloud_run_v2_service.cuff_sim")
    s.check(
        cuff["_env"].get("MLLP_HOST") == ADDRESS and cuff["_env"].get("MLLP_PORT") == MLLP_PORT,
        "cuff-sim must send MLLP to the VM's address and the firewall's port",
    )
    s.check(
        cuff["vpc_access"][0]["network_interfaces"][0]["subnetwork"] == f"{PREFIX}-serverless",
        "cuff-sim must egress through the serverless subnet (the firewall rule's source)",
    )

    writers = {
        (r["values"]["role"], r["values"]["member"])
        for r in s.of_type("google_secret_manager_secret_iam_member")
        if r["values"]["secret_id"] == secret_resource_id("openemr-client-private-key")
    }
    s.check(
        ("roles/secretmanager.secretVersionAdder", f"serviceAccount:{edge}") in writers,
        "the VM must be able to add the OpenEMR client key version (edge_vm_writes_openemr_key)",
    )


def check_front_door(s: Scan) -> None:
    synthetic = s.expect["synthetic"]
    _, proxy = s.service("module.phr.google_cloud_run_v2_service.fhir_proxy")
    env = proxy["_env"]
    if synthetic:
        s.check("HEALTH_SYNC_URL" not in env, "no HEALTH_SYNC_URL with synthetic data")
    else:
        s.check(
            env.get("HEALTH_SYNC_URL") == URIS["health-sync"],
            "fhir-proxy must call health-sync's URL",
        )
    s.check(
        env.get("OPENEMR_SITE_ADDRESS") == f"https://{ADDRESS}",
        "fhir-proxy must call OpenEMR at the VM's address",
    )
    s.check(
        env.get("CUFF_SIM_URL") == URIS["cuff-sim"], "fhir-proxy must forward to cuff-sim's URL"
    )
    s.check(env.get("TRUSTED_PROXY_HOPS") == "1", "trusted_proxy_hops must reach fhir-proxy")
    volumes = {
        v["name"]: v["secret"][0]["secret"] for v in proxy.get("volumes", []) if v.get("secret")
    }
    s.check(
        volumes.get("openemr-client") == f"{IDENTITY_PREFIX}-openemr-client-private-key",
        f"fhir-proxy must mount the OpenEMR client secret by its short ID: {volumes}",
    )
    s.check(
        volumes.get("reviewer-password") == f"{IDENTITY_PREFIX}-reviewer-password",
        "fhir-proxy must mount the reviewer password",
    )
    s.check(
        proxy["vpc_access"][0]["egress"] == "ALL_TRAFFIC",
        "fhir-proxy egress ALL_TRAFFIC (internal-only callees)",
    )

    _, v2 = s.service("module.ingestion.google_cloud_run_v2_service.v2_to_fhir")
    dataset = s.get("module.healthcare.google_healthcare_dataset.this") or {}
    s.check(
        dataset.get("project") == PROJECT and v2["_env"].get("GCP_PROJECT_ID") == PROJECT,
        "the Healthcare dataset and ingestion must share a project (push is internal only then)",
    )


def check_alerts(s: Scan) -> None:
    channels = s.of_type("google_monitoring_notification_channel")
    s.check(
        len(channels) == 1 and channels[0]["values"]["labels"] == {"email_address": ALERT_EMAIL},
        "alert_email must create one email channel",
    )
    policies = s.of_type("google_monitoring_alert_policy")
    s.check(len(policies) > 0, "the alerts must exist (an address is set)")
    if channels:
        name = channels[0]["values"]["name"]
        for p in policies:
            s.check(
                name in (p["values"].get("notification_channels") or []),
                f"{p['address']} must notify the email channel",
            )


def check_environment(s: Scan) -> None:
    protect = s.expect["env"] == "prod"
    policies = {
        r["values"].get("deletion_policy")
        for r in s.by_address.values()
        if r["type"]
        in (
            "google_healthcare_dataset",
            "google_healthcare_fhir_store",
            "google_healthcare_hl7_v2_store",
        )
    }
    s.check(
        policies == {"PREVENT" if protect else "DELETE"}, f"Healthcare deletion policies {policies}"
    )
    # prod keeps a week of daily snapshots of the OpenEMR database disk; dev none.
    snaps = s.of_type("google_compute_resource_policy")
    attached = s.of_type("google_compute_disk_resource_policy_attachment")
    if protect:
        days = [
            p["values"]["snapshot_schedule_policy"][0]["retention_policy"][0]["max_retention_days"]
            for p in snaps
        ]
        s.check(
            days == [7] and len(attached) == 1, f"prod: one 7-day snapshot schedule, found {days}"
        )
        if attached:
            s.check(
                attached[0]["values"]["disk"] == f"{PREFIX}-edge-data",
                "the schedule is on the data disk",
            )
    else:
        s.check(not snaps and not attached, "dev: no snapshot schedule")
    secrets = {
        r["values"].get("deletion_protection") for r in s.of_type("google_secret_manager_secret")
    }
    s.check(secrets == {protect}, f"secret deletion_protection {secrets}")
    runs = {
        r["values"].get("deletion_protection")
        for r in s.of_type("google_cloud_run_v2_service") + s.of_type("google_cloud_run_v2_job")
    }
    s.check(runs == {protect}, f"Cloud Run deletion_protection {runs}")
    labels = {
        r["values"].get("labels", {}).get("environment")
        for r in s.of_type("google_cloud_run_v2_service")
    }
    s.check(labels == {s.expect["env"]}, f"environment label {labels}")


# The Healthcare coordinates every workload must get, as literals: a crossed wire (the audit store
# where the main store belongs, a hard-coded project) shows up as a wrong value here.
EXPECTED_ENV = {
    "GCP_PROJECT_ID": PROJECT,
    "HEALTHCARE_LOCATION": "us-central1",
    "HEALTHCARE_DATASET_ID": PREFIX,
    "FHIR_STORE_ID": f"{PREFIX}-fhir",
    "AUDIT_FHIR_STORE_ID": f"{PREFIX}-fhir-audit",
    "HL7V2_STORE_ID": f"{PREFIX}-hl7v2",
}
# Which of those each workload must have (D15: cuff-sim has no FHIR_STORE_ID, the health data).
WORKLOADS = {
    # name: (identity key, store variables it must have)
    f"{PREFIX}-v2-to-fhir": ("v2-to-fhir", {"FHIR_STORE_ID", "HL7V2_STORE_ID"}),
    f"{PREFIX}-health-sync": ("health-sync", {"FHIR_STORE_ID"}),
    f"{PREFIX}-fhir-proxy": (
        "fhir-proxy",
        {"FHIR_STORE_ID", "AUDIT_FHIR_STORE_ID", "HL7V2_STORE_ID"},
    ),
    f"{PREFIX}-cuff-sim": ("cuff-sim", {"AUDIT_FHIR_STORE_ID", "HL7V2_STORE_ID"}),
    f"{PREFIX}-seed": ("seeder", {"FHIR_STORE_ID"}),
}
COMMON = {"GCP_PROJECT_ID", "HEALTHCARE_LOCATION", "HEALTHCARE_DATASET_ID"}


def check_workload_identity_and_stores(s: Scan) -> None:
    """Each Cloud Run service and the job runs as ITS OWN account and gets the right stores."""
    workloads = []
    for r in s.of_type("google_cloud_run_v2_service"):
        template = r["values"]["template"][0]
        workloads.append((r["values"]["name"], template))
    for r in s.of_type("google_cloud_run_v2_job"):
        workloads.append((r["values"]["name"], r["values"]["template"][0]["template"][0]))
    if not workloads:
        s.fail("no Cloud Run workload found")
    for name, template in workloads:
        if name not in WORKLOADS:
            s.fail(f"unexpected Cloud Run workload {name}")
            continue
        key, stores = WORKLOADS[name]
        s.check(
            template.get("service_account") == email(key),
            f"{name} must run as its own account {email(key)}, "
            f"not {template.get('service_account')}",
        )
        env = env_of(template["containers"][0])
        for var in sorted(COMMON | stores):
            s.check(
                env.get(var) == EXPECTED_ENV[var],
                f"{name}: {var} = {env.get(var)!r}, expected {EXPECTED_ENV[var]!r}",
            )
        for var in sorted(set(EXPECTED_ENV) - COMMON - stores):
            s.check(var not in env, f"{name} must not get {var} (= {env.get(var)!r})")


# The positive controls: planned values of the email notification channel itself. Its display
# name is not sensitive and must appear (the search sees the channel's planned values at all),
# and its address label must appear as redacted (the address is sensitive, T26).
PLAN_TEXT_POSITIVE_CONTROLS = (
    'display_name        = "PulseBridge pulsebridge alerts"',
    '"email_address" = (sensitive value)',
)


def check_plan_redaction() -> list[str]:
    """The patient's identifier and names and the alert address are sensitive root variables, so a
    human-readable plan (what CI posts on a pull request, T26) must not show them. `terraform test
    -verbose` prints each run's plan or state the way `terraform plan` does. Before T26 the alert
    address was the positive control (it could not be sensitive); module ingestion's separate bool
    made it sensitive, so it is now checked like the patient values, and the positive controls are
    the email channel's display name and its redacted label."""
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}
    home = tempfile.mkdtemp(prefix="stack-plan-scan-home-")
    env.update(
        HOME=home,
        CLOUDSDK_CONFIG=os.path.join(home, "gcloud"),
        GCE_METADATA_HOST="127.0.0.1:9",
        GCE_METADATA_IP="127.0.0.1:9",
        NO_GCE_CHECK="True",
        CHECKPOINT_DISABLE="1",
    )
    try:
        result = subprocess.run(
            [
                "terraform",
                f"-chdir={STACK_DIR}",
                "test",
                "-no-color",
                "-verbose",
                "-filter=tests/stack.tftest.hcl",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    text = result.stdout + result.stderr
    failures = []
    if result.returncode != 0:
        failures.append("redaction: terraform test -verbose failed")
    for value in ("PAT-0001", "SYN-0001", "Examplefamily", "Examplegiven", ALERT_EMAIL):
        if value in text:
            failures.append(
                f"redaction: the sensitive value {value!r} is shown in a human-readable plan"
            )
    for control in PLAN_TEXT_POSITIVE_CONTROLS:
        if control not in text:
            failures.append(
                f"redaction: {control!r} is not in the plan text; the search proves nothing"
            )
    return failures


def main() -> int:
    states = run_terraform()
    # Every apply run in the test file is a scenario, and every scenario is an apply run: an empty
    # or stale SCENARIOS table would otherwise check nothing and pass.
    if not SCENARIOS or set(SCENARIOS) != set(states):
        raise SystemExit(
            f"FAIL: SCENARIOS {sorted(SCENARIOS)} must equal the state-producing runs "
            f"{sorted(states)}"
        )
    failures: list[str] = []
    for name, expect in SCENARIOS.items():
        scan = Scan(name, states[name], expect)
        for check in (
            check_security,
            check_mode_and_ingest,
            check_patient,
            check_edge_vm,
            check_front_door,
            check_alerts,
            check_environment,
            check_workload_identity_and_stores,
        ):
            check(scan)
        if len(scan.by_address) < 50:
            scan.fail(f"only {len(scan.by_address)} resources in the state; the stack has about 94")
        print(f"{name}: {len(scan.by_address)} resources checked, {len(scan.failures)} failure(s)")
        failures += scan.failures
    failures += check_plan_redaction()
    if failures:
        for f in failures:
            print("FAIL:", f, file=sys.stderr)
        return 1
    print("PASS: the stack's planned values match the modules' wiring rules in every scenario")
    return 0


if __name__ == "__main__":
    sys.exit(main())
