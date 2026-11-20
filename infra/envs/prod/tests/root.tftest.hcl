# The prod ROOT: its defaults and the settings that make it prod. Mocked providers,
# `command = plan`, no backend. The rules shared with dev (variables.tf is identical in both
# roots, checked by infra/envs/_stack/tests/check_env_roots.py) are tested in dev's tests; the
# planned prod values (PREVENT, deletion protection) in infra/envs/_stack/tests/plan_scan.py.

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "100000000001" }
  }
}

mock_provider "google-beta" {}

# Synthetic values only (never the deployer's real patient or address).
variables {
  project_id                  = "example-project"
  patient_id                  = "PAT-0001"
  patient_assigning_authority = "EXAMPLE"
  patient_family_name         = "Example"
  patient_given_name          = "Pat"
  time_zone                   = "America/New_York"
  alert_email                 = "alerts@example.com"

  v2_to_fhir_image  = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir@sha256:1111111111111111111111111111111111111111111111111111111111111111"
  health_sync_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync@sha256:2222222222222222222222222222222222222222222222222222222222222222"
  fhir_proxy_image  = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
  cuff_sim_image    = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"
}

run "defaults_are_a_real_data_prod_stack_with_ingest_off" {
  command = plan

  assert {
    condition     = output.environment == "prod"
    error_message = "This root is prod."
  }
  assert {
    condition     = output.health_sync_service_name == "pulsebridge-health-sync" && output.seed_job_name == null
    error_message = "prod defaults to use_synthetic_data = false: health-sync and no seeder."
  }
  assert {
    condition     = output.ingest_enabled == false && output.scheduler_job_names == []
    error_message = "ingest_enabled defaults to false in prod too."
  }
  assert {
    # Measured behind Firebase Hosting (infra/envs/README.md, P1). That the value reaches the
    # proxy's TRUSTED_PROXY_HOPS is checked on the planned service by
    # infra/envs/_stack/tests/check_env_roots.py (example plans).
    condition     = var.trusted_proxy_hops == 2
    error_message = "trusted_proxy_hops defaults to 2: the page reaches the proxy through Firebase Hosting."
  }
}

run "prod_refuses_a_syn_patient_with_real_data" {
  command = plan

  variables {
    patient_id = "SYN-0001"
  }

  expect_failures = [var.patient_id]
}

run "prod_can_run_synthetic_data_if_asked" {
  command = plan

  variables {
    use_synthetic_data  = true
    patient_id          = "SYN-0001"
    patient_family_name = null
    patient_given_name  = null
    seed_image          = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/seed@sha256:3333333333333333333333333333333333333333333333333333333333333333"
  }

  assert {
    condition     = output.seed_job_name == "pulsebridge-seed"
    error_message = "use_synthetic_data = true must work in prod as well (it is a default, not a lock)."
  }
}

run "patient_check_fires_when_ingestion_and_phr_disagree" {
  command = plan

  override_module {
    target = module.stack
    outputs = {
      patient_wiring = {
        ingestion = { id = "PAT-0001", assigning_authority = "EXAMPLE" }
        phr       = { id = "PAT-0001", assigning_authority = "OTHER" }
      }
    }
  }

  expect_failures = [check.one_patient_for_the_cuff_path]
}

# T26: the build trigger runs as the build account, so it is refused without it.
run "build_trigger_needs_the_build_account" {
  command = plan

  variables {
    build_trigger_repository = "projects/example-project/locations/us-central1/connections/github/repositories/pulsebridge"
  }

  expect_failures = [var.build_trigger_repository]
}

# T26 review: triggered builds wait for an approval unless the root turns it off.
run "build_approval_is_on_by_default" {
  command = plan

  assert {
    condition     = var.build_trigger_approval_required == true
    error_message = "build_trigger_approval_required must default to true in the root."
  }
}

# T44: the demo settings pass through the root; unset keeps module phr's defaults.
run "demo_settings_pass_through_the_root" {
  command = plan

  variables {
    recaptcha_min_score                   = 0.7
    bot_checks_per_day                    = 120
    session_ttl_seconds                   = 3600
    sessions_per_address_per_hour         = 9
    requests_per_address_per_minute       = 450
    reads_per_session_per_minute          = 240
    cuff_sends_per_session_per_10_minutes = 4
    shares_per_session_per_10_minutes     = 1
    shares_per_instance_per_hour          = 15
    history_days                          = 14
  }

  assert {
    condition     = output.demo_settings == { recaptcha_min_score = 0.7, session_ttl_seconds = 3600, sessions_per_address_per_hour = 9, bot_checks_per_day = 120, requests_per_address_per_minute = 450, reads_per_session_per_minute = 240, cuff_sends_per_session_per_10_minutes = 4, shares_per_session_per_10_minutes = 1, shares_per_instance_per_hour = 15, history_days = 14 }
    error_message = "Each demo setting set in tfvars must reach module phr."
  }
}

run "demo_settings_default_at_the_root" {
  command = plan

  assert {
    condition     = output.demo_settings == { recaptcha_min_score = 0.5, session_ttl_seconds = 86400, sessions_per_address_per_hour = 5, bot_checks_per_day = 300, requests_per_address_per_minute = 300, reads_per_session_per_minute = 120, cuff_sends_per_session_per_10_minutes = 3, shares_per_session_per_10_minutes = 2, shares_per_instance_per_hour = 30, history_days = 7 }
    error_message = "Unset demo settings must give module phr's defaults."
  }
}

# T44: make smoke reads the proxy's issuer from this output (PULSEBRIDGE_TOKEN_ISSUER).
run "token_issuer_output_follows_name_prefix" {
  command = plan

  variables {
    name_prefix = "pbsmoke"
  }

  assert {
    condition     = endswith(output.token_issuer, ":pbsmoke") && startswith(output.token_issuer, "pulsebridge-fhir-proxy:")
    error_message = "token_issuer must end with name_prefix (not identity_name_prefix)."
  }
}
