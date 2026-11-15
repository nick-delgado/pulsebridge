# Bad inputs are refused at plan time. Mocked Google providers only (make tf-test).

mock_provider "google" {}
mock_provider "google-beta" {}

variables {
  project_id                  = "example-project"
  healthcare_location         = "us-central1"
  healthcare_dataset_id       = "pulsebridge"
  fhir_store_id               = "pulsebridge-fhir"
  audit_fhir_store_id         = "pulsebridge-audit"
  hl7v2_store_id              = "pulsebridge-hl7v2"
  patient_id                  = "PAT-0001"
  patient_assigning_authority = "EXAMPLE"
  patient_family_name         = "Example"
  patient_given_name          = "Pat"
  fhir_proxy_image            = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
  cuff_sim_image              = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"
  service_account_emails = {
    "fhir-proxy" = "pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    "cuff-sim"   = "pb-cuff-sim@example-project.iam.gserviceaccount.com"
  }
  secret_ids = {
    "reviewer-password" = "pulsebridge-reviewer-password"
  }
  network_name           = "pulsebridge-vpc"
  serverless_subnet_name = "pulsebridge-serverless"
  mllp_host              = "10.10.0.2"
}

run "valid_inputs_plan" {
  command = plan
}

# ---- images by digest only ------------------------------------------------------------------

run "tag_only_image_refused" {
  command = plan
  variables {
    fhir_proxy_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy:v1"
  }
  expect_failures = [var.fhir_proxy_image]
}

run "tag_and_digest_image_refused" {
  command = plan
  variables {
    cuff_sim_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim:v1@sha256:5555555555555555555555555555555555555555555555555555555555555555"
  }
  expect_failures = [var.cuff_sim_image]
}

run "short_digest_refused" {
  command = plan
  variables {
    cuff_sim_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555"
  }
  expect_failures = [var.cuff_sim_image]
}

# ---- URLs and origins -----------------------------------------------------------------------

run "http_health_sync_url_refused" {
  command = plan
  variables {
    health_sync_url = "http://pulsebridge-health-sync-abc123-uc.a.run.app"
  }
  expect_failures = [var.health_sync_url]
}

run "health_sync_url_with_path_refused" {
  command = plan
  variables {
    health_sync_url = "https://pulsebridge-health-sync-abc123-uc.a.run.app/status"
  }
  expect_failures = [var.health_sync_url]
}

run "health_sync_url_with_synthetic_data_refused" {
  command = plan
  variables {
    use_synthetic_data  = true
    patient_id          = "SYN-0001"
    patient_family_name = null
    patient_given_name  = null
    health_sync_url     = "https://pulsebridge-health-sync-abc123-uc.a.run.app"
  }
  expect_failures = [var.health_sync_url]
}

run "public_origin_with_path_refused" {
  command = plan
  variables {
    public_origin = "https://phr.example.org/api"
  }
  expect_failures = [var.public_origin]
}

run "public_origin_http_refused" {
  command = plan
  variables {
    public_origin = "http://phr.example.org"
  }
  expect_failures = [var.public_origin]
}

run "wildcard_origin_refused" {
  command = plan
  variables {
    extra_allowed_origins = ["https://*.example.org"]
  }
  expect_failures = [var.extra_allowed_origins]
}

run "origin_with_trailing_slash_refused" {
  command = plan
  variables {
    extra_allowed_origins = ["http://localhost:5173/"]
  }
  expect_failures = [var.extra_allowed_origins]
}

run "openemr_http_url_refused" {
  command = plan
  variables {
    openemr = {
      site_address     = "http://10.10.0.2"
      token_url        = "https://10.10.0.2/oauth2/default/token"
      fhir_base_url    = "https://10.10.0.2/apis/default/fhir"
      scopes           = "system/Patient.read"
      client_secret_id = "pb-openemr-client-private-key"
    }
  }
  expect_failures = [var.openemr]
}

# ---- hop count ------------------------------------------------------------------------------

run "negative_hops_refused" {
  command = plan
  variables {
    trusted_proxy_hops = -1
  }
  expect_failures = [var.trusted_proxy_hops]
}

run "too_many_hops_refused" {
  command = plan
  variables {
    trusted_proxy_hops = 6
  }
  expect_failures = [var.trusted_proxy_hops]
}

run "fractional_hops_refused" {
  command = plan
  variables {
    trusted_proxy_hops = 1.5
  }
  expect_failures = [var.trusted_proxy_hops]
}

run "five_hops_allowed" {
  command = plan
  variables {
    trusted_proxy_hops = 5
  }
}

# ---- the patient and the synthetic-name rule -------------------------------------------------

run "real_name_with_synthetic_data_refused" {
  command = plan
  variables {
    use_synthetic_data  = true
    patient_id          = "SYN-0001"
    patient_family_name = "Example"
    patient_given_name  = "Demo"
  }
  expect_failures = [var.patient_family_name]
}

run "real_given_name_with_synthetic_data_refused" {
  command = plan
  variables {
    use_synthetic_data  = true
    patient_id          = "SYN-0001"
    patient_family_name = null
    patient_given_name  = "Pat"
  }
  expect_failures = [var.patient_given_name]
}

run "missing_name_without_synthetic_data_refused" {
  command = plan
  variables {
    patient_family_name = null
  }
  expect_failures = [var.patient_family_name]
}

run "syn_patient_without_synthetic_data_refused" {
  command = plan
  variables {
    patient_id = "SYN-0001"
  }
  expect_failures = [var.patient_id]
}

run "real_patient_with_synthetic_data_refused" {
  command = plan
  variables {
    use_synthetic_data  = true
    patient_family_name = null
    patient_given_name  = null
  }
  expect_failures = [var.patient_id]
}

run "backslash_in_name_refused" {
  command = plan
  variables {
    patient_family_name = "Ex\\ample"
  }
  expect_failures = [var.patient_family_name]
}

run "long_assigning_authority_refused" {
  command = plan
  variables {
    patient_assigning_authority = "AUTHORITY-LONGER-THAN-20"
  }
  expect_failures = [var.patient_assigning_authority]
}

# ---- wiring mistakes ------------------------------------------------------------------------

run "audit_store_equal_to_fhir_store_refused" {
  command = plan
  variables {
    audit_fhir_store_id = "pulsebridge-fhir"
  }
  expect_failures = [var.audit_fhir_store_id]
}

run "missing_cuff_sim_account_refused" {
  command = plan
  variables {
    service_account_emails = { "fhir-proxy" = "pb-fhir-proxy@example-project.iam.gserviceaccount.com" }
  }
  expect_failures = [var.service_account_emails]
}

run "missing_reviewer_password_secret_refused" {
  command = plan
  variables {
    secret_ids = { "openemr-client-private-key" = "pb-openemr-client-private-key" }
  }
  expect_failures = [var.secret_ids]
}

run "mllp_host_name_refused" {
  command = plan
  variables {
    mllp_host = "edge-vm.internal"
  }
  expect_failures = [var.mllp_host]
}

run "region_hosting_cannot_rewrite_to_refused" {
  command = plan
  variables {
    region = "africa-south1"
  }
  expect_failures = [var.region]
}

run "subnet_path_refused" {
  command = plan
  variables {
    serverless_subnet_name = "projects/example-project/regions/us-central1/subnetworks/pulsebridge-serverless"
  }
  expect_failures = [var.serverless_subnet_name]
}

run "too_many_cuff_instances_refused" {
  command = plan
  variables {
    cuff_sim_max_instances = 4
  }
  expect_failures = [var.cuff_sim_max_instances]
}
