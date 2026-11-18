# Two more CI identities (T26), both opt-in, so that the deployer's full rights are used only
# where something is applied:
#
#   planner        read-only. `terraform plan` on pull requests (dev) and in the prod-plan
#                  Environment (prod) runs as it, so a pull request's code, or prod's code before
#                  approval, never holds a token that can change anything. It can read the state
#                  and the CI tfvars (which hold the patient's ID and, in prod, name, and the alert
#                  address in clear) and the resources' configuration; no FHIR resources, HL7v2
#                  messages, log entries or secret values. It plans with -lock=false.
#   page deployer  roles/firebasehosting.admin and roles/serviceusage.serviceUsageViewer on the
#                  project, plus roles/run.viewer on fhir-proxy's service only (granted by module
#                  phr from the env roots, T41). The page deploy job runs the Firebase CLI and its
#                  npm dependencies; with this account a compromised dependency can at worst
#                  change the Hosting site, not the infrastructure.
#
# Like the deployer, neither gets a key; GitHub Actions impersonates them through Workload
# Identity Federation (wif.tf binds each to its own contexts).

resource "google_service_account" "planner" {
  count = var.create_planner ? 1 : 0

  project      = local.project_id
  account_id   = var.planner_account_id
  display_name = "Terraform planner (read-only)"
  description  = "Runs terraform plan for CI: reads resources, IAM policies, secret metadata (never secret values) and the state. Created by infra/bootstrap (T26)."

  depends_on = [google_project_service.bootstrap]
}

# The planner's one project role: a CUSTOM role with exactly what `terraform plan` needs to refresh
# the stack's resources (T26 review). Predefined viewer roles read too much: iam.securityReviewer
# includes healthcare.hl7V2Messages.list (message contents) and logging.privateLogEntries.list,
# compute.viewer includes the VM's serial port output and screenshots, logging.viewer reads log
# entries. Each permission below is the read (get) of one resource type the modules manage, or the
# IAM policy read (getIamPolicy) of a type that has *_iam_member resources; nothing lists or reads
# data. Creating a custom role needs iam.roles.create: bootstrap runs as the owner, who has it.
locals {
  planner_permissions = sort(distinct(concat([
    "resourcemanager.projects.get",          # data "google_project"; every provider call
    "resourcemanager.projects.getIamPolicy", # google_project_iam_member (identity)
    "serviceusage.services.get",             # google_project_service (project-base)
    "serviceusage.services.list",            # google_project_service refreshes by listing the enabled services (first CI run on dev)
    "serviceusage.services.use",             # google-beta bills its calls to the project (user_project_override)
    "iam.serviceAccounts.get",               # google_service_account (identity)
    "iam.serviceAccounts.getIamPolicy",      # google_service_account_iam_member (identity)
    "secretmanager.secrets.get",             # google_secret_manager_secret (metadata, never a value)
    "secretmanager.secrets.getIamPolicy",    # google_secret_manager_secret_iam_member
    "secretmanager.versions.get",            # google_secret_manager_secret_version (state of the dev version; not .access)
    "artifactregistry.repositories.get",     # google_artifact_registry_repository (project-base)
    "artifactregistry.repositories.getIamPolicy",
    "compute.networks.get",                # google_compute_network
    "compute.subnetworks.get",             # google_compute_subnetwork
    "compute.firewalls.get",               # google_compute_firewall
    "compute.routers.get",                 # google_compute_router, google_compute_router_nat
    "compute.addresses.get",               # google_compute_address (edge-vm)
    "compute.disks.get",                   # google_compute_disk, google_compute_disk_resource_policy_attachment
    "compute.instances.get",               # google_compute_instance (not getSerialPortOutput or getScreenshot)
    "compute.resourcePolicies.get",        # google_compute_resource_policy (prod's snapshot schedule)
    "healthcare.datasets.get",             # google_healthcare_dataset
    "healthcare.fhirStores.get",           # google_healthcare_fhir_store (configuration only)
    "healthcare.fhirStores.getIamPolicy",  # google_healthcare_fhir_store_iam_member
    "healthcare.hl7V2Stores.get",          # google_healthcare_hl7_v2_store (configuration only)
    "healthcare.hl7V2Stores.getIamPolicy", # google_healthcare_hl7_v2_store_iam_member
    "pubsub.topics.get",                   # google_pubsub_topic
    "pubsub.topics.getIamPolicy",          # google_pubsub_topic_iam_member
    "pubsub.subscriptions.get",            # google_pubsub_subscription
    "pubsub.subscriptions.getIamPolicy",   # google_pubsub_subscription_iam_member
    "run.services.get",                    # google_cloud_run_v2_service (its environment holds the patient ID, as the state does)
    "run.services.getIamPolicy",           # google_cloud_run_v2_service_iam_member
    "run.jobs.get",                        # google_cloud_run_v2_job (the seeder)
    "run.jobs.getIamPolicy",               # google_cloud_run_v2_job_iam_member
    "cloudscheduler.jobs.get",             # google_cloud_scheduler_job
    "logging.logMetrics.get",              # google_logging_metric (the metric's definition, not log entries)
    "monitoring.alertPolicies.get",        # google_monitoring_alert_policy
    "monitoring.notificationChannels.get", # google_monitoring_notification_channel (labels hold the alert address, as the state does)
    "firebasehosting.sites.get",           # google_firebase_hosting_site (phr)
    "firebase.projects.get",               # google_firebase_project, when phr manages it
    "cloudbuild.builds.get",               # google_cloudbuild_trigger (triggers.get needs builds.get)
  ], var.planner_extra_permissions)))
}

