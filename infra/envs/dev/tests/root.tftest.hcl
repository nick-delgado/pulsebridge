# The dev ROOT: its defaults, the rules a stranger meets first, and the patient check. Mocked
# providers, `command = plan`, no backend (terraform test keeps state in memory). The wiring of
# the seven modules is tested in infra/envs/_stack/tests; that dev and prod differ only in env.tf
# and backend.tf is checked by infra/envs/_stack/tests/check_env_roots.py.

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "100000000001" }
  }
}

mock_provider "google-beta" {}

# Synthetic values only. Only what dev has no default for is set: the quick start.
variables {
  project_id                  = "example-project"
  patient_id                  = "SYN-0001"
  patient_assigning_authority = "EXAMPLE"
  time_zone                   = "America/New_York"
  alert_email                 = "alerts@example.com"

  v2_to_fhir_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir@sha256:1111111111111111111111111111111111111111111111111111111111111111"
  seed_image       = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/seed@sha256:3333333333333333333333333333333333333333333333333333333333333333"
  fhir_proxy_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
  cuff_sim_image   = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"
}

run "defaults_are_a_synthetic_dev_stack_with_ingest_off" {
  command = plan

  assert {
    condition     = output.environment == "dev" && output.region == "us-central1"
    error_message = "dev's defaults: environment dev, region us-central1."
  }
  assert {
    condition     = output.seed_job_name == "pulsebridge-seed" && output.health_sync_service_name == null
    error_message = "dev defaults to use_synthetic_data = true: the seeder job, no health-sync."
  }
  assert {
    condition     = output.ingest_enabled == false && output.push_enabled == false && output.scheduler_job_names == []
    error_message = "ingest_enabled defaults to false: no Scheduler job, no push, until the setup step has run."
  }
  assert {
    condition     = output.secret_name_prefix == "pb" && output.healthcare.fhir_store_id == "pulsebridge-fhir"
    error_message = "The default prefixes must give the names the connect script and the services expect."
  }
  assert {
    # Measured behind Firebase Hosting (infra/envs/README.md, P1). That the value reaches the
    # proxy's TRUSTED_PROXY_HOPS is checked on the planned service by
    # infra/envs/_stack/tests/check_env_roots.py (example plans).
    condition     = var.trusted_proxy_hops == 2
    error_message = "trusted_proxy_hops defaults to 2: the page reaches the proxy through Firebase Hosting."
  }
}

# ---- use_synthetic_data and the patient ----------------------------------------------------

run "synthetic_data_requires_a_syn_patient" {
  command = plan

  variables {
    patient_id = "PAT-0001"
  }

  expect_failures = [var.patient_id]
}

run "real_data_refuses_a_syn_patient" {
  command = plan

  variables {
    use_synthetic_data  = false
    health_sync_image   = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    patient_family_name = "Example"
    patient_given_name  = "Pat"
  }

  expect_failures = [var.patient_id]
}

run "synthetic_data_refuses_a_real_name" {
  command = plan

  variables {
    patient_family_name = "Example"
  }

  expect_failures = [var.patient_family_name]
}

run "real_data_requires_the_names" {
  command = plan

  variables {
    use_synthetic_data = false
    patient_id         = "PAT-0001"
    health_sync_image  = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync@sha256:2222222222222222222222222222222222222222222222222222222222222222"
  }

  expect_failures = [var.patient_family_name, var.patient_given_name]
}

run "real_data_plans_with_names_and_a_real_patient" {
  command = plan

  variables {
    use_synthetic_data  = false
    patient_id          = "PAT-0001"
    patient_family_name = "Example"
    patient_given_name  = "Pat"
    health_sync_image   = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync@sha256:2222222222222222222222222222222222222222222222222222222222222222"
  }

  assert {
    condition     = output.health_sync_service_name == "pulsebridge-health-sync" && output.seed_job_name == null
    error_message = "use_synthetic_data = false: health-sync and no seeder."
  }
}

# ---- other rules a stranger meets first ---------------------------------------------------

run "alerts_need_a_destination" {
  command = plan

  variables {
    alert_email = null
  }

  expect_failures = [var.alert_email]
}

run "zone_must_be_in_the_region" {
  command = plan

  variables {
    zone = "us-east1-b"
  }

  expect_failures = [var.zone]
}

run "images_must_be_pinned_by_digest" {
  command = plan

  variables {
    v2_to_fhir_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir:v1"
  }

  expect_failures = [var.v2_to_fhir_image]
}

# ---- the patient check (checks.tf) ------------------------------------------------------------
# override_module stands in for module.stack with a WRONG wiring, which the real inputs cannot
# produce (there is one variable each): the check must fire.

run "patient_check_fires_when_ingestion_and_phr_disagree" {
  command = plan

  override_module {
    target = module.stack
    outputs = {
      patient_wiring = {
        ingestion = { id = "SYN-0001", assigning_authority = "EXAMPLE" }
        phr       = { id = "SYN-0002", assigning_authority = "EXAMPLE" }
      }
    }
  }

  expect_failures = [check.one_patient_for_the_cuff_path]
}

run "patient_check_fires_without_an_assigning_authority" {
  command = plan

  override_module {
    target = module.stack
    outputs = {
      patient_wiring = {
        ingestion = { id = "SYN-0001", assigning_authority = null }
        phr       = { id = "SYN-0001", assigning_authority = null }
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

# ---- T41: CI's page deployer -----------------------------------------------------------------

# Optional, off by default. That a set value becomes exactly one roles/run.viewer grant on
# fhir-proxy is checked on the planned resources by infra/envs/_stack/tests/plan_scan.py.
run "page_deployer_is_off_by_default" {
  command = plan

  assert {
    condition     = var.page_deployer_service_account_email == null
    error_message = "page_deployer_service_account_email defaults to null: no grant until CI's page deployer exists."
  }
}

run "page_deployer_must_be_a_service_account" {
  command = plan

  variables {
    page_deployer_service_account_email = "someone@example.com"
  }

  expect_failures = [var.page_deployer_service_account_email]
}

run "page_deployer_service_account_plans" {
  command = plan

  variables {
    page_deployer_service_account_email = "page-deployer@example-project.iam.gserviceaccount.com"
  }
}

# T44: the demo settings pass through the root; unset keeps module phr's defaults.
run "demo_settings_pass_through_the_root" {
  command = plan

  variables {
    recaptcha_min_score           = 0.7
    bot_checks_per_day            = 120
    session_ttl_seconds           = 3600
    sessions_per_address_per_hour = 9
  }

  assert {
    condition     = output.demo_settings == { recaptcha_min_score = 0.7, session_ttl_seconds = 3600, sessions_per_address_per_hour = 9, bot_checks_per_day = 120 }
    error_message = "Each demo setting set in tfvars must reach module phr."
  }
}

run "demo_settings_default_at_the_root" {
  command = plan

  assert {
    condition     = output.demo_settings == { recaptcha_min_score = 0.5, session_ttl_seconds = 86400, sessions_per_address_per_hour = 5, bot_checks_per_day = 300 }
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
