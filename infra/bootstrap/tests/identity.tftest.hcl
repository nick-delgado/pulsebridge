# AC: a deployer service account and a Workload Identity Federation pool and provider restricted
# to one GitHub repository; outputs for the env roots and GitHub Actions. Mocked provider, plan only.

mock_provider "google" {
  source          = "./tests/mocks"
  override_during = plan
}

variables {
  create_project    = false
  project_id        = "example-project"
  github_repository = "example-owner/example-repo"
}

run "wif_provider_is_pinned_to_one_repository" {
  command = plan

  assert {
    # Exact string: the condition must be an equality on the repository claim, nothing looser
    # (no startsWith on the owner, no `true`).
    condition     = google_iam_workload_identity_pool_provider.github.attribute_condition == "assertion.repository == \"example-owner/example-repo\""
    error_message = "The attribute condition must pin assertion.repository to var.github_repository exactly."
  }

  assert {
    condition     = google_iam_workload_identity_pool_provider.github.oidc[0].issuer_uri == "https://token.actions.githubusercontent.com"
    error_message = "The provider must trust GitHub Actions' OIDC issuer."
  }

  assert {
    condition = (
      google_iam_workload_identity_pool_provider.github.attribute_mapping["google.subject"] == "assertion.sub" &&
      google_iam_workload_identity_pool_provider.github.attribute_mapping["attribute.repository"] == "assertion.repository"
    )
    error_message = "google.subject and attribute.repository must be mapped from GitHub's claims."
  }

  assert {
    condition = (
      google_service_account_iam_member.deployer_github_wif.role == "roles/iam.workloadIdentityUser" &&
      google_service_account_iam_member.deployer_github_wif.service_account_id == google_service_account.deployer.name
    )
    error_message = "The pool's identities must be bound to the deployer with roles/iam.workloadIdentityUser."
  }

  assert {
    condition     = google_service_account_iam_member.deployer_github_wif.member == "principalSet://iam.googleapis.com/projects/111111111111/locations/global/workloadIdentityPools/github-actions/attribute.repository/example-owner/example-repo"
    error_message = "Only identities from var.github_repository may impersonate the deployer."
  }
}

run "wif_condition_also_pins_repository_id_when_given" {
  command = plan

  variables {
    github_repository_id = "123456789"
  }

  assert {
    condition     = google_iam_workload_identity_pool_provider.github.attribute_condition == "assertion.repository == \"example-owner/example-repo\" && assertion.repository_id == \"123456789\""
    error_message = "With github_repository_id set, the condition must pin both the name and the numeric ID."
  }
}

run "deployer_gets_exactly_the_configured_roles" {
  command = plan

  variables {
    deployer_roles = ["roles/run.admin", "roles/pubsub.admin"]
  }

  assert {
    condition     = toset(keys(google_project_iam_member.deployer)) == toset(["roles/run.admin", "roles/pubsub.admin"])
    error_message = "The deployer must get exactly var.deployer_roles at project level."
  }

  assert {
    condition     = alltrue([for b in google_project_iam_member.deployer : b.member == google_service_account.deployer.member && b.project == "example-project"])
    error_message = "Every role binding must be for the deployer, on the bootstrapped project."
  }
}

run "default_deployer_roles_are_not_basic_roles" {
  command = plan

  assert {
    condition     = length(setintersection(toset(keys(google_project_iam_member.deployer)), toset(["roles/owner", "roles/editor", "roles/viewer"]))) == 0
    error_message = "The default deployer roles must not include a basic role."
  }

  assert {
    # Spot-check that the default can actually build the stack's core pieces.
    condition = length(setsubtract(toset([
      "roles/run.admin",
      "roles/healthcare.fhirStoreAdmin",
      "roles/healthcare.hl7V2StoreAdmin",
      "roles/secretmanager.admin",
      "roles/serviceusage.serviceUsageAdmin",
      "roles/iam.serviceAccountUser",
    ]), toset(keys(google_project_iam_member.deployer)))) == 0
    error_message = "The default deployer roles are missing a role the stack needs."
  }
}

run "outputs_carry_what_consumers_need" {
  command = plan

  assert {
    condition     = output.deployer_service_account_email == "terraform-deployer@example-project.iam.gserviceaccount.com"
    error_message = "deployer_service_account_email must be the deployer's email."
  }

  assert {
    condition     = output.workload_identity_provider == "projects/111111111111/locations/global/workloadIdentityPools/github-actions/providers/github-oidc"
    error_message = "workload_identity_provider must be the provider's full resource name (what google-github-actions/auth expects)."
  }

  assert {
    condition     = google_service_account.deployer.account_id == "terraform-deployer"
    error_message = "The deployer account_id must default to terraform-deployer."
  }
}