resource "google_project_iam_custom_role" "planner" {
  count = var.create_planner ? 1 : 0

  project     = local.project_id
  role_id     = var.planner_role_id
  title       = "Terraform planner (read-only)"
  description = "Reads the PulseBridge stack's resources and IAM policies for terraform plan; no data, no secret values. Created by infra/bootstrap (T26)."
  permissions = local.planner_permissions
  stage       = "GA"
}

resource "google_project_iam_member" "planner" {
  count = var.create_planner ? 1 : 0

  project = local.project_id
  role    = google_project_iam_custom_role.planner[0].name
  member  = google_service_account.planner[0].member
}

# Read the state (and the CI tfvars object next to it); no write, so no lock.
resource "google_storage_bucket_iam_member" "planner_state" {
  count = var.create_planner ? 1 : 0

  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.planner[0].member
}

resource "google_service_account" "page_deployer" {
  count = var.create_page_deployer ? 1 : 0

  project      = local.project_id
  account_id   = var.page_deployer_account_id
  display_name = "Dashboard page deployer"
  description  = "Deploys web/dist to Firebase Hosting from CI: Hosting admin and Service Usage Viewer on the project, Cloud Run Viewer on fhir-proxy only. Created by infra/bootstrap (T26, T41)."

  depends_on = [google_project_service.bootstrap]
}

# The page deployer's two project roles. Neither is a variable on purpose: anything broader would
# defeat the point of the account. Two separate resources rather than one for_each, so that the
# first one keeps the address google_project_iam_member.page_deployer[0] it had before T41 and an
# existing state needs no `moved` block.
#
# roles/firebasehosting.admin: create a Hosting version, upload the files, release it.
resource "google_project_iam_member" "page_deployer" {
  count = var.create_page_deployer ? 1 : 0

  project = local.project_id
  role    = "roles/firebasehosting.admin"
  member  = google_service_account.page_deployer[0].member
}

# roles/serviceusage.serviceUsageViewer (serviceusage.services.get and .list: read whether an API
# is enabled, nothing else). Before it deploys, the Firebase CLI checks that the Hosting API is
# enabled (GET serviceusage.googleapis.com/v1/projects/<p>/services/firebasehosting.googleapis.com);
# without this role that check is a 403 "Permission denied to get service" and the deploy stops
# (first CI run on dev, 18 November 2026). Hosting admin does not include it.
#
# The third grant the deploy needs, run.services.get on the fhir-proxy service that /api rewrites
# to, is NOT here: that service is created by the env root, so module phr grants roles/run.viewer
# on that one service (its input page_deployer_service_account_email).
resource "google_project_iam_member" "page_deployer_service_usage" {
  count = var.create_page_deployer ? 1 : 0

  project = local.project_id
  role    = "roles/serviceusage.serviceUsageViewer"
  member  = google_service_account.page_deployer[0].member
}
