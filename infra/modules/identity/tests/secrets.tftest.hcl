# Secret Manager secrets and their accessor bindings. Mock provider: no credentials, no cloud calls.

mock_provider "google" {}

variables {
  project_id            = "example-project-01"
  create_seeder_account = true # the seeder must not get any secret either
}

run "eight_secrets_without_versions" {
  command = apply

  assert {
    condition = toset([for s in google_secret_manager_secret.this : s.secret_id]) == toset([
      "pb-google-health-client-id",
      "pb-google-health-client-secret",
      "pb-google-health-refresh-token",
      "pb-openemr-client-private-key",
      "pb-reviewer-password",
      "pb-db-root-password",
      "pb-openemr-db-password",
      "pb-openemr-admin-password",
    ])
    error_message = "Expected exactly the eight secrets from the brief (three Google Health, OpenEMR key, reviewer password, three database/OpenEMR passwords)."
  }

  assert {
    condition     = length(google_secret_manager_secret_version.reviewer_password) == 0 && output.reviewer_password_version_created == false
    error_message = "By default no secret has a version; values are added by scripts after apply."
  }

  assert {
    condition     = alltrue([for s in google_secret_manager_secret.this : s.deletion_protection == false])
    error_message = "Deletion protection is off by default so dev can be destroyed nightly."
  }
}

# The accessor bindings, compared as a whole: for every secret, the exact set of members that may
# read it. A missing, extra or misdirected binding fails here.
run "accessor_bindings_match_the_table" {
  command = apply

  assert {
    condition = {
      for b in google_secret_manager_secret_iam_member.accessor : b.secret_id => b.member...
      } == {
      (google_secret_manager_secret.this["google-health-client-id"].id)     = ["serviceAccount:${google_service_account.this["health-sync"].email}"]
      (google_secret_manager_secret.this["google-health-client-secret"].id) = ["serviceAccount:${google_service_account.this["health-sync"].email}"]
      (google_secret_manager_secret.this["google-health-refresh-token"].id) = ["serviceAccount:${google_service_account.this["health-sync"].email}"]
      (google_secret_manager_secret.this["openemr-client-private-key"].id)  = ["serviceAccount:${google_service_account.this["fhir-proxy"].email}"]
      (google_secret_manager_secret.this["reviewer-password"].id)           = ["serviceAccount:${google_service_account.this["fhir-proxy"].email}"]
      (google_secret_manager_secret.this["db-root-password"].id)            = ["serviceAccount:${google_service_account.this["edge-vm"].email}"]
      (google_secret_manager_secret.this["openemr-db-password"].id)         = ["serviceAccount:${google_service_account.this["edge-vm"].email}"]
      (google_secret_manager_secret.this["openemr-admin-password"].id)      = ["serviceAccount:${google_service_account.this["edge-vm"].email}"]
    }
    error_message = "Secret accessor bindings do not match the README table."
  }

  assert {
    condition     = toset([for b in google_secret_manager_secret_iam_member.accessor : b.role]) == toset(["roles/secretmanager.secretAccessor"])
    error_message = "Secret bindings may only grant read access (secretAccessor); nothing may add versions or administer secrets."
  }
}

# The same rules stated from the workloads' side, so a failure names the account at fault.
run "workloads_without_secrets_read_none" {
  command = apply

  assert {
    condition = length([
      for b in google_secret_manager_secret_iam_member.accessor : b
      if contains([
        "serviceAccount:${google_service_account.this["cuff-sim"].email}",
        "serviceAccount:${google_service_account.this["v2-to-fhir"].email}",
        "serviceAccount:${google_service_account.this["scheduler-invoker"].email}",
        "serviceAccount:${google_service_account.this["pubsub-push"].email}",
        "serviceAccount:${google_service_account.this["seeder"].email}",
      ], b.member)
    ]) == 0
    error_message = "cuff-sim, v2-to-fhir, scheduler-invoker, pubsub-push and seeder must not read any secret."
  }

  assert {
    condition = toset([
      for b in google_secret_manager_secret_iam_member.accessor : b.member
      if b.secret_id == google_secret_manager_secret.this["reviewer-password"].id || b.secret_id == google_secret_manager_secret.this["openemr-client-private-key"].id
    ]) == toset(["serviceAccount:${google_service_account.this["fhir-proxy"].email}"])
    error_message = "Only fhir-proxy may read the reviewer password and the OpenEMR private key."
  }

  assert {
    condition = toset([
      for b in google_secret_manager_secret_iam_member.accessor : b.secret_id
      if b.member == "serviceAccount:${google_service_account.this["health-sync"].email}"
      ]) == toset([
      google_secret_manager_secret.this["google-health-client-id"].id,
      google_secret_manager_secret.this["google-health-client-secret"].id,
      google_secret_manager_secret.this["google-health-refresh-token"].id,
    ])
    error_message = "health-sync must read exactly the three Google Health secrets (D2) and nothing else."
  }
}

run "labels_merge_and_deletion_protection_option" {
  command = apply

  variables {
    secret_labels              = { env = "dev" }
    secret_deletion_protection = true
  }

  assert {
    condition     = google_secret_manager_secret.this["reviewer-password"].labels == tomap({ component = "pulsebridge", managed-by = "terraform", env = "dev" })
    error_message = "secret_labels must be merged over the module's own labels."
  }

  assert {
    condition     = alltrue([for s in google_secret_manager_secret.this : s.deletion_protection])
    error_message = "secret_deletion_protection = true must protect every secret."
  }
}
