# AC: a deployer service account and a Workload Identity Federation pool and provider restricted
# to one GitHub repository; outputs for the env roots and GitHub Actions. Mocked provider, plan only.

mock_provider "google" {
  source          = "./tests/mocks"
  override_during = plan
}

variables {
  create_project       = false
  project_id           = "example-project"
  github_repository    = "example-owner/example-repo"
  github_repository_id = "123456789"
}

run "wif_provider_is_pinned_to_one_repository" {
  command = plan

  assert {
    # Exact string: equality on BOTH the repository name and its immutable numeric ID, nothing
    # looser (no startsWith on the owner, no `true`, no name-only match).
    condition     = google_iam_workload_identity_pool_provider.github.attribute_condition == "assertion.repository == \"example-owner/example-repo\" && assertion.repository_id == \"123456789\""
    error_message = "The attribute condition must pin assertion.repository and assertion.repository_id exactly."
  }

  assert {
    condition     = google_iam_workload_identity_pool_provider.github.oidc[0].issuer_uri == "https://token.actions.githubusercontent.com"
    error_message = "The provider must trust GitHub Actions' OIDC issuer."
  }

  assert {
    condition = (
      google_iam_workload_identity_pool_provider.github.attribute_mapping["google.subject"] == "assertion.sub" &&
      google_iam_workload_identity_pool_provider.github.attribute_mapping["attribute.repository"] == "assertion.repository" &&
      google_iam_workload_identity_pool_provider.github.attribute_mapping["attribute.repository_id"] == "assertion.repository_id" &&
      google_iam_workload_identity_pool_provider.github.attribute_mapping["attribute.repository_owner_id"] == "assertion.repository_owner_id"
    )
    error_message = "google.subject, attribute.repository, attribute.repository_id and attribute.repository_owner_id must be mapped from GitHub's claims."
  }

  assert {
    condition = (
      google_service_account_iam_member.deployer_github_wif.role == "roles/iam.workloadIdentityUser" &&
      google_service_account_iam_member.deployer_github_wif.service_account_id == google_service_account.deployer.name
    )
    error_message = "The pool's identities must be bound to the deployer with roles/iam.workloadIdentityUser."
  }

  assert {
    # Keyed on the immutable repository ID, so the binding is never weaker than the condition.
    condition     = google_service_account_iam_member.deployer_github_wif.member == "principalSet://iam.googleapis.com/projects/111111111111/locations/global/workloadIdentityPools/github-actions/attribute.repository_id/123456789"
    error_message = "Only identities from the repository with ID var.github_repository_id may impersonate the deployer."
  }
}

run "wif_condition_adds_owner_id_and_refs_when_given" {
  command = plan

  variables {
    github_repository_owner_id = "987654"
    github_allowed_refs        = ["refs/heads/main", "refs/tags/v1.0.0"]
  }

  assert {
    condition     = google_iam_workload_identity_pool_provider.github.attribute_condition == "assertion.repository == \"example-owner/example-repo\" && assertion.repository_id == \"123456789\" && assertion.repository_owner_id == \"987654\" && assertion.ref in [\"refs/heads/main\", \"refs/tags/v1.0.0\"]"
    error_message = "github_repository_owner_id and github_allowed_refs must each add their clause to the condition."
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
