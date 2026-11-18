# The stack composition under mocked providers: no credentials, nothing created. `make tf-test`
# runs it with every Google credential source removed from the environment.
#
# The apply runs build the whole stack in the mock. The override_resource blocks below give the
# PRODUCER resources (service accounts, secrets, the VM's reserved address, the Cloud Run URLs,
# the store IDs) fixed, distinct, well-formed values, so (1) the modules' own validations accept
# what flows between them, and (2) an assertion here, or in tests/plan_scan.py, that a value
# arrives at a CONSUMER (the firewall rule, the VM's hooks, a Cloud Run environment variable) is
# testing the wiring, not echoing the mock.
#
# Terraform test assertions can read this module's outputs and its child modules' outputs, but
# not resources inside the child modules. tests/plan_scan.py therefore reads the full state of
# the scenario runs (the apply runs named in its SCENARIOS) from `terraform test -json -verbose`
# and checks the planned values of resources anywhere in the tree.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number     = "100000000001"
      project_id = "example-project"
    }
  }
}

mock_provider "google-beta" {}

# Module identity's Pub/Sub service agent (google-beta) answers with the agent of the synthetic
# project number above, as Google does; the module's postcondition refuses any other address.
override_resource {
  target = module.identity.google_project_service_identity.pubsub
  values = {
    email  = "service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
    member = "serviceAccount:service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
  }
}

# T38: module healthcare's Healthcare service agent (google-beta), likewise.
override_resource {
  target = module.healthcare.google_project_service_identity.healthcare
  values = {
    email  = "service-100000000001@gcp-sa-healthcare.iam.gserviceaccount.com"
    member = "serviceAccount:service-100000000001@gcp-sa-healthcare.iam.gserviceaccount.com"
  }
}

# ---- producers: identity (generated pattern: <prefix>-<key> accounts and secrets) ------------

