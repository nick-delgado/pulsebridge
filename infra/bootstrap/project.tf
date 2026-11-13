# The create-or-adopt switch.
#
# `count` on a resource or data source decides how many instances of it exist: 1 or 0. With
# create_project = true the google_project resource exists (count = 1) and the data source does
# not (count = 0); with false it is the other way round. Because a counted block is a list,
# it is addressed as google_project.this[0], and the locals below pick whichever one exists, so
# everything after this file only ever refers to local.project_id / local.project_number and does
# not care which mode ran.

resource "google_project" "this" {
  count = var.create_project ? 1 : 0

  project_id = var.project_id
  name       = coalesce(var.project_name, var.project_id)

  # Linking billing here (rather than with a separate google_billing_project_info resource)
  # means the project never exists without billing, so the API enablement below cannot fail
  # for lack of it.
  billing_account = var.billing_account

  # Both null (the default) means "no parent": a project owned by a personal Google account,
  # which needs no organization.
  org_id    = var.org_id
  folder_id = var.folder_id

  # false: the provider enables the Compute Engine API and deletes the auto-created "default"
  # network, whose firewall rules allow SSH/RDP from anywhere. module project-base builds the
  # stack's own VPC.
  auto_create_network = var.auto_create_network

  # PREVENT (the provider's default, stated here so a reader sees it): `terraform destroy` on
  # bootstrap will refuse to delete the project and everything in it. Deleting a project is a
  # deliberate console action, not a side effect.
  deletion_policy = "PREVENT"

  labels = var.labels
}

# Adopt mode: read the existing project, mainly to get its number (the numeric ID that some
# IAM member strings and the Workload Identity provider's resource name use). A data source
# only reads; it never creates, changes or deletes anything.
data "google_project" "existing" {
  count = var.create_project ? 0 : 1

  project_id = var.project_id
}

locals {
  project_id     = var.create_project ? google_project.this[0].project_id : data.google_project.existing[0].project_id
  project_number = var.create_project ? google_project.this[0].number : data.google_project.existing[0].number

  # Only the APIs bootstrap itself calls. The full stack's APIs are enabled by module
  # project-base, run later by the deployer account (which holds serviceUsageAdmin for that).
  bootstrap_apis = toset([
    "cloudresourcemanager.googleapis.com", # read the project, set project IAM policy
    "serviceusage.googleapis.com",         # enable APIs (this list, and project-base's later)
    "iam.googleapis.com",                  # service account, Workload Identity pool and provider
    "iamcredentials.googleapis.com",       # GitHub Actions mints deployer access tokens here
    "sts.googleapis.com",                  # Security Token Service: swaps GitHub's OIDC token for a Google token
    "storage.googleapis.com",              # the state bucket
  ])
}

resource "google_project_service" "bootstrap" {
  for_each = local.bootstrap_apis

  project = local.project_id
  service = each.value

  # Destroying bootstrap must not switch off APIs the rest of the stack depends on.
  disable_on_destroy = false
}
