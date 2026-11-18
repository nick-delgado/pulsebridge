# AC: a deployer service account and a Workload Identity Federation pool and provider restricted
# to one GitHub repository; outputs for the env roots and GitHub Actions. Mocked provider, plan only.

# google-beta (google_firebase_project) is mocked too, so no real provider ever runs.
mock_provider "google-beta" {}

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
    # T26: by default only the deployer is bound, and only to the "main" context.
    condition = keys(google_service_account_iam_member.github_wif) == ["deployer/main"] && (
      google_service_account_iam_member.github_wif["deployer/main"].role == "roles/iam.workloadIdentityUser" &&
      google_service_account_iam_member.github_wif["deployer/main"].service_account_id == google_service_account.deployer.name
    )
    error_message = "By default the pool's identities must be bound to the deployer only, from the main context, with roles/iam.workloadIdentityUser."
  }

  assert {
    condition     = google_service_account_iam_member.github_wif["deployer/main"].member == "principalSet://iam.googleapis.com/projects/111111111111/locations/global/workloadIdentityPools/github-actions/attribute.ci_context/main"
    error_message = "The deployer binding must match attribute.ci_context = main."
  }

  # The context folds in the repository ID (a foreign repository is "other-repository"), the
  # environment, the main ref and the event (pull_request_target must not read as main).
  assert {
    condition = google_iam_workload_identity_pool_provider.github.attribute_mapping["attribute.ci_context"] == join("", [
      "assertion.repository_id != \"123456789\" ? \"other-repository\" : ",
      "(\"environment\" in assertion) ? (\"env-\" + assertion.environment + ((assertion.ref == \"refs/heads/main\" && assertion.event_name in [\"push\", \"schedule\", \"workflow_dispatch\"]) ? \"-main\" : \"-other\")) : ",
      "assertion.event_name == \"pull_request\" ? \"pull-request\" : ",
      "(assertion.ref == \"refs/heads/main\" && assertion.event_name in [\"push\", \"schedule\", \"workflow_dispatch\"]) ? \"main\" : \"other\"",
    ])
    error_message = "attribute.ci_context must be mapped from the repository ID, environment, main ref and event exactly as documented."
  }

  assert {
    condition     = length(google_service_account.planner) == 0 && length(google_service_account.page_deployer) == 0 && output.planner_service_account_email == null && output.page_deployer_service_account_email == null
    error_message = "The planner and the page deployer are opt-in: none by default."
  }
}

