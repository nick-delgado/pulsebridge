# The public demo's resources (D16, T44): the reCAPTCHA score key, the counter bucket and its one
# grant, and the settings fhir-proxy gets for them. Mocked Google providers only; run with Google
# credentials removed (make tf-test).

mock_provider "google" {
  override_resource {
    target = google_cloud_run_v2_service.cuff_sim
    values = { uri = "https://pulsebridge-cuff-sim-abc123-uc.a.run.app" }
  }
  override_resource {
    target = google_recaptcha_enterprise_key.demo
    values = { name = "synthetic-test-site-key-0000000000000000" }
  }
}

mock_provider "google-beta" {}

variables {
  project_id            = "example-project"
  region                = "us-central1"
  healthcare_location   = "us-central1"
  healthcare_dataset_id = "pulsebridge"
  fhir_store_id         = "pulsebridge-fhir"
  audit_fhir_store_id   = "pulsebridge-audit"
  hl7v2_store_id        = "pulsebridge-hl7v2"

  patient_id                  = "PAT-0001"
  patient_assigning_authority = "EXAMPLE"
  patient_family_name         = "Example"
  patient_given_name          = "Pat"

  fhir_proxy_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
  cuff_sim_image   = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"

  service_account_emails = {
    "fhir-proxy"  = "pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    "cuff-sim"    = "pb-cuff-sim@example-project.iam.gserviceaccount.com"
    "health-sync" = "pb-health-sync@example-project.iam.gserviceaccount.com"
  }
  secret_ids = {
    "session-signing-key" = "pulsebridge-session-signing-key"
  }

  network_name           = "pulsebridge-vpc"
  serverless_subnet_name = "pulsebridge-serverless"
  mllp_host              = "10.10.0.2"
  mllp_port              = 2575

  health_sync_url = "https://pulsebridge-health-sync-abc123-uc.a.run.app"
}

# The runs compare against literals, so a wrong expression in the module cannot also make the
# expectation wrong.

# ---- the reCAPTCHA key ------------------------------------------------------------------------

run "score_key_for_the_sites_hosts" {
  command = apply

  assert {
    condition = (
      length(google_recaptcha_enterprise_key.demo.web_settings) == 1 &&
      google_recaptcha_enterprise_key.demo.web_settings[0].integration_type == "SCORE" &&
      google_recaptcha_enterprise_key.demo.web_settings[0].allow_all_domains == false &&
      google_recaptcha_enterprise_key.demo.project == "example-project"
    )
    error_message = "The demo key must be a SCORE web key in the project, restricted to its domains."
  }
  assert {
    condition = toset(google_recaptcha_enterprise_key.demo.web_settings[0].allowed_domains) == toset([
      "example-project.web.app",
      "example-project.firebaseapp.com",
    ])
    error_message = "Without public_origin the key must allow exactly <site>.web.app and <site>.firebaseapp.com."
  }
  # The key is for a web page only: no app or WAF settings.
  assert {
    condition = (
      length(google_recaptcha_enterprise_key.demo.android_settings) == 0 &&
      length(google_recaptcha_enterprise_key.demo.ios_settings) == 0 &&
      length(google_recaptcha_enterprise_key.demo.waf_settings) == 0 &&
      length(google_recaptcha_enterprise_key.demo.testing_options) == 0
    )
    error_message = "The key must have no Android, iOS, WAF or testing settings (a testing score would pass every bot)."
  }
  assert {
    condition     = google_recaptcha_enterprise_key.demo.deletion_policy == "DELETE"
    error_message = "Without deletion_protection the key must go with a destroy (dev)."
  }
  assert {
    condition     = output.recaptcha_site_key == "synthetic-test-site-key-0000000000000000"
    error_message = "recaptcha_site_key must be the key's ID."
  }
}

run "custom_domain_and_site_id_reach_the_key" {
  command = apply
  variables {
    hosting_site_id = "pulsebridge-demo-site"
    public_origin   = "https://phr.example.org"
  }

  assert {
    condition = toset(google_recaptcha_enterprise_key.demo.web_settings[0].allowed_domains) == toset([
      "pulsebridge-demo-site.web.app",
      "pulsebridge-demo-site.firebaseapp.com",
      "phr.example.org",
    ])
    error_message = "The key must allow the site's two Hosting hosts and public_origin's host (no scheme)."
  }
}

run "public_origin_equal_to_the_web_app_host_is_listed_once" {
  command = apply
  variables {
    public_origin = "https://example-project.web.app"
  }

  assert {
    condition     = length(google_recaptcha_enterprise_key.demo.web_settings[0].allowed_domains) == 2
    error_message = "A public_origin that is the site's own web.app host must not be listed twice."
  }
}

