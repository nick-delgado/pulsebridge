# Service accounts and project-level roles. Runs with a mock provider: no credentials, no cloud
# calls. `command = apply` against the mock gives computed attributes (email, member) fake but
# distinct values, so the assertions can tell one account from another.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "100000000001" # synthetic project number
    }
  }
}

variables {
  project_id = "example-project-01"
}

# The provider checks that a service_account_id looks like a real account name, so the mock's
# random names are replaced with synthetic, well-formed ones: for the push account, and for
# fhir-proxy so that a Token Creator grant misdirected to it fails the assertion below rather than
# the provider's format check. File level, because the accounts are created by the first run and
# later runs reuse that state.
override_resource {
  target = google_service_account.this["pubsub-push"]
  values = {
    name  = "projects/example-project-01/serviceAccounts/pb-pubsub-push@example-project-01.iam.gserviceaccount.com"
    email = "pb-pubsub-push@example-project-01.iam.gserviceaccount.com"
  }
}

override_resource {
  target = google_service_account.this["fhir-proxy"]
  values = {
    name  = "projects/example-project-01/serviceAccounts/pb-fhir-proxy@example-project-01.iam.gserviceaccount.com"
    email = "pb-fhir-proxy@example-project-01.iam.gserviceaccount.com"
  }
}

run "one_account_per_workload" {
  command = apply

  assert {
    condition = toset(keys(google_service_account.this)) == toset([
      "health-sync", "v2-to-fhir", "cuff-sim", "fhir-proxy", "edge-vm", "scheduler-invoker", "pubsub-push",
    ])
    error_message = "Expected exactly one service account per workload, and no seeder account by default."
  }

  assert {
    condition = toset([for sa in google_service_account.this : sa.account_id]) == toset([
      "pb-health-sync", "pb-v2-to-fhir", "pb-cuff-sim", "pb-fhir-proxy", "pb-edge-vm", "pb-scheduler-invoker", "pb-pubsub-push",
    ])
    error_message = "Account IDs must be <prefix>-<workload> and distinct."
  }

  assert {
    condition     = alltrue([for sa in google_service_account.this : sa.project == "example-project-01"])
    error_message = "Every account must be created in var.project_id."
  }

  assert {
    condition     = length(distinct(values(output.service_account_emails))) == 7
    error_message = "service_account_emails must hold seven distinct emails."
  }
}

# The healthcare module (T05) takes service_account_emails whole and picks the keys it uses, so
# the keys must be exactly these names and known at plan time (before any account exists).
run "email_keys_known_at_plan_time" {
  command = plan

  variables {
    create_seeder_account = true
  }

  assert {
    condition = toset(keys(output.service_account_emails)) == toset([
      "health-sync", "v2-to-fhir", "cuff-sim", "fhir-proxy", "edge-vm", "scheduler-invoker", "pubsub-push", "seeder",
    ])
    error_message = "service_account_emails must be keyed by the workload names, known at plan time."
  }
}

run "seeder_account_when_requested" {
  command = apply

  variables {
    create_seeder_account = true
  }

  assert {
    condition     = contains(keys(google_service_account.this), "seeder") && length(google_service_account.this) == 8
    error_message = "create_seeder_account = true must add exactly one account, seeder."
  }
}

run "only_project_role_is_edge_vm_log_writer" {
  command = apply

  variables {
    create_seeder_account = true
  }

  # The whole set of project-level grants, as role=member strings. Anything broader (owner,
  # editor, an admin role, a project-wide Healthcare or Secret Manager role) makes this fail.
  assert {
    condition = toset([for b in google_project_iam_member.this : "${b.role}=${b.member}"]) == toset([
      "roles/logging.logWriter=${"serviceAccount:${google_service_account.this["edge-vm"].email}"}",
    ])
    error_message = "The only project-level grant must be roles/logging.logWriter for the edge VM."
  }

  assert {
    condition = length([
      for b in google_project_iam_member.this : b.role
      if can(regex("(?i)(owner|editor|admin|iam\\.|serviceAccount|secretmanager|healthcare|run\\.)", b.role))
    ]) == 0
    error_message = "No broad, admin, IAM, Secret Manager, Healthcare or Cloud Run role may be granted at project level."
  }
}

run "pubsub_token_creator_can_be_turned_off" {
  command = apply

  variables {
    grant_pubsub_token_creator = false
  }

  assert {
    condition     = length(google_service_account_iam_member.pubsub_token_creator) == 0
    error_message = "grant_pubsub_token_creator = false must remove the Pub/Sub service agent grant."
  }
}

# On by default (this module is the single owner of the grant), and scoped to one account.
run "pubsub_token_creator_on_by_default_and_scoped_to_push_account" {
  command = apply

  assert {
    condition     = google_service_account_iam_member.pubsub_token_creator[0].service_account_id == google_service_account.this["pubsub-push"].name
    error_message = "Token Creator must be granted on the pubsub-push account only."
  }

  assert {
    condition     = google_service_account_iam_member.pubsub_token_creator[0].member == "serviceAccount:service-100000000001@gcp-sa-pubsub.iam.gserviceaccount.com"
    error_message = "Token Creator must go to this project's Pub/Sub service agent."
  }

  assert {
    condition     = google_service_account_iam_member.pubsub_token_creator[0].role == "roles/iam.serviceAccountTokenCreator"
    error_message = "The Pub/Sub service agent needs roles/iam.serviceAccountTokenCreator, nothing broader."
  }
}

run "prefix_is_applied" {
  command = apply

  variables {
    name_prefix = "demo"
  }

  assert {
    condition     = google_service_account.this["fhir-proxy"].account_id == "demo-fhir-proxy"
    error_message = "name_prefix must prefix the account IDs."
  }
}

run "prefix_too_long_is_rejected" {
  command = plan

  variables {
    name_prefix = "abcdefghijklm" # 13 characters: "abcdefghijklm-scheduler-invoker" would be 31
  }

  expect_failures = [var.name_prefix]
}

run "project_id_with_capitals_is_rejected" {
  command = plan

  variables {
    project_id = "Example-Project-01"
  }

  expect_failures = [var.project_id]
}

run "project_id_too_short_is_rejected" {
  command = plan

  variables {
    project_id = "ab-12" # 5 characters; project IDs have at least 6
  }

  expect_failures = [var.project_id]
}

run "project_id_ending_in_hyphen_is_rejected" {
  command = plan

  variables {
    project_id = "example-project-"
  }

  expect_failures = [var.project_id]
}
