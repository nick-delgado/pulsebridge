# AC: a versioned GCS state bucket, with public access prevented. Mocked provider, plan only.

mock_provider "google" {
  source          = "./tests/mocks"
  override_during = plan
}

variables {
  create_project    = false
  project_id        = "example-project"
  github_repository = "example-owner/example-repo"
}

run "bucket_is_versioned_private_and_protected" {
  command = plan

  assert {
    condition     = google_storage_bucket.tfstate.versioning[0].enabled == true
    error_message = "The state bucket must have object versioning enabled."
  }

  assert {
    condition     = google_storage_bucket.tfstate.public_access_prevention == "enforced"
    error_message = "The state bucket must enforce public access prevention."
  }

  assert {
    condition     = google_storage_bucket.tfstate.uniform_bucket_level_access == true
    error_message = "The state bucket must use uniform bucket-level access (IAM only, no object ACLs)."
  }

  assert {
    condition     = google_storage_bucket.tfstate.force_destroy == false
    error_message = "Deleting the bucket must never silently delete state objects."
  }

  assert {
    condition = (
      length(google_storage_bucket.tfstate.lifecycle_rule) == 1 &&
      one(google_storage_bucket.tfstate.lifecycle_rule[0].action).type == "Delete" &&
      one(google_storage_bucket.tfstate.lifecycle_rule[0].condition).with_state == "ARCHIVED" &&
      one(google_storage_bucket.tfstate.lifecycle_rule[0].condition).num_newer_versions == 10
    )
    error_message = "Only noncurrent (ARCHIVED) state versions beyond the newest 10 may be deleted."
  }

  assert {
    condition     = google_storage_bucket.tfstate.name == "example-project-tfstate" && output.state_bucket_name == "example-project-tfstate"
    error_message = "The bucket name must default to <project_id>-tfstate and be output."
  }

  assert {
    condition     = google_storage_bucket.tfstate.location == "us-central1"
    error_message = "The bucket must default to a free-tier region."
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.deployer_state.role == "roles/storage.objectAdmin" &&
      google_storage_bucket_iam_member.deployer_state.member == google_service_account.deployer.member
    )
    error_message = "The deployer must be able to read and write state objects in the bucket."
  }
}

run "bucket_name_and_retention_are_configurable" {
  command = plan

  variables {
    state_bucket_name                 = "someone-elses-state"
    state_noncurrent_versions_to_keep = 3
  }

  assert {
    condition     = google_storage_bucket.tfstate.name == "someone-elses-state"
    error_message = "state_bucket_name must override the default name."
  }

  assert {
    condition     = one(google_storage_bucket.tfstate.lifecycle_rule[0].condition).num_newer_versions == 3
    error_message = "state_noncurrent_versions_to_keep must drive the lifecycle rule."
  }
}
