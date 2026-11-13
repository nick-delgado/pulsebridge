# Secret Manager secrets. A "secret" is only a named container; the value lives in a "secret
# version". Terraform creates the containers and their access policy here, and scripts add the
# versions after apply (see local.secrets[*].written_by), so no value is in code or state.
resource "google_secret_manager_secret" "this" {
  for_each = local.secrets

  project   = var.project_id
  secret_id = "${var.name_prefix}-${each.key}"

  labels = merge(
    {
      component  = "pulsebridge"
      managed-by = "terraform"
    },
    var.secret_labels,
  )

  annotations = {
    purpose = each.value.purpose
  }

  # Google chooses where to replicate the encrypted payload. A user-managed replica list would pin
  # the data to named regions; nothing here needs that.
  replication {
    auto {}
  }

  deletion_protection = var.secret_deletion_protection
}

# Read access, one binding per (secret, reader) pair. `_iam_member` is additive: it grants one
# role to one member on this one secret. The secret, not the project, is the resource, so
# health-sync can read the Google Health secrets and nothing else; a project-level
# secretAccessor grant would let every holder read every secret in the project.
resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each = local.secret_accessors

  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.this[each.value.workload].email}"
}

# The OpenEMR key writer grants (see local.secret_writer_grants): on that one secret, for edge-vm.
resource "google_secret_manager_secret_iam_member" "writer" {
  for_each = local.secret_writer_grants

  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.value.secret].id
  role      = each.value.role
  member    = "serviceAccount:${google_service_account.this[each.value.workload].email}"
}

# Dev only (decision D9): the first reviewer-password version, from tfvars.
#
# `secret_data_wo` is a write-only argument (Terraform 1.11+): Terraform passes the value to the
# provider during apply and then forgets it. It is never written to the state file or a saved plan,
# unlike `secret_data`, which would be stored in state as plain text. Because Terraform keeps no
# copy, it cannot tell when the value changes; `secret_data_wo_version` is the trigger, and a new
# number makes the provider write the new value as a new secret version.
resource "google_secret_manager_secret_version" "reviewer_password" {
  count = var.reviewer_password_wo_version > 0 ? 1 : 0

  secret                 = google_secret_manager_secret.this["reviewer-password"].id
  secret_data_wo         = var.reviewer_password
  secret_data_wo_version = tostring(var.reviewer_password_wo_version)
}
