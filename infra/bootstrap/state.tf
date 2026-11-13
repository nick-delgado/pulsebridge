# The remote-state bucket. Every other root (infra/envs/dev, infra/envs/prod) stores its state
# here under its own prefix, using the `gcs` backend, which also provides state locking.

locals {
  state_bucket_name = coalesce(var.state_bucket_name, "${var.project_id}-tfstate")
}

resource "google_storage_bucket" "tfstate" {
  project       = local.project_id
  name          = local.state_bucket_name
  location      = var.state_bucket_location
  storage_class = "STANDARD"
  labels        = var.labels

  # Versioning: every write of a state file keeps the previous version as a "noncurrent" object.
  # A corrupted or wrongly applied state can then be restored instead of being lost.
  versioning {
    enabled = true
  }

  # Keep only the newest N noncurrent versions of each state file, so versioning does not make
  # the bucket grow without limit.
  lifecycle_rule {
    condition {
      num_newer_versions = var.state_noncurrent_versions_to_keep
      with_state         = "ARCHIVED" # ARCHIVED = noncurrent; the live version is never deleted
    }
    action {
      type = "Delete"
    }
  }

  # Access only through IAM (no per-object ACLs), and block any public grant outright:
  # state files can hold resource details that should never be world-readable.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Never delete objects (state files) as a side effect of deleting the bucket.
  force_destroy = false

  # prevent_destroy makes any plan that would destroy this bucket fail. Losing the bucket loses
  # every environment's state. To really remove it, delete this line first, on purpose.
  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.bootstrap]
}

# The deployer can already manage buckets through roles/storage.admin in var.deployer_roles.
# This bucket-scoped grant keeps state access working even if someone trims that list, and
# documents the one thing the deployer must always be able to do here.
resource "google_storage_bucket_iam_member" "deployer_state" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.deployer.member
}
