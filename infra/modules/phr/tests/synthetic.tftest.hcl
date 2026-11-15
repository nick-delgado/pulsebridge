# use_synthetic_data = true: the seeder instead of health-sync. The cuff path's patient name is
# the seeder's synthetic name, and there is no health-sync URL.
# Mocked Google providers only; run with Google credentials removed (make tf-test).

mock_provider "google" {
  override_resource {
    target = google_cloud_run_v2_service.cuff_sim
    values = { uri = "https://pulsebridge-cuff-sim-abc123-ez.a.run.app" }
  }
}

mock_provider "google-beta" {}

variables {
  use_synthetic_data    = true
  project_id            = "example-project"
  region                = "europe-west4"
  healthcare_location   = "europe-west4"
  healthcare_dataset_id = "pulsebridge"
  fhir_store_id         = "pulsebridge-fhir"
  audit_fhir_store_id   = "pulsebridge-audit"
  hl7v2_store_id        = "pulsebridge-hl7v2"

  patient_id                  = "SYN-0001"
  patient_assigning_authority = "PULSEBRIDGE"

  fhir_proxy_image = "europe-west4-docker.pkg.dev/example-project/pulsebridge-services/fhir-proxy@sha256:4444444444444444444444444444444444444444444444444444444444444444"
  cuff_sim_image   = "europe-west4-docker.pkg.dev/example-project/pulsebridge-services/cuff-sim@sha256:5555555555555555555555555555555555555555555555555555555555555555"

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

run "synthetic_name_by_default" {
  command = apply

  assert {
    condition = (
      { for e in google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env : e.name => e.value }["PATIENT_FAMILY_NAME"] == "Synthetic" &&
      { for e in google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env : e.name => e.value }["PATIENT_GIVEN_NAME"] == "Demo" &&
      { for e in google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env : e.name => e.value }["PATIENT_ID"] == "SYN-0001"
    )
    error_message = "With synthetic data cuff-sim must send the seeder's synthetic name (Synthetic, Demo) for the SYN- patient."
  }
  assert {
    condition = (
      { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["PATIENT_FAMILY_NAME"] == "Synthetic" &&
      { for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name => e.value }["PATIENT_GIVEN_NAME"] == "Demo"
    )
    error_message = "fhir-proxy must get the same PID values as cuff-sim."
  }
}

run "synthetic_name_given_explicitly" {
  command = apply
  variables {
    patient_family_name = "Synthetic"
    patient_given_name  = "Demo"
  }

  assert {
    condition     = { for e in google_cloud_run_v2_service.cuff_sim.template[0].containers[0].env : e.name => e.value }["PATIENT_FAMILY_NAME"] == "Synthetic"
    error_message = "The synthetic name may also be given explicitly."
  }
}

run "no_health_sync_url" {
  command = apply

  assert {
    condition     = !contains([for e in google_cloud_run_v2_service.fhir_proxy.template[0].containers[0].env : e.name], "HEALTH_SYNC_URL")
    error_message = "With synthetic data HEALTH_SYNC_URL is not set (the status strip says health-sync-not-deployed)."
  }
  assert {
    condition     = jsondecode(output.firebase_json).hosting.rewrites[0].run.region == "europe-west4"
    error_message = "The rewrite must name the services' region."
  }
}