# ---- the Origin check accepts every page origin the key accepts ----------------------------------

run "allowed_origins_cover_both_hosting_hosts" {
  command = apply

  assert {
    condition = (
      { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["ALLOWED_ORIGINS"] ==
      "https://example-project.web.app,https://example-project.firebaseapp.com"
    )
    error_message = "Without a custom domain ALLOWED_ORIGINS must be the site's web.app and firebaseapp.com origins (a visitor on either gets a token from the key)."
  }
}

run "allowed_origins_with_custom_domain_and_extras_deduplicated" {
  command = apply
  variables {
    public_origin         = "https://phr.example.org"
    extra_allowed_origins = ["http://localhost:5173", "https://example-project.web.app"]
  }

  assert {
    condition = (
      { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["ALLOWED_ORIGINS"] ==
      "https://phr.example.org,https://example-project.web.app,https://example-project.firebaseapp.com,http://localhost:5173"
    )
    error_message = "ALLOWED_ORIGINS must be public_origin, the two Hosting origins, then the extras, each once."
  }
  # Every domain the key accepts tokens from has its https origin in ALLOWED_ORIGINS.
  assert {
    condition = alltrue([
      for d in google_recaptcha_enterprise_key.demo.web_settings[0].allowed_domains :
      contains(output.allowed_origins, "https://${d}")
    ])
    error_message = "Every reCAPTCHA domain must be an allowed origin, or its visitors get 403 origin-refused after passing the bot check."
  }
}

# ---- the issuer make smoke signs with is the proxy's ------------------------------------------

run "token_issuer_output_is_the_proxys_token_issuer" {
  command = apply
  variables {
    name_prefix = "pbx"
  }

  assert {
    condition = output.token_issuer == {
      for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value
    }["TOKEN_ISSUER"]
    error_message = "token_issuer must be exactly fhir-proxy's TOKEN_ISSUER, or make smoke signs sessions the proxy refuses."
  }
  assert {
    condition     = output.token_issuer == "pulsebridge-fhir-proxy:example-project:pbx"
    error_message = "token_issuer must be pulsebridge-fhir-proxy:<project_id>:<name_prefix> (name_prefix, not the identity prefix)."
  }
}

# ---- the counter bucket -----------------------------------------------------------------------

run "counter_bucket_is_private_short_lived_and_destroyable_in_dev" {
  command = apply

  assert {
    condition = (
      google_storage_bucket.demo_counters.name == "example-project-pulsebridge-demo-counters" &&
      google_storage_bucket.demo_counters.project == "example-project" &&
      google_storage_bucket.demo_counters.location == "us-central1"
    )
    error_message = "The counter bucket must be <project_id>-<name_prefix>-demo-counters, in the services' region."
  }
  assert {
    condition = (
      google_storage_bucket.demo_counters.uniform_bucket_level_access == true &&
      google_storage_bucket.demo_counters.public_access_prevention == "enforced"
    )
    error_message = "The counter bucket must use uniform access and enforce public access prevention."
  }
  assert {
    condition = (
      length(google_storage_bucket.demo_counters.lifecycle_rule) == 1 &&
      [for a in google_storage_bucket.demo_counters.lifecycle_rule[0].action : a.type] == ["Delete"] &&
      [for c in google_storage_bucket.demo_counters.lifecycle_rule[0].condition : c.age] == [7]
    )
    error_message = "Counters must be deleted 7 days after they were created (one lifecycle rule)."
  }
  assert {
    condition = (
      google_storage_bucket.demo_counters.versioning[0].enabled == false &&
      google_storage_bucket.demo_counters.soft_delete_policy[0].retention_duration_seconds == 0
    )
    error_message = "Each increment replaces the object: no versioning and no soft delete, so old generations are not kept and billed."
  }
  assert {
    condition     = google_storage_bucket.demo_counters.force_destroy == true
    error_message = "Without deletion_protection (dev) a destroy must delete the counters with the bucket."
  }
  assert {
    condition     = output.demo_counter_bucket == google_storage_bucket.demo_counters.name
    error_message = "demo_counter_bucket must name the bucket."
  }
}

run "only_fhir_proxy_can_write_counters" {
  command = apply

  assert {
    condition = (
      google_storage_bucket_iam_member.demo_counter_writer.bucket == "example-project-pulsebridge-demo-counters" &&
      google_storage_bucket_iam_member.demo_counter_writer.role == "roles/storage.objectUser" &&
      google_storage_bucket_iam_member.demo_counter_writer.member == "serviceAccount:pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    )
    error_message = "fhir-proxy, and only it, gets roles/storage.objectUser on the counter bucket."
  }
}

# ---- prod: protect_from_destroy ---------------------------------------------------------------

run "protected_in_prod" {
  command = apply
  variables {
    deletion_protection = true
  }

  assert {
    condition     = google_storage_bucket.demo_counters.force_destroy == false
    error_message = "With deletion_protection the counters must not be deleted as a side effect of a destroy."
  }
  assert {
    condition     = google_recaptcha_enterprise_key.demo.deletion_policy == "PREVENT"
    error_message = "With deletion_protection a destroy of the key must fail."
  }
}

# ---- the proxy's settings -----------------------------------------------------------------------

run "demo_settings_follow_the_variables" {
  command = apply
  variables {
    recaptcha_min_score           = 0.7
    session_ttl_seconds           = 3600
    sessions_per_address_per_hour = 9
    bot_checks_per_day            = 120
    name_prefix                   = "pbx"
  }

  assert {
    condition = {
      for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value
      if contains(["RECAPTCHA_SITE_KEY", "RECAPTCHA_ACTION", "RECAPTCHA_MIN_SCORE", "SESSION_TTL_SECONDS", "SESSIONS_PER_ADDRESS_PER_HOUR", "BOT_CHECKS_PER_DAY", "DEMO_COUNTER_BUCKET", "SESSION_SIGNING_KEY_FILE"], e.name)
      } == {
      RECAPTCHA_SITE_KEY            = "synthetic-test-site-key-0000000000000000"
      RECAPTCHA_ACTION              = "view_demo"
      RECAPTCHA_MIN_SCORE           = "0.7"
      SESSION_TTL_SECONDS           = "3600"
      SESSIONS_PER_ADDRESS_PER_HOUR = "9"
      BOT_CHECKS_PER_DAY            = "120"
      DEMO_COUNTER_BUCKET           = "example-project-pbx-demo-counters"
      SESSION_SIGNING_KEY_FILE      = "/secrets/session-signing-key/value"
    }
    error_message = "Each demo setting must reach fhir-proxy under its contract name."
  }
  # The removed password settings must not come back.
  assert {
    condition = length([
      for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name
      if contains(["REVIEWER_PASSWORD_FILE", "PASSWORD_CACHE_SECONDS", "LOGIN_FAILURES_PER_ADDRESS", "LOGIN_FAILURES_TOTAL", "LOGIN_WINDOW_SECONDS"], e.name)
    ]) == 0
    error_message = "The reviewer-password settings were removed by D16 and must not be set."
  }
}

# ---- input rules ------------------------------------------------------------------------------

run "zero_min_score_refused" {
  command = plan
  variables {
    recaptcha_min_score = 0
  }
  expect_failures = [var.recaptcha_min_score]
}

run "min_score_above_one_refused" {
  command = plan
  variables {
    recaptcha_min_score = 1.1
  }
  expect_failures = [var.recaptcha_min_score]
}

run "ttl_above_a_day_refused" {
  command = plan
  variables {
    session_ttl_seconds = 86401
  }
  expect_failures = [var.session_ttl_seconds]
}

run "zero_sessions_per_address_refused" {
  command = plan
  variables {
    sessions_per_address_per_hour = 0
  }
  expect_failures = [var.sessions_per_address_per_hour]
}

run "bot_checks_above_3000_refused" {
  command = plan
  variables {
    bot_checks_per_day = 3001
  }
  expect_failures = [var.bot_checks_per_day]
}

run "fractional_bot_checks_refused" {
  command = plan
  variables {
    bot_checks_per_day = 2.5
  }
  expect_failures = [var.bot_checks_per_day]
}

# A split Healthcare project would make every assessment go to the wrong project (the contract's
# GCP_PROJECT_ID is the Healthcare project), so the key refuses it.
run "split_healthcare_project_refused" {
  command = plan
  variables {
    healthcare_project_id = "other-project-01"
  }
  expect_failures = [google_recaptcha_enterprise_key.demo]
}

# The bucket name must fit Cloud Storage's 63 characters: a 30-character project and a
# 20-character prefix make 65.
run "too_long_bucket_name_refused" {
  command = plan
  variables {
    project_id       = "a-thirty-character-project-id1"
    name_prefix      = "a-twenty-char-prefix"
    fhir_proxy_image = "us-central1-docker.pkg.dev/a-thirty-character-project-id1/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    cuff_sim_image   = "us-central1-docker.pkg.dev/a-thirty-character-project-id1/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"
  }
  expect_failures = [google_storage_bucket.demo_counters]
}
