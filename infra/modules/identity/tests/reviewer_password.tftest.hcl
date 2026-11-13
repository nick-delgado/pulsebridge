# The optional dev reviewer-password version (D9) and its input rules. Mock provider only.
# The value below is a synthetic placeholder, not a real password. The full state search lives in
# tests/state_scan.py, which also proves the search can find a value that does reach state.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "100000000001" # synthetic project number
    }
  }
}

# The Pub/Sub Token Creator grant is on by default and the provider checks that its
# service_account_id looks like a real account name, so the push account gets a synthetic,
# well-formed name instead of the mock's random string.
override_resource {
  target = google_service_account.this["pubsub-push"]
  values = {
    name  = "projects/example-project-01/serviceAccounts/pb-pubsub-push@example-project-01.iam.gserviceaccount.com"
    email = "pb-pubsub-push@example-project-01.iam.gserviceaccount.com"
  }
}

variables {
  project_id = "example-project-01"
}

run "no_version_without_the_switch" {
  command = apply

  variables {
    # A password alone does nothing: the version number is the switch, so prod (which never sets
    # the number) cannot get a version from a stray tfvars line.
    reviewer_password = "placeholder-not-a-real-password"
  }

  assert {
    condition     = length(google_secret_manager_secret_version.reviewer_password) == 0
    error_message = "No reviewer-password version may be created while reviewer_password_wo_version is 0."
  }
}

run "dev_version_uses_write_only_argument" {
  command = apply

  variables {
    reviewer_password            = "placeholder-not-a-real-password"
    reviewer_password_wo_version = 1
  }

  assert {
    condition     = google_secret_manager_secret_version.reviewer_password[0].secret == google_secret_manager_secret.this["reviewer-password"].id
    error_message = "The dev version must be added to the reviewer-password secret."
  }

  # After apply, write-only attributes are null in state; `secret_data` (the stored-in-state
  # argument) must not be used at all.
  assert {
    condition     = google_secret_manager_secret_version.reviewer_password[0].secret_data_wo == null
    error_message = "secret_data_wo must not be kept in state."
  }

  assert {
    condition     = google_secret_manager_secret_version.reviewer_password[0].secret_data == null
    error_message = "The password must be sent through secret_data_wo, never secret_data (which is stored in state)."
  }

  assert {
    condition     = google_secret_manager_secret_version.reviewer_password[0].secret_data_wo_version == "1"
    error_message = "secret_data_wo_version must follow reviewer_password_wo_version, so raising it writes a new version."
  }

  assert {
    condition     = output.reviewer_password_version_created
    error_message = "reviewer_password_version_created must report the dev version."
  }
}

# Rotation in dev: raising the number is what makes Terraform send a new value, because it keeps
# no copy of the old one to compare with.
run "raising_the_version_number_rotates" {
  command = apply

  variables {
    reviewer_password            = "another-placeholder-not-real"
    reviewer_password_wo_version = 2
  }

  assert {
    condition     = google_secret_manager_secret_version.reviewer_password[0].secret_data_wo_version == "2"
    error_message = "secret_data_wo_version must follow reviewer_password_wo_version."
  }
}

run "version_without_password_is_rejected" {
  command = plan

  variables {
    reviewer_password_wo_version = 1
  }

  expect_failures = [var.reviewer_password_wo_version]
}

run "short_password_is_rejected" {
  command = plan

  variables {
    reviewer_password            = substr("placeholder-not-a-real-password", 0, 15) # 15 characters
    reviewer_password_wo_version = 1
  }

  expect_failures = [var.reviewer_password]
}

run "empty_password_is_rejected_with_the_length_rule" {
  command = plan

  variables {
    reviewer_password            = ""
    reviewer_password_wo_version = 1
  }

  expect_failures = [var.reviewer_password]
}

run "sixteen_characters_is_accepted" {
  command = plan

  variables {
    reviewer_password            = substr("placeholder-not-a-real-password", 0, 16) # 16 characters, the minimum
    reviewer_password_wo_version = 1
  }

  assert {
    condition     = length(google_secret_manager_secret_version.reviewer_password) == 1
    error_message = "A 16-character password with the switch on must plan one version."
  }
}

run "fractional_version_is_rejected" {
  command = plan

  variables {
    reviewer_password            = "placeholder-not-a-real-password"
    reviewer_password_wo_version = 1.5
  }

  expect_failures = [var.reviewer_password_wo_version]
}
