# Driven by tests/state_scan.py, which supplies a fresh random dummy password with
# `-var reviewer_password=...` and runs this directory with `-test-directory=tests/state_scan`
# (so a plain `terraform test` does not pick it up without the value). The script captures the
# state after each run from `terraform test -json -verbose` and searches it.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "100000000001" # synthetic project number
    }
  }
}

# Same reason as in tests/service_accounts.tftest.hcl: the Token Creator binding needs a
# well-formed account name, and the mock's is random.
override_resource {
  target = google_service_account.this["pubsub-push"]
  values = {
    name  = "projects/example-project-01/serviceAccounts/pb-pubsub-push@example-project-01.iam.gserviceaccount.com"
    email = "pb-pubsub-push@example-project-01.iam.gserviceaccount.com"
  }
}

# Supplied by tests/state_scan.py through TF_VAR_reviewer_password; declared here so the control
# run can pass the same value to its helper module.
variable "reviewer_password" {
  type      = string
  sensitive = true
}

variables {
  project_id = "example-project-01"
}

# Module defaults (the password is supplied but the version switch is off): the scan checks that
# no secret version exists at all.
run "defaults" {
  command = apply
}

# Everything switched on, including the dev reviewer password through secret_data_wo: the scan
# checks the dummy value is absent and that only allowed resource types exist.
run "dev_password" {
  command = apply

  variables {
    reviewer_password_wo_version = 1
    create_seeder_account        = true
    grant_pubsub_token_creator   = true
  }

  assert {
    condition     = length(google_secret_manager_secret_version.reviewer_password) == 1
    error_message = "The dev run must create the reviewer-password version, or the scan proves nothing."
  }
}

# Positive control: the same dummy value stored through `secret_data`. The scan must find it.
run "control" {
  command = apply

  module {
    source = "./tests/control"
  }

  variables {
    value = var.reviewer_password
  }
}