# T26: the three CI identities as configured for prod, each bound only to its own contexts.
run "prod_identities_are_bound_to_their_environments" {
  command = plan

  variables {
    github_environments        = ["prod-plan", "prod", "prod-page"]
    github_allowed_refs        = ["refs/heads/main"]
    deployer_wif_contexts      = ["env-prod-main"]
    create_planner             = true
    planner_wif_contexts       = ["env-prod-plan-main"]
    create_page_deployer       = true
    page_deployer_wif_contexts = ["env-prod-page-main"]
  }

  override_resource {
    target          = google_service_account.planner[0]
    override_during = plan
    values = {
      email  = "terraform-planner@example-project.iam.gserviceaccount.com"
      member = "serviceAccount:terraform-planner@example-project.iam.gserviceaccount.com"
      name   = "projects/example-project/serviceAccounts/terraform-planner@example-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = google_service_account.page_deployer[0]
    override_during = plan
    values = {
      email  = "page-deployer@example-project.iam.gserviceaccount.com"
      member = "serviceAccount:page-deployer@example-project.iam.gserviceaccount.com"
      name   = "projects/example-project/serviceAccounts/page-deployer@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = {
      for k, b in google_service_account_iam_member.github_wif : k => "${b.service_account_id}|${b.member}"
      } == {
      "deployer/env-prod-main"           = "projects/example-project/serviceAccounts/terraform-deployer@example-project.iam.gserviceaccount.com|principalSet://iam.googleapis.com/projects/111111111111/locations/global/workloadIdentityPools/github-actions/attribute.ci_context/env-prod-main"
      "planner/env-prod-plan-main"       = "projects/example-project/serviceAccounts/terraform-planner@example-project.iam.gserviceaccount.com|principalSet://iam.googleapis.com/projects/111111111111/locations/global/workloadIdentityPools/github-actions/attribute.ci_context/env-prod-plan-main"
      "page-deployer/env-prod-page-main" = "projects/example-project/serviceAccounts/page-deployer@example-project.iam.gserviceaccount.com|principalSet://iam.googleapis.com/projects/111111111111/locations/global/workloadIdentityPools/github-actions/attribute.ci_context/env-prod-page-main"
    }
    error_message = "prod: the deployer only from env-prod-main, the planner only from env-prod-plan-main, the page deployer only from env-prod-page-main."
  }

  # Ref AND environment together in the pool's condition (B2).
  assert {
    condition     = google_iam_workload_identity_pool_provider.github.attribute_condition == "assertion.repository == \"example-owner/example-repo\" && assertion.repository_id == \"123456789\" && assertion.ref in [\"refs/heads/main\"] && assertion.environment in [\"prod-plan\", \"prod\", \"prod-page\"]"
    error_message = "github_allowed_refs and github_environments must both be in the condition."
  }

  # The planner reads, never writes, and reads no data (T26 review M1): one custom role whose
  # permissions are all get/getIamPolicy (plus serviceusage.services.use and .list, which names
  # the project's enabled APIs), none of the data-reading ones, and no predefined project role.
  assert {
    condition = (
      length(google_project_iam_member.planner) == 1 &&
      google_project_iam_member.planner[0].role == "projects/example-project/roles/pulsebridgeTerraformPlanner" &&
      google_project_iam_member.planner[0].member == "serviceAccount:terraform-planner@example-project.iam.gserviceaccount.com" &&
      google_project_iam_custom_role.planner[0].role_id == "pulsebridgeTerraformPlanner"
    )
    error_message = "The planner's only project role must be its custom role."
  }

  assert {
    condition = alltrue([
      for p in google_project_iam_custom_role.planner[0].permissions :
      can(regex("\\.(get|getIamPolicy)$", p)) || contains(["serviceusage.services.use", "serviceusage.services.list"], p)
    ])
    error_message = "The planner's custom role may hold only get, getIamPolicy, serviceusage.services.use and serviceusage.services.list."
  }

  assert {
    condition = length([
      for p in google_project_iam_custom_role.planner[0].permissions : p
      if can(regex("^(healthcare\\.(hl7V2Messages|fhirResources|dicom|consent)|healthcare\\..*\\.(export|import|ingest)$|logging\\.(privateLogEntries|logEntries)|secretmanager\\.versions\\.access$|storage\\.objects\\.|compute\\.instances\\.(getSerialPortOutput|getScreenshot)$)", p))
    ]) == 0
    error_message = "The planner must not read HL7v2 messages, FHIR resources, log entries, secret values, objects, or the VM's serial port or screen."
  }

  assert {
    # Every permission the stack's refresh needs (spot check of the ones a plan cannot do without).
    condition = length(setsubtract(toset([
      "resourcemanager.projects.getIamPolicy", "healthcare.fhirStores.getIamPolicy", "run.services.get",
      "compute.instances.get", "secretmanager.secrets.getIamPolicy", "serviceusage.services.use",
      "serviceusage.services.list",
    ]), toset(google_project_iam_custom_role.planner[0].permissions))) == 0
    error_message = "The planner's custom role is missing a permission the plan needs."
  }

  assert {
    condition     = google_storage_bucket_iam_member.planner_state[0].role == "roles/storage.objectViewer" && google_storage_bucket_iam_member.planner_state[0].member == "serviceAccount:terraform-planner@example-project.iam.gserviceaccount.com"
    error_message = "The planner may only read the state bucket (it plans with -lock=false)."
  }

  # The page deployer: Hosting and nothing else.
  assert {
    condition     = google_project_iam_member.page_deployer[0].role == "roles/firebasehosting.admin" && google_project_iam_member.page_deployer[0].member == "serviceAccount:page-deployer@example-project.iam.gserviceaccount.com" && length(google_project_iam_member.page_deployer) == 1
    error_message = "The page deployer must hold roles/firebasehosting.admin only."
  }

  assert {
    condition     = output.planner_service_account_email == "terraform-planner@example-project.iam.gserviceaccount.com" && output.page_deployer_service_account_email == "page-deployer@example-project.iam.gserviceaccount.com"
    error_message = "The two new accounts' emails must be outputs (repository variables)."
  }
}

run "dev_planner_from_pull_requests_deployer_from_main" {
  command = plan

  variables {
    create_planner       = true
    create_page_deployer = true
  }

  assert {
    condition     = toset(keys(google_service_account_iam_member.github_wif)) == toset(["deployer/main", "planner/pull-request", "page-deployer/env-dev-page-main"])
    error_message = "dev defaults: the deployer from main, the planner from pull requests, the page deployer only from its dev-page Environment."
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

# T26: the prod project's pool admits only jobs that reference a protected GitHub Environment.
run "wif_condition_requires_the_github_environment_when_given" {
  command = plan

  variables {
    github_environments        = ["prod-plan", "prod", "prod-page"]
    deployer_wif_contexts      = ["env-prod-main"]
    planner_wif_contexts       = ["env-prod-plan-main"]
    page_deployer_wif_contexts = ["env-prod-page-main"]
  }

  assert {
    condition     = google_iam_workload_identity_pool_provider.github.attribute_condition == "assertion.repository == \"example-owner/example-repo\" && assertion.repository_id == \"123456789\" && assertion.environment in [\"prod-plan\", \"prod\", \"prod-page\"]"
    error_message = "github_environments must add `assertion.environment in [...]` to the condition, and nothing else."
  }

  assert {
    condition     = google_iam_workload_identity_pool_provider.github.attribute_mapping["attribute.environment"] == "assertion.environment"
    error_message = "A condition field must be mapped: attribute.environment from assertion.environment."
  }
}

run "no_environment_clause_or_mapping_by_default" {
  command = plan

  assert {
    condition     = !strcontains(google_iam_workload_identity_pool_provider.github.attribute_condition, "environment") && !contains(keys(google_iam_workload_identity_pool_provider.github.attribute_mapping), "attribute.environment")
    error_message = "Without github_environments the condition and mapping must not mention the environment (pull-request plans have none)."
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
    # Module ingestion creates log-based metrics on every apply, and alert policies and an
    # email channel when asked; without these the deployer's apply fails there.
    condition = length(setsubtract(toset([
      "roles/logging.configWriter",
      "roles/monitoring.alertPolicyEditor",
      "roles/monitoring.notificationChannelEditor",
    ]), toset(keys(google_project_iam_member.deployer)))) == 0
    error_message = "The default deployer roles must include the logging and monitoring roles module ingestion needs."
  }

  assert {
    # Module phr's Hosting site and the page deploys need the Hosting role; the stack creates no
    # bucket, so no project-wide storage role (state access is the bucket-scoped grant).
    condition     = contains(keys(google_project_iam_member.deployer), "roles/firebasehosting.admin") && !contains(keys(google_project_iam_member.deployer), "roles/storage.admin")
    error_message = "The default deployer roles must include roles/firebasehosting.admin and not roles/storage.admin."
  }
  assert {
    # T26: no Cloud Build role by default; allow_build_trigger adds it (run below).
    condition     = length([for r in keys(google_project_iam_member.deployer) : r if startswith(r, "roles/cloudbuild.")]) == 0
    error_message = "The default deployer roles must not include a Cloud Build role."
  }
  assert {
    # Adding Firebase is bootstrap's job (add_firebase), so the deployer gets no broad Firebase role.
    condition     = length(setintersection(toset(keys(google_project_iam_member.deployer)), toset(["roles/firebase.admin", "roles/firebase.editor"]))) == 0
    error_message = "The deployer must not get roles/firebase.admin or roles/firebase.editor."
  }

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

run "build_trigger_role_only_when_opted_in" {
  command = plan

  variables {
    allow_build_trigger = true
  }

  assert {
    condition     = [for r in keys(google_project_iam_member.deployer) : r if startswith(r, "roles/cloudbuild.")] == ["roles/cloudbuild.builds.editor"]
    error_message = "allow_build_trigger must add roles/cloudbuild.builds.editor and no other Cloud Build role."
  }
}
