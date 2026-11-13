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
  count = var.grant_pubsub_token_creator ? 1 : 0

  project_id = var.project_id
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  count = var.grant_pubsub_token_creator ? 1 : 0

  service_account_id = google_service_account.this["pubsub-push"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this[0].number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