override_resource {
  target = module.identity.google_service_account.this["health-sync"]
  values = {
    email  = "pb-health-sync@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:pb-health-sync@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/pb-health-sync@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target = module.identity.google_service_account.this["v2-to-fhir"]
  values = {
    email  = "pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target = module.identity.google_service_account.this["cuff-sim"]
  values = {
    email  = "pb-cuff-sim@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:pb-cuff-sim@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/pb-cuff-sim@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target = module.identity.google_service_account.this["fhir-proxy"]
  values = {
    email  = "pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/pb-fhir-proxy@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target = module.identity.google_service_account.this["edge-vm"]
  values = {
    email  = "pb-edge-vm@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:pb-edge-vm@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/pb-edge-vm@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target = module.identity.google_service_account.this["scheduler-invoker"]
  values = {
    email  = "pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target = module.identity.google_service_account.this["pubsub-push"]
  values = {
    email  = "pb-pubsub-push@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:pb-pubsub-push@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/pb-pubsub-push@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target = module.identity.google_service_account.this["seeder"]
  values = {
    email  = "pb-seeder@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:pb-seeder@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/pb-seeder@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target = module.identity.google_secret_manager_secret.this["google-health-client-id"]
  values = {
    id   = "projects/example-project/secrets/pb-google-health-client-id"
    name = "projects/100000000001/secrets/pb-google-health-client-id"
  }
}

override_resource {
  target = module.identity.google_secret_manager_secret.this["google-health-client-secret"]
  values = {
    id   = "projects/example-project/secrets/pb-google-health-client-secret"
    name = "projects/100000000001/secrets/pb-google-health-client-secret"
  }
}

override_resource {
  target = module.identity.google_secret_manager_secret.this["google-health-refresh-token"]
  values = {
    id   = "projects/example-project/secrets/pb-google-health-refresh-token"
    name = "projects/100000000001/secrets/pb-google-health-refresh-token"
  }
}

override_resource {
  target = module.identity.google_secret_manager_secret.this["openemr-client-private-key"]
  values = {
    id   = "projects/example-project/secrets/pb-openemr-client-private-key"
    name = "projects/100000000001/secrets/pb-openemr-client-private-key"
  }
}

override_resource {
  target = module.identity.google_secret_manager_secret.this["reviewer-password"]
  values = {
    id   = "projects/example-project/secrets/pb-reviewer-password"
    name = "projects/100000000001/secrets/pb-reviewer-password"
  }
}

override_resource {
  target = module.identity.google_secret_manager_secret.this["db-root-password"]
  values = {
    id   = "projects/example-project/secrets/pb-db-root-password"
    name = "projects/100000000001/secrets/pb-db-root-password"
  }
}

override_resource {
  target = module.identity.google_secret_manager_secret.this["openemr-db-password"]
  values = {
    id   = "projects/example-project/secrets/pb-openemr-db-password"
    name = "projects/100000000001/secrets/pb-openemr-db-password"
  }
}

override_resource {
  target = module.identity.google_secret_manager_secret.this["openemr-admin-password"]
  values = {
    id   = "projects/example-project/secrets/pb-openemr-admin-password"
    name = "projects/100000000001/secrets/pb-openemr-admin-password"
  }
}

# ---- producers: network, stores, services, the VM's address ----------------------------------

override_resource {
  target = module.project_base.google_compute_network.this
  values = {
    id        = "projects/example-project/global/networks/pulsebridge-vpc"
    self_link = "https://www.googleapis.com/compute/v1/projects/example-project/global/networks/pulsebridge-vpc"
  }
}

override_resource {
  target = module.project_base.google_compute_subnetwork.edge
  values = {
    id        = "projects/example-project/regions/us-central1/subnetworks/pulsebridge-edge"
    self_link = "https://www.googleapis.com/compute/v1/projects/example-project/regions/us-central1/subnetworks/pulsebridge-edge"
  }
}

override_resource {
  target = module.project_base.google_compute_subnetwork.serverless
  values = {
    id        = "projects/example-project/regions/us-central1/subnetworks/pulsebridge-serverless"
    self_link = "https://www.googleapis.com/compute/v1/projects/example-project/regions/us-central1/subnetworks/pulsebridge-serverless"
  }
}

override_resource {
  target = module.healthcare.google_healthcare_dataset.this
  values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge" }
}

override_resource {
  target = module.healthcare.google_healthcare_fhir_store.this
  values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir" }
}

override_resource {
  target = module.healthcare.google_healthcare_fhir_store.audit
  values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir-audit" }
}

override_resource {
  target = module.healthcare.google_healthcare_hl7_v2_store.this
  values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2" }
}

override_resource {
  target = module.healthcare.google_pubsub_topic.ingest
  values = { id = "projects/example-project/topics/pulsebridge-hl7v2-ingest" }
}

override_resource {
  target = module.healthcare.google_pubsub_topic.dead_letter
  values = { id = "projects/example-project/topics/pulsebridge-hl7v2-ingest-dead-letter" }
}

override_resource {
  target = module.ingestion.google_cloud_run_v2_service.v2_to_fhir
  values = { uri = "https://pulsebridge-v2-to-fhir-abc123-uc.a.run.app" }
}

override_resource {
  target = module.ingestion.google_cloud_run_v2_service.health_sync
  values = { uri = "https://pulsebridge-health-sync-abc123-uc.a.run.app" }
}

override_resource {
  target = module.phr.google_cloud_run_v2_service.fhir_proxy
  values = { uri = "https://pulsebridge-fhir-proxy-abc123-uc.a.run.app" }
}

override_resource {
  target = module.phr.google_cloud_run_v2_service.cuff_sim
  values = { uri = "https://pulsebridge-cuff-sim-abc123-uc.a.run.app" }
}

override_resource {
  target = module.phr.google_firebase_hosting_site.dashboard
  values = { default_url = "https://example-project.web.app" }
}

override_resource {
  target = module.edge_vm.google_compute_address.internal
  values = { address = "10.10.0.10" }
}

override_resource {
  target = module.edge_vm.google_compute_disk.data
  values = {
    id        = "projects/example-project/zones/us-central1-a/disks/pulsebridge-edge-data"
    self_link = "https://www.googleapis.com/compute/v1/projects/example-project/zones/us-central1-a/disks/pulsebridge-edge-data"
  }
}

# ---- a real-data dev deployment; runs change what they test ---------------------------------
# Synthetic values only: no real project, patient, person or address.

variables {
  environment                         = "dev"
  protect_from_destroy                = false
  allow_reviewer_password_from_tfvars = true

  project_id           = "example-project"
  region               = "us-central1"
  zone                 = "us-central1-a"
  name_prefix          = "pulsebridge"
  identity_name_prefix = "pb"

  use_synthetic_data = false
  ingest_enabled     = false

  v2_to_fhir_image  = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir@sha256:1111111111111111111111111111111111111111111111111111111111111111"
  health_sync_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync@sha256:2222222222222222222222222222222222222222222222222222222222222222"
  seed_image        = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/seed@sha256:3333333333333333333333333333333333333333333333333333333333333333"
  fhir_proxy_image  = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
  cuff_sim_image    = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"

  patient_id                  = "PAT-0001"
  patient_assigning_authority = "EXAMPLE"
  patient_family_name         = "Examplefamily"
  patient_given_name          = "Examplegiven"
  time_zone                   = "America/New_York"

  alert_email = "alerts@example.com"

  create_build_service_account    = false
  build_trigger_approval_required = true

  mllp_port          = 2575
  openemr_https_port = 443
  enable_iap_ssh     = false
  enable_cloud_nat   = false
  # Deliberately NOT module phr's default (2): tests/plan_scan.py checks that this 1 reaches the
  # proxy, which a missing pass-through (module default) would fail.
  trusted_proxy_hops = 1
  # T41: null here; the real_ingest_on scenario sets it, and tests/plan_scan.py checks the grant
  # exists there only.
  page_deployer_service_account_email = null

  v2_to_fhir_ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  health_sync_ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"
}

# ---- T38: the service agents' grants, on a new project ----------------------------------------

# The first plan on a new project: identity's Pub/Sub service agent and healthcare's Healthcare
# agent are still to be created, yet the three grants' members are known (a plan-time assertion
# on an unknown value fails with "Unknown condition value"). A plan run changes no state, so this
# runs against the empty state, as a real first plan does.
run "new_project_agent_grant_members_known_at_plan" {
  command = plan

  assert {
    condition = module.healthcare.service_agent_grant_members == {
      healthcare_agent_publisher         = "serviceAccount:service-100000000001@gcp-sa-healthcare.iam.gserviceaccount.com"
      pubsub_agent_dead_letter_publisher = "serviceAccount:service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
      pubsub_agent_ingest_subscriber     = "serviceAccount:service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
    }
    error_message = "On a new project the agent grants' members must be known at plan and be the agents of the project number."
  }
}

# ---- scenario runs (also read by tests/plan_scan.py; keep the names in step) ---------------

run "real_ingest_off" {
  command = apply

  assert {
    condition     = output.ingest_enabled == false && output.push_enabled == false && output.scheduler_job_names == []
    error_message = "With ingest_enabled = false there must be no Scheduler job and the ingest subscription must not push (D11)."
  }
  assert {
    condition     = module.healthcare.push_enabled == module.ingestion.ingest_enabled
    error_message = "module healthcare's push switch must be module ingestion's ingest_enabled output (one switch for both)."
  }
  assert {
    condition     = output.health_sync_service_name == "pulsebridge-health-sync" && output.seed_job_name == null
    error_message = "Real data: health-sync and no seeder job."
  }
  assert {
    condition     = !contains(keys(module.identity.service_account_emails), "seeder")
    error_message = "Real data: no seeder service account (identity's create_seeder_account = use_synthetic_data)."
  }
  assert {
    condition = module.project_base.firewall_target == {
      type  = "service_account"
      value = "pb-edge-vm@example-project.iam.gserviceaccount.com"
    }
    error_message = "project-base must get the edge VM's account: its firewall rules target that account (and it grants the mirror read access)."
  }
  assert {
    condition     = module.project_base.dockerhub_mirror_url == "us-central1-docker.pkg.dev/example-project/pulsebridge-dockerhub"
    error_message = "The Docker Hub mirror must stay on: edge-vm and openemr pull through it."
  }
  assert {
    condition     = output.edge_vm.hook_metadata_keys == ["pulsebridge-hook-openemr", "pulsebridge-hook-openemr-oauth-client"]
    error_message = "edge-vm must get module openemr's two hooks."
  }
  assert {
    condition     = module.openemr.hook_timeout_minutes == 40
    error_message = "OpenEMR's first boot needs its 40-minute hook limit."
  }
  assert {
    condition     = output.openemr.site_address == "https://10.10.0.10" && output.edge_vm.internal_ip == "10.10.0.10"
    error_message = "module openemr must take the VM's reserved internal address."
  }
  assert {
    condition     = output.patient_wiring.ingestion == output.patient_wiring.phr && output.patient_wiring.phr == { id = "PAT-0001", assigning_authority = "EXAMPLE" }
    error_message = "ingestion and phr must both get the one patient_id and patient_assigning_authority."
  }
  assert {
    condition     = output.healthcare.dataset_id == "pulsebridge" && output.healthcare.fhir_store_id == "pulsebridge-fhir" && output.healthcare.location == "us-central1" && output.healthcare.project_id == "example-project"
    error_message = "The healthcare output must give the short IDs the connect script and the seeder take."
  }
  assert {
    condition = output.setup_env == join("\n", [
      "# PulseBridge dev: generated by `terraform output -raw setup_env`. No secret values.",
      "# Real data: the connect script writes the Patient with this PATIENT_ID.",
      "export GCP_PROJECT_ID='example-project'",
      "export HEALTHCARE_LOCATION='us-central1'",
      "export HEALTHCARE_DATASET_ID='pulsebridge'",
      "export FHIR_STORE_ID='pulsebridge-fhir'",
      "export PATIENT_ID='PAT-0001'",
      "export PULSEBRIDGE_NAME_PREFIX='pb'",
      "",
    ])
    error_message = "setup_env must be exactly the connect script's variables, from the stack's own values."
  }
  assert {
    condition     = output.secret_name_prefix == "pb" && output.secret_ids["google-health-refresh-token"] == "pb-google-health-refresh-token"
    error_message = "The secret prefix and names must match module identity's."
  }
  assert {
    condition     = strcontains(output.next_steps, "&& ../../../scripts/connect-google-health/connect.sh") && !strcontains(output.next_steps, "gcloud run jobs execute") && strcontains(output.next_steps, "10. Second apply")
    error_message = "Real data, ingest off: next_steps must say connect Google Health, then the second apply, and not the seeding."
  }
  assert {
    condition     = length(output.alert_policy_names) == 3
    error_message = "Real data with alert_email: one alert policy per metric (failed run, not connected, Scheduler attempt)."
  }
}

run "real_ingest_on" {
  command = apply

  variables {
    ingest_enabled = true
    # T41: tests/plan_scan.py checks roles/run.viewer on fhir-proxy for this account, here only.
    page_deployer_service_account_email = "page-deployer@example-project.iam.gserviceaccount.com"
  }

  assert {
    condition     = output.ingest_enabled && output.push_enabled && output.scheduler_job_names == ["pulsebridge-health-sync-every-15-min"]
    error_message = "ingest_enabled = true: the health-sync Scheduler job exists and the subscription pushes."
  }
  assert {
    condition     = !strcontains(output.next_steps, "Second apply")
    error_message = "Once ingest is on, next_steps must not ask for the second apply again."
  }
}

run "synthetic_ingest_off" {
  command = apply

  variables {
    use_synthetic_data  = true
    patient_id          = "SYN-0001"
    patient_family_name = null
    patient_given_name  = null
  }

  assert {
    condition     = output.seed_job_name == "pulsebridge-seed" && output.health_sync_service_name == null && output.scheduler_job_names == []
    error_message = "Synthetic data: the seeder job, no health-sync, and no Scheduler job while ingest is off."
  }
  assert {
    condition     = contains(keys(module.identity.service_account_emails), "seeder")
    error_message = "Synthetic data: the seeder service account exists."
  }
  assert {
    condition     = module.ingestion.health_sync_url == null
    error_message = "Synthetic data: no health-sync URL for module phr."
  }
  assert {
    condition     = strcontains(output.next_steps, "gcloud run jobs execute pulsebridge-seed --region us-central1 --project example-project") && strcontains(output.next_steps, "--args=--days,30") && strcontains(output.next_steps, "NEVER run the Google Health connect script")
    error_message = "Synthetic data: next_steps must give the first --days 30 seeding with this job's name, and warn against the connect script."
  }
  assert {
    condition     = strcontains(output.setup_env, "NEVER run the Google Health connect script with this SYN- patient ID") && strcontains(output.setup_env, "export PATIENT_ID='SYN-0001'")
    error_message = "Synthetic data: setup_env must carry the SYN- warning."
  }
  assert {
    condition     = length(output.alert_policy_names) == 2
    error_message = "Synthetic data with alert_email: two alert policies (failed seeding, Scheduler attempt)."
  }
}

run "synthetic_ingest_on" {
  command = apply

  variables {
    use_synthetic_data  = true
    ingest_enabled      = true
    patient_id          = "SYN-0001"
    patient_family_name = null
    patient_given_name  = null
  }

  assert {
    condition     = output.push_enabled && output.scheduler_job_names == ["pulsebridge-seed-hourly"]
    error_message = "Synthetic data, ingest on: the hourly seeder Scheduler job exists and the subscription pushes."
  }
}

run "prod_settings" {
  command = apply

  variables {
    environment                         = "prod"
    protect_from_destroy                = true
    allow_reviewer_password_from_tfvars = false
  }

  assert {
    condition     = output.environment == "prod" && strcontains(output.setup_env, "PulseBridge prod")
    error_message = "The environment name must reach the outputs."
  }
}

# ---- T38: an unrelated project-base change does not touch healthcare's agent grants --------

# These plan runs start from the state prod_settings applied (plan runs change no state) and
# repeat its settings, so the only difference is in project-base. Before T38 the stack gave module
# healthcare `depends_on = [module.project_base]`: healthcare's data.google_project was then read
# only at apply, the members became unknown, and the plan replaced all three grants (dev, 16
# November). The assertions below need the members at plan time and equal to what was applied.
# tests/plan_scan.py additionally checks these runs' plans: no healthcare resource is replaced
# and no data source is deferred.

run "enabling_an_api_keeps_the_agent_grants" {
  command = plan

  variables {
    environment                         = "prod"
    protect_from_destroy                = true
    allow_reviewer_password_from_tfvars = false
    enable_iap_ssh                      = true # one more API (iap) and one more firewall rule
  }

  assert {
    # Not vacuous: the plan really enables one more API in project-base.
    condition     = contains(module.project_base.enabled_apis, "iap.googleapis.com")
    error_message = "This run must change project-base (enable iap.googleapis.com) to test anything."
  }
  assert {
    condition = module.healthcare.service_agent_grant_members == {
      healthcare_agent_publisher         = "serviceAccount:service-100000000001@gcp-sa-healthcare.iam.gserviceaccount.com"
      pubsub_agent_dead_letter_publisher = "serviceAccount:service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
      pubsub_agent_ingest_subscriber     = "serviceAccount:service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
    }
    error_message = "Enabling an API must leave the agent grants' members known at plan and unchanged, or the grants are replaced."
  }
}

# Another project-base resource changing (Cloud NAT: a router and a NAT gateway, no new API).
run "another_project_base_change_keeps_the_agent_grants" {
  command = plan

  variables {
    environment                         = "prod"
    protect_from_destroy                = true
    allow_reviewer_password_from_tfvars = false
    enable_cloud_nat                    = true
  }

  assert {
    condition     = module.project_base.cloud_nat_enabled
    error_message = "This run must change project-base (add Cloud NAT) to test anything."
  }
  assert {
    condition = module.healthcare.service_agent_grant_members == {
      healthcare_agent_publisher         = "serviceAccount:service-100000000001@gcp-sa-healthcare.iam.gserviceaccount.com"
      pubsub_agent_dead_letter_publisher = "serviceAccount:service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
      pubsub_agent_ingest_subscriber     = "serviceAccount:service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
    }
    error_message = "A project-base change must leave the agent grants' members known at plan and unchanged."
  }

  # The stack's cost warning for Cloud NAT (checks.tf) fires, as it should.
  expect_failures = [check.cloud_nat_is_billed_hourly]
}

# ---- the reviewer password from tfvars: dev only -------------------------------------------

run "dev_may_set_reviewer_password_from_tfvars" {
  command = plan

  variables {
    reviewer_password            = "a-synthetic-test-password-0123"
    reviewer_password_wo_version = 1
  }

  assert {
    condition     = module.identity.reviewer_password_version_created
    error_message = "In dev a reviewer-password version may come from tfvars (write-only)."
  }
}

run "prod_refuses_reviewer_password_from_tfvars" {
  command = plan

  variables {
    environment                         = "prod"
    protect_from_destroy                = true
    allow_reviewer_password_from_tfvars = false
    reviewer_password                   = "a-synthetic-test-password-0123"
    reviewer_password_wo_version        = 1
  }

  expect_failures = [var.reviewer_password_wo_version]
}

# ---- images: a full plan needs them; the targeted first applies do not ----------------------

run "full_plan_without_images_is_refused" {
  command = plan

  variables {
    v2_to_fhir_image = null
    fhir_proxy_image = null
    cuff_sim_image   = null
  }

  expect_failures = [var.v2_to_fhir_image, var.fhir_proxy_image, var.cuff_sim_image]
}

# One run per targeted step of the runbook (infra/envs/README.md): each must plan before any image
# exists. A step that pulled in a module needing images would fail here on its image validation.

run "step_3_targeted_project_base_needs_no_images" {
  command = plan

  variables {
    v2_to_fhir_image  = null
    fhir_proxy_image  = null
    cuff_sim_image    = null
    health_sync_image = null
    seed_image        = null
  }

  plan_options {
    target = [module.project_base]
  }
}

run "step_4_targeted_identity_needs_no_images" {
  command = plan

  variables {
    v2_to_fhir_image  = null
    fhir_proxy_image  = null
    cuff_sim_image    = null
    health_sync_image = null
    seed_image        = null
  }

  plan_options {
    target = [module.identity]
  }
}

# Step 6 boots the VM (and registers fhir-proxy's OpenEMR client) before fhir-proxy exists.
run "step_6_targeted_edge_vm_needs_no_images" {
  command = plan

  variables {
    v2_to_fhir_image  = null
    fhir_proxy_image  = null
    cuff_sim_image    = null
    health_sync_image = null
    seed_image        = null
  }

  plan_options {
    target = [module.edge_vm]
  }
}

# ---- T26: the alert address is sensitive; image builds are opt-in ----------------------------

# The channel follows "is an address set", through a plain bool (the address itself is sensitive).
run "no_alert_address_means_no_email_channel" {
  command = plan

  variables {
    alert_email                 = null
    alert_notification_channels = ["projects/example-project/notificationChannels/123"]
  }

  assert {
    condition     = length(module.ingestion.alert_policy_names) > 0 && module.ingestion.alert_email_channel == null
    error_message = "With only a notification channel there must be alert policies but no email channel."
  }
}

run "no_build_account_or_trigger_by_default" {
  command = plan

  assert {
    condition = (
      !contains(keys(module.identity.service_account_emails), "build") &&
      output.build_service_account_email == null &&
      output.build_trigger_name == null &&
      length(module.project_base.build_account_registry_grants) == 0
    )
    error_message = "Image builds are opt-in: no build account, grant or trigger by default."
  }
}

run "image_builds_are_wired_when_enabled" {
  command = plan

  variables {
    create_build_service_account = true
    build_trigger_repository     = "projects/example-project/locations/us-central1/connections/github/repositories/pulsebridge"
  }

  assert {
    condition = (
      contains(keys(module.identity.service_account_emails), "build") &&
      module.project_base.build_account_registry_grants == tolist(["pulsebridge-dockerhub=roles/artifactregistry.reader", "pulsebridge-services=roles/artifactregistry.writer"]) &&
      output.build_trigger_name == "pulsebridge-images-on-tag" &&
      module.project_base.build_trigger_approval_required == true
    )
    error_message = "With create_build_service_account and build_trigger_repository the build account, its two repository grants and the trigger must exist."
  }
}

run "build_account_without_trigger" {
  command = plan

  variables {
    create_build_service_account = true
  }

  assert {
    condition     = length(module.project_base.build_account_registry_grants) == 2 && output.build_trigger_name == null
    error_message = "The build account (for manual builds) must not need the trigger."
  }
}

run "trigger_without_build_account_is_refused" {
  command = plan

  variables {
    build_trigger_repository = "projects/example-project/locations/us-central1/connections/github/repositories/pulsebridge"
  }

  expect_failures = [var.build_trigger_repository]
}

run "the_build_approval_reaches_the_trigger" {
  command = plan

  variables {
    create_build_service_account    = true
    build_trigger_repository        = "projects/example-project/locations/us-central1/connections/github/repositories/pulsebridge"
    build_trigger_approval_required = false
  }

  assert {
    condition     = module.project_base.build_trigger_approval_required == false
    error_message = "_stack must pass build_trigger_approval_required to project-base."
  }
}
