# Service accounts: one per workload, so a compromised service can do only what its own job needs
# and audit logs show which workload made each call. No `google_service_account_key` anywhere:
# Cloud Run, Compute Engine, Cloud Scheduler and Pub/Sub all obtain short-lived tokens for these
# accounts from Google, so no long-lived key file ever exists.
resource "google_service_account" "this" {
  for_each = local.workloads

  project      = var.project_id
  account_id   = "${var.name_prefix}-${each.key}"
  display_name = each.value.display_name
  description  = each.value.description
}

# Project-level grants (see local.project_roles). `google_project_iam_member` is additive: it adds
# one member to one role and leaves every other grant in the project alone. The authoritative
# flavours (`google_project_iam_binding`, `google_project_iam_policy`) would remove grants made by
# other modules or by hand, which in a project policy can lock the deployer out.
resource "google_project_iam_member" "this" {
  for_each = local.project_role_bindings

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.this[each.value.workload].email}"
}

# Pub/Sub signs the OIDC token for an authenticated push subscription as the pubsub-push account.
# In projects created before 8 April 2021 its service agent needs Token Creator for that; the
# grant is on the pubsub-push account only (a resource-level binding on a resource this module owns).
data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Google's documented address of a project's Pub/Sub service agent. Built from the project
  # number, which the data source reads at plan time, so the grant's `member` is KNOWN in the
  # plan. Using google_project_service_identity.pubsub's `member` attribute instead would make it
  # "(known after apply)" whenever that resource is new, and because `member` forces replacement,
  # a project where the grant already exists would see it destroyed and re-created for nothing.
  # The postcondition below checks that the address Google returns is this one.
  pubsub_service_agent_email = "service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# A service agent (a Google-managed service account that a Google service acts as inside your
# project) is not guaranteed to exist just because its API is enabled: Google creates some of
# them lazily, on first use. On a brand-new project (first dev deploy, 16 November 2026) the
# Pub/Sub one did not exist yet, and the grant below failed with "Service account
# service-<number>@gcp-sa-pubsub.iam.gserviceaccount.com does not exist". This resource calls
# Service Usage's `services.generateServiceIdentity` (v1beta1), which creates the agent if it is
# missing and returns it if it exists, so it is safe on old and new projects alike.
#
# Created whatever grant_pubsub_token_creator says: module healthcare also grants this agent
# roles (dead-letter publisher, subscriber), and it costs nothing. Beta provider only: the
# resource exists only in google-beta. It cannot be updated or destroyed (a destroy only drops it
# from state; the agent stays, as every service agent does), and it does not support import.
resource "google_project_service_identity" "pubsub" {
  provider = google-beta

  project = var.project_id
  service = "pubsub.googleapis.com"

  lifecycle {
    postcondition {
      # An empty or null email proves nothing either way (the API does not promise one for every
      # service), so only a different address fails the apply.
      condition     = self.email == null ? true : contains(["", local.pubsub_service_agent_email], self.email)
      error_message = "The Pub/Sub service agent Google returned is not service-<project number>@gcp-sa-pubsub.iam.gserviceaccount.com, the address the Token Creator grant (and module healthcare) assume."
    }
  }
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  count = var.grant_pubsub_token_creator ? 1 : 0

  service_account_id = google_service_account.this["pubsub-push"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${local.pubsub_service_agent_email}"

  # The member string does not reference the service identity (see the local above), so the
  # order has to be stated: the agent must exist before IAM accepts it as a member.
  depends_on = [google_project_service_identity.pubsub]
}
