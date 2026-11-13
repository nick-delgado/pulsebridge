# AC: variables have validation blocks that reject bad input. Each run feeds one bad value and
# expects exactly that variable's validation to fail (expect_failures). If the validation were
# removed or loosened, the plan would succeed and the run would fail.

mock_provider "google" {
  source          = "./tests/mocks"
  override_during = plan
}

variables {
  create_project    = false
  project_id        = "example-project"
  github_repository = "example-owner/example-repo"
}

run "valid_baseline_passes" {
  command = plan
}

run "rejects_bad_project_id" {
  command = plan
  variables {
    project_id = "Example_Project"
  }
  expect_failures = [var.project_id]
}

run "rejects_create_without_billing" {
  command = plan
  variables {
    create_project = true
  }
  expect_failures = [var.billing_account]
}

run "rejects_billing_in_adopt_mode" {
  command = plan
  variables {
    billing_account = "000000-AAAAAA-111111"
  }
  expect_failures = [var.billing_account]
}

run "rejects_malformed_billing_account" {
  command = plan
  variables {
    create_project  = true
    billing_account = "not-a-billing-account"
  }
  expect_failures = [var.billing_account]
}

run "rejects_org_and_folder_together" {
  command = plan
  variables {
    create_project  = true
    billing_account = "000000-AAAAAA-111111"
    org_id          = "123"
    folder_id       = "456"
  }
  expect_failures = [var.org_id]
}

run "rejects_org_in_adopt_mode" {
  command = plan
  variables {
    org_id = "123"
  }
  expect_failures = [var.org_id]
}

run "rejects_project_name_in_adopt_mode" {
  command = plan
  variables {
    project_name = "Something"
  }
  expect_failures = [var.project_name]
}

run "rejects_owner_role_for_deployer" {
  command = plan
  variables {
    deployer_roles = ["roles/run.admin", "roles/owner"]
  }
  expect_failures = [var.deployer_roles]
}

run "rejects_malformed_role" {
  command = plan
  variables {
    deployer_roles = ["run.admin"]
  }
  expect_failures = [var.deployer_roles]
}

run "rejects_empty_roles" {
  command = plan
  variables {
    deployer_roles = []
  }
  expect_failures = [var.deployer_roles]
}

run "rejects_repository_without_owner" {
  command = plan
  variables {
    github_repository = "example-repo"
  }
  expect_failures = [var.github_repository]
}

run "rejects_repository_wildcard" {
  command = plan
  variables {
    github_repository = "example-owner/*"
  }
  expect_failures = [var.github_repository]
}

run "rejects_non_numeric_repository_id" {
  command = plan
  variables {
    github_repository_id = "example-owner/example-repo"
  }
  expect_failures = [var.github_repository_id]
}

run "rejects_reserved_pool_prefix" {
  command = plan
  variables {
    wif_pool_id = "gcp-github"
  }
  expect_failures = [var.wif_pool_id]
}

run "rejects_bad_bucket_name" {
  command = plan
  variables {
    state_bucket_name = "Has_Upper_Case"
  }
  expect_failures = [var.state_bucket_name]
}

run "rejects_zero_state_versions" {
  command = plan
  variables {
    state_noncurrent_versions_to_keep = 0
  }
  expect_failures = [var.state_noncurrent_versions_to_keep]
}

run "rejects_bad_deployer_account_id" {
  command = plan
  variables {
    deployer_account_id = "tf"
  }
  expect_failures = [var.deployer_account_id]
}

run "rejects_bad_label_key" {
  command = plan
  variables {
    labels = { "App" = "pulsebridge" }
  }
  expect_failures = [var.labels]
}
