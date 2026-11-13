# AC: create_project = true creates a project and links billing; false adopts an existing one.
# No organization is required.
#
# Every test file uses a mocked google provider: Terraform loads the real provider's schema but
# makes no API calls, so these runs need no credentials and touch no project. `command = plan`
# never applies anything; override_during = plan makes the mocked computed values (project
# numbers, resource names) known at plan time so they can be asserted on.

mock_provider "google" {
  source          = "./tests/mocks"
  override_during = plan
}

variables {
  project_id        = "example-project"
  github_repository = "example-owner/example-repo"
}

run "create_mode_creates_project_and_links_billing" {
  command = plan

  variables {
    create_project  = true
    billing_account = "000000-AAAAAA-111111"
  }

  assert {
    condition     = length(google_project.this) == 1
    error_message = "create_project = true must create exactly one google_project."
  }

  assert {
    condition     = length(data.google_project.existing) == 0
    error_message = "create_project = true must not also read an existing project."
  }

  assert {
    condition     = google_project.this[0].project_id == "example-project"
    error_message = "The created project must use var.project_id."
  }

  assert {
    condition     = google_project.this[0].billing_account == "000000-AAAAAA-111111"
    error_message = "The created project must be linked to var.billing_account."
  }

  assert {
    # No organization: a personal account's project has no parent.
    condition     = google_project.this[0].org_id == null && google_project.this[0].folder_id == null
    error_message = "With org_id and folder_id unset the project must have no parent (no organization required)."
  }

  assert {
    condition     = google_project.this[0].auto_create_network == false
    error_message = "By default the open-to-the-internet default network must not be kept."
  }

  assert {
    condition     = google_project.this[0].deletion_policy == "PREVENT"
    error_message = "The created project must be protected from terraform destroy."
  }

  assert {
    condition     = google_project.this[0].name == "example-project"
    error_message = "project_name must default to project_id."
  }

  assert {
    # The number must come from the created project (mock 111...), not the data source (222...).
    condition     = output.project_id == "example-project" && output.project_number == "111111111111"
    error_message = "In create mode the project outputs must come from the created project."
  }
}

run "create_mode_under_optional_folder" {
  command = plan

  variables {
    create_project  = true
    billing_account = "000000-AAAAAA-111111"
    folder_id       = "123456789"
    project_name    = "PulseBridge demo"
  }

  assert {
    condition     = google_project.this[0].folder_id == "123456789" && google_project.this[0].org_id == null
    error_message = "folder_id must be passed through as the project's parent."
  }

  assert {
    condition     = google_project.this[0].name == "PulseBridge demo"
    error_message = "project_name must be used as the display name when set."
  }
}

run "adopt_mode_reads_existing_project_and_creates_none" {
  command = plan

  variables {
    create_project = false
  }

  assert {
    condition     = length(google_project.this) == 0
    error_message = "create_project = false must not create a project (and so cannot touch billing)."
  }

  assert {
    condition     = length(data.google_project.existing) == 1 && data.google_project.existing[0].project_id == "example-project"
    error_message = "create_project = false must read the existing project var.project_id."
  }

  assert {
    condition     = output.project_id == "example-project" && output.project_number == "222222222222"
    error_message = "In adopt mode the project outputs must come from the existing project."
  }

  assert {
    condition     = google_storage_bucket.tfstate.project == "example-project" && google_service_account.deployer.project == "example-project"
    error_message = "Bootstrap resources must be created in the adopted project."
  }
}

run "only_bootstrap_apis_are_enabled" {
  command = plan

  variables {
    create_project = false
  }

  assert {
    condition = toset(keys(google_project_service.bootstrap)) == toset([
      "cloudresourcemanager.googleapis.com",
      "serviceusage.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "sts.googleapis.com",
      "storage.googleapis.com",
    ])
    error_message = "Bootstrap must enable exactly the APIs it needs; the stack's APIs belong to module project-base."
  }

  assert {
    condition     = alltrue([for s in google_project_service.bootstrap : s.disable_on_destroy == false && s.project == "example-project"])
    error_message = "Bootstrap APIs must be enabled on the target project and never disabled on destroy."
  }
}
