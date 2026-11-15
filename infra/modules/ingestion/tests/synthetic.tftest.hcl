# use_synthetic_data = true: v2-to-fhir and the seeder job; nothing of health-sync.
# Mocked Google provider only; run with Google credentials removed (make tf-test).

mock_provider "google" {
  override_resource {
    target = google_cloud_run_v2_service.v2_to_fhir
    values = { uri = "https://pulsebridge-v2-to-fhir-abc123-uc.a.run.app" }
  }
}

variables {
  use_synthetic_data    = true
  project_id            = "example-project"
  region                = "europe-west4"
  healthcare_location   = "europe-west4"
  healthcare_dataset_id = "pulsebridge"
  fhir_store_id         = "pulsebridge-fhir"
  hl7v2_store_id        = "pulsebridge-hl7v2"
  patient_id            = "SYN-0001"
  time_zone             = "Europe/Amsterdam"
  v2_to_fhir_image      = "europe-west4-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir@sha256:1111111111111111111111111111111111111111111111111111111111111111"
  seed_image            = "europe-west4-docker.pkg.dev/example-project/pulsebridge-services/seed@sha256:3333333333333333333333333333333333333333333333333333333333333333"

  # No health-sync, no fhir-proxy key needed; identity's map with create_seeder_account = true.
  service_account_emails = {
    "v2-to-fhir"        = "pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
    "pubsub-push"       = "pb-pubsub-push@example-project.iam.gserviceaccount.com"
    "scheduler-invoker" = "pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
    "seeder"            = "pb-seeder@example-project.iam.gserviceaccount.com"
  }
}

run "seeder_instead_of_health_sync" {
  command = apply

  assert {
    condition     = length(google_cloud_run_v2_job.seed) == 1
    error_message = "use_synthetic_data = true must create the seeder job."
  }
  assert {
    condition = (
      length(google_cloud_run_v2_service.health_sync) == 0 &&
      length(google_cloud_scheduler_job.health_sync) == 0 &&
      length(google_logging_metric.health_sync) == 0 &&
      length(google_monitoring_alert_policy.health_sync) == 0
    )
    error_message = "use_synthetic_data = true must create nothing of health-sync (service, trigger, metrics, alerts)."
  }
  assert {
    condition = toset([
      for b in google_cloud_run_v2_service_iam_member.invoker : "${b.name} ${b.member}"
    ]) == toset(["pulsebridge-v2-to-fhir serviceAccount:pb-pubsub-push@example-project.iam.gserviceaccount.com"])
    error_message = "With synthetic data the only service invoker is pubsub-push on v2-to-fhir."
  }
  assert {
    condition     = output.health_sync_url == null && output.seed_job_name == "pulsebridge-seed"
    error_message = "With synthetic data, health_sync_url is null and seed_job_name is set."
  }
}

run "seeder_job_settings" {
  command = apply

  assert {
    condition     = google_cloud_run_v2_job.seed[0].template[0].template[0].service_account == "pb-seeder@example-project.iam.gserviceaccount.com"
    error_message = "The job must run as the seeder account."
  }
  assert {
    condition     = google_cloud_run_v2_job.seed[0].template[0].template[0].max_retries == 0
    error_message = "The job must not retry (max_retries = 0)."
  }
  assert {
    condition     = google_cloud_run_v2_job.seed[0].template[0].template[0].containers[0].image == var.seed_image
    error_message = "The job must run seed_image by digest."
  }
  assert {
    condition     = google_cloud_run_v2_job.seed[0].template[0].template[0].containers[0].args == tolist(["--hours", "4"]) && google_cloud_run_v2_job.seed[0].template[0].template[0].containers[0].command == tolist(["pulsebridge-seed"])
    error_message = "The scheduled execution must run pulsebridge-seed --hours 4."
  }
  assert {
    condition     = google_cloud_run_v2_job.seed[0].template[0].template[0].timeout == "1800s"
    error_message = "The task timeout must be seed_task_timeout (default 1800s)."
  }
  assert {
    condition = { for e in google_cloud_run_v2_job.seed[0].template[0].template[0].containers[0].env : e.name => e.value } == {
      GCP_PROJECT_ID        = "example-project"
      HEALTHCARE_LOCATION   = "europe-west4"
      HEALTHCARE_DATASET_ID = "pulsebridge"
      FHIR_STORE_ID         = "pulsebridge-fhir"
      PATIENT_ID            = "SYN-0001"
      TIME_ZONE             = "Europe/Amsterdam"
    }
    error_message = "The seeder must get exactly the environment its README lists for writes."
  }
}

run "job_runner_grant_is_narrow" {
  command = apply

  assert {
    condition = (
      length(google_cloud_run_v2_job_iam_member.seed_runner) == 1 &&
      google_cloud_run_v2_job_iam_member.seed_runner[0].name == "pulsebridge-seed" &&
      google_cloud_run_v2_job_iam_member.seed_runner[0].role == "roles/run.invoker" &&
      google_cloud_run_v2_job_iam_member.seed_runner[0].member == "serviceAccount:pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
    )
    error_message = "scheduler-invoker must get roles/run.invoker on the seeder job only."
  }
}

run "no_trigger_until_ingest_enabled" {
  command = apply

  assert {
    condition     = length(google_cloud_scheduler_job.seed) == 0 && length(google_cloud_scheduler_job.health_sync) == 0
    error_message = "With ingest_enabled = false no Scheduler job may exist, so the first --days 30 seeding runs first."
  }
}

run "hourly_trigger_when_ingest_enabled" {
  command = apply
  variables {
    ingest_enabled = true
  }

  assert {
    condition     = length(google_cloud_scheduler_job.seed) == 1 && length(google_cloud_scheduler_job.health_sync) == 0
    error_message = "ingest_enabled with synthetic data must create the seeder trigger only."
  }
  assert {
    condition     = google_cloud_scheduler_job.seed[0].schedule == "7 * * * *"
    error_message = "The seeder must be triggered hourly."
  }
  assert {
    condition     = google_cloud_scheduler_job.seed[0].retry_config[0].retry_count == 0
    error_message = "The seeder trigger must not retry."
  }
  assert {
    condition = (
      google_cloud_scheduler_job.seed[0].http_target[0].uri == "https://run.googleapis.com/v2/projects/example-project/locations/europe-west4/jobs/pulsebridge-seed:run" &&
      google_cloud_scheduler_job.seed[0].http_target[0].http_method == "POST"
    )
    error_message = "The trigger must POST to the Cloud Run Admin API's jobs.run for the seeder job."
  }
  assert {
    condition = (
      google_cloud_scheduler_job.seed[0].http_target[0].oauth_token[0].service_account_email == "pb-scheduler-invoker@example-project.iam.gserviceaccount.com" &&
      length(google_cloud_scheduler_job.seed[0].http_target[0].oidc_token) == 0
    )
    error_message = "A Google API call takes an OAuth token (not OIDC) for scheduler-invoker."
  }
}
