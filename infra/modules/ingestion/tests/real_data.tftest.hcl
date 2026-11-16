# use_synthetic_data = false: v2-to-fhir and health-sync, health-sync's alerts, no seeder.
# Plans and applies against a mocked Google provider only: nothing is created anywhere. Run with
# Google credentials removed from the environment (make tf-test does that).

mock_provider "google" {
  override_resource {
    target = google_cloud_run_v2_service.v2_to_fhir
    values = { uri = "https://pulsebridge-v2-to-fhir-abc123-uc.a.run.app" }
  }
  override_resource {
    target = google_cloud_run_v2_service.health_sync
    values = { uri = "https://pulsebridge-health-sync-abc123-uc.a.run.app" }
  }
  override_resource {
    target = google_monitoring_notification_channel.email
    values = { name = "projects/example-project/notificationChannels/999" }
  }
}

variables {
  project_id            = "example-project"
  healthcare_location   = "us-central1"
  healthcare_dataset_id = "pulsebridge"
  fhir_store_id         = "pulsebridge-fhir"
  hl7v2_store_id        = "pulsebridge-hl7v2"
  patient_id            = "PB-EXAMPLE-0001"
  time_zone             = "America/New_York"
  v2_to_fhir_image      = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir@sha256:1111111111111111111111111111111111111111111111111111111111111111"
  health_sync_image     = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync@sha256:2222222222222222222222222222222222222222222222222222222222222222"

  service_account_emails = {
    "v2-to-fhir"        = "pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
    "pubsub-push"       = "pb-pubsub-push@example-project.iam.gserviceaccount.com"
    "health-sync"       = "pb-health-sync@example-project.iam.gserviceaccount.com"
    "scheduler-invoker" = "pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
    "fhir-proxy"        = "pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    "cuff-sim"          = "pb-cuff-sim@example-project.iam.gserviceaccount.com"
    "edge-vm"           = "pb-edge-vm@example-project.iam.gserviceaccount.com"
  }
  secret_ids = {
    "google-health-client-id"     = "pb-google-health-client-id"
    "google-health-client-secret" = "pb-google-health-client-secret"
    "google-health-refresh-token" = "pb-google-health-refresh-token"
    "reviewer-password"           = "pb-reviewer-password"
  }
}

run "default_is_ingest_disabled" {
  command = apply

  assert {
    condition     = length(google_cloud_scheduler_job.health_sync) == 0 && length(google_cloud_scheduler_job.seed) == 0
    error_message = "With ingest_enabled = false (the default) no Scheduler job may exist: nothing may start ingestion before the setup step."
  }
  assert {
    condition     = output.ingest_enabled == false
    error_message = "The ingest_enabled output must echo false, so module healthcare's push stays off."
  }
  assert {
    condition     = length(google_cloud_run_v2_service.health_sync) == 1 && length(google_cloud_run_v2_job.seed) == 0 && length(google_cloud_run_v2_job_iam_member.seed_runner) == 0
    error_message = "use_synthetic_data = false must create health-sync and no seeder job or its grant."
  }
}

run "exact_invokers_and_nothing_public" {
  command = apply

  assert {
    condition = toset([
      for b in google_cloud_run_v2_service_iam_member.invoker : "${b.name} ${b.role} ${b.member}"
      ]) == toset([
      "pulsebridge-v2-to-fhir roles/run.invoker serviceAccount:pb-pubsub-push@example-project.iam.gserviceaccount.com",
      "pulsebridge-health-sync roles/run.invoker serviceAccount:pb-scheduler-invoker@example-project.iam.gserviceaccount.com",
      "pulsebridge-health-sync roles/run.invoker serviceAccount:pb-fhir-proxy@example-project.iam.gserviceaccount.com",
    ])
    error_message = "Invokers must be exactly: pubsub-push on v2-to-fhir; scheduler-invoker and fhir-proxy on health-sync."
  }
  assert {
    condition = alltrue([
      for b in google_cloud_run_v2_service_iam_member.invoker : !contains(["allUsers", "allAuthenticatedUsers"], b.member)
    ])
    error_message = "No service may be invocable by allUsers or allAuthenticatedUsers."
  }
  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.invoker_iam_disabled == false && google_cloud_run_v2_service.health_sync[0].invoker_iam_disabled == false
    error_message = "Cloud Run's invoker IAM check must stay on for both services."
  }
  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY" && google_cloud_run_v2_service.health_sync[0].ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    error_message = "Both services must default to internal-only ingress."
  }
}

run "scale_to_zero_and_one_health_sync_instance" {
  command = apply

  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.template[0].scaling[0].min_instance_count == 0 && google_cloud_run_v2_service.health_sync[0].template[0].scaling[0].min_instance_count == 0
    error_message = "Both services must scale to zero (min_instance_count = 0)."
  }
  assert {
    condition     = google_cloud_run_v2_service.health_sync[0].template[0].scaling[0].max_instance_count == 1
    error_message = "health-sync must have max_instance_count = 1, so its run lock covers the whole service."
  }
  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.template[0].scaling[0].max_instance_count == 2
    error_message = "v2-to-fhir's instance cap must be v2_to_fhir_max_instances (default 2)."
  }
  assert {
    condition     = tonumber(trimsuffix(google_cloud_run_v2_service.health_sync[0].template[0].timeout, "s")) >= 180
    error_message = "health-sync's request timeout must be at least 180 s (the Scheduler attempt deadline)."
  }
  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.template[0].containers[0].resources[0].cpu_idle && google_cloud_run_v2_service.health_sync[0].template[0].containers[0].resources[0].cpu_idle
    error_message = "CPU must be billed only during requests (cpu_idle = true)."
  }
}

run "images_service_accounts_and_v2_environment" {
  command = apply

  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.template[0].containers[0].image == var.v2_to_fhir_image && google_cloud_run_v2_service.health_sync[0].template[0].containers[0].image == var.health_sync_image
    error_message = "Each service must run the image given by digest."
  }
  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.template[0].service_account == "pb-v2-to-fhir@example-project.iam.gserviceaccount.com" && google_cloud_run_v2_service.health_sync[0].template[0].service_account == "pb-health-sync@example-project.iam.gserviceaccount.com"
    error_message = "Each service must run as its own service account."
  }
  assert {
    condition = { for e in google_cloud_run_v2_service.v2_to_fhir.template[0].containers[0].env : e.name => e.value } == {
      GCP_PROJECT_ID        = "example-project"
      HEALTHCARE_LOCATION   = "us-central1"
      HEALTHCARE_DATASET_ID = "pulsebridge"
      FHIR_STORE_ID         = "pulsebridge-fhir"
      HL7V2_STORE_ID        = "pulsebridge-hl7v2"
      PATIENT_ID            = "PB-EXAMPLE-0001"
    }
    error_message = "v2-to-fhir must get exactly the environment its README lists (no assigning authority unless set)."
  }
}

run "v2_assigning_authority_is_passed_when_set" {
  command = apply
  variables {
    patient_assigning_authority = "PULSEBRIDGE"
  }
  assert {
    condition     = contains([for e in google_cloud_run_v2_service.v2_to_fhir.template[0].containers[0].env : "${e.name}=${e.value}"], "PATIENT_ASSIGNING_AUTHORITY=PULSEBRIDGE")
    error_message = "patient_assigning_authority must become PATIENT_ASSIGNING_AUTHORITY."
  }
}

run "health_sync_secrets_are_files_at_latest" {
  command = apply

  assert {
    condition = toset([
      for v in google_cloud_run_v2_service.health_sync[0].template[0].volumes :
      "${v.name} ${v.secret[0].secret} ${v.secret[0].items[0].version} ${v.secret[0].items[0].path}"
      ]) == toset([
      "google-health-client-id pb-google-health-client-id latest value",
      "google-health-client-secret pb-google-health-client-secret latest value",
      "google-health-refresh-token pb-google-health-refresh-token latest value",
    ])
    error_message = "health-sync must mount exactly the three Google Health secrets as volumes at version latest, file \"value\"."
  }
  assert {
    condition = toset([
      for m in google_cloud_run_v2_service.health_sync[0].template[0].containers[0].volume_mounts : "${m.name} ${m.mount_path}"
      ]) == toset([
      "google-health-client-id /secrets/google-health-client-id",
      "google-health-client-secret /secrets/google-health-client-secret",
      "google-health-refresh-token /secrets/google-health-refresh-token",
    ])
    error_message = "Each secret must be mounted in its own directory."
  }
  assert {
    condition = alltrue([
      for e in google_cloud_run_v2_service.health_sync[0].template[0].containers[0].env : length(e.value_source) == 0
    ])
    error_message = "No environment variable may take its value from a secret."
  }
  assert {
    condition = { for e in google_cloud_run_v2_service.health_sync[0].template[0].containers[0].env : e.name => e.value } == {
      GCP_PROJECT_ID                   = "example-project"
      HEALTHCARE_LOCATION              = "us-central1"
      HEALTHCARE_DATASET_ID            = "pulsebridge"
      FHIR_STORE_ID                    = "pulsebridge-fhir"
      PATIENT_ID                       = "PB-EXAMPLE-0001"
      TIME_ZONE                        = "America/New_York"
      SYNC_INTERVAL_MINUTES            = "15"
      RUN_DEADLINE_SECONDS             = "150"
      GOOGLE_HEALTH_CLIENT_ID_FILE     = "/secrets/google-health-client-id/value"
      GOOGLE_HEALTH_CLIENT_SECRET_FILE = "/secrets/google-health-client-secret/value"
      GOOGLE_HEALTH_REFRESH_TOKEN_FILE = "/secrets/google-health-refresh-token/value"
    }
    error_message = "health-sync's environment must be exactly its README's required variables, the file paths matching the mounts."
  }
}

run "alert_metrics_without_channel" {
  command = apply

  assert {
    condition     = toset(keys(google_logging_metric.this)) == toset(["health-sync-failed-run", "health-sync-not-connected", "scheduler-attempt-failed"])
    error_message = "With real data there must be the failed-run, not-connected and Scheduler-attempt metrics."
  }
  assert {
    condition     = length(google_monitoring_alert_policy.this) == 0 && length(google_monitoring_notification_channel.email) == 0 && output.alert_email_channel == null
    error_message = "With neither alert_email nor notification_channels there must be no alert policy and no channel (metrics only)."
  }
  assert {
    condition = (
      strcontains(google_logging_metric.this["health-sync-failed-run"].filter, "resource.labels.service_name=\"pulsebridge-health-sync\"") &&
      strcontains(google_logging_metric.this["health-sync-failed-run"].filter, "(jsonPayload.event=\"sync-run\" AND jsonPayload.http_status>=500)")
    )
    error_message = "The failed-run metric must use the README's filter, scoped to health-sync."
  }
  assert {
    condition     = strcontains(google_logging_metric.this["health-sync-failed-run"].filter, "OR (jsonPayload.event=\"bug\" AND jsonPayload.message=~\"^sync failed\")")
    error_message = "The failed-run metric must also match the sync handler's own crash (event bug, message \"sync failed: ...\") and only that one."
  }
  assert {
    condition = (
      strcontains(google_logging_metric.this["health-sync-not-connected"].filter, "resource.labels.service_name=\"pulsebridge-health-sync\"") &&
      strcontains(google_logging_metric.this["health-sync-not-connected"].filter, "jsonPayload.event=\"sync-run\" AND (jsonPayload.outcome=\"reconnect-needed\" OR jsonPayload.outcome=\"not-configured\")")
    )
    error_message = "The not-connected metric must count reconnect-needed and not-configured sync runs of health-sync."
  }
  assert {
    condition = google_logging_metric.this["scheduler-attempt-failed"].filter == join(" AND ", [
      "resource.type=\"cloud_scheduler_job\"",
      "resource.labels.job_id=\"pulsebridge-health-sync-every-15-min\"",
      "jsonPayload.@type=\"type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished\"",
      "severity>=ERROR",
    ])
    error_message = "The Scheduler metric must count failed AttemptFinished entries of the sync trigger."
  }
}

run "alert_policies_with_channel" {
  command = apply
  variables {
    notification_channels = ["projects/example-project/notificationChannels/123"]
    labels                = { env = "test" }
  }
  assert {
    condition     = toset(keys(google_monitoring_alert_policy.this)) == toset(keys(google_logging_metric.this)) && length(google_monitoring_alert_policy.this) == 3
    error_message = "A notification channel must add one alert policy per metric."
  }
  assert {
    condition = alltrue([
      for k, p in google_monitoring_alert_policy.this :
      p.notification_channels == tolist(["projects/example-project/notificationChannels/123"]) &&
      p.conditions[0].condition_threshold[0].filter == "metric.type=\"logging.googleapis.com/user/${google_logging_metric.this[k].name}\""
    ])
    error_message = "Each policy must notify the channel and watch its own log metric."
  }
  assert {
    condition = alltrue([
      for k, p in google_monitoring_alert_policy.this :
      p.conditions[0].condition_threshold[0].comparison == "COMPARISON_GT" &&
      p.conditions[0].condition_threshold[0].threshold_value == 0 &&
      p.conditions[0].condition_threshold[0].duration == "0s" &&
      p.conditions[0].condition_threshold[0].aggregations[0].alignment_period == "900s" &&
      p.conditions[0].condition_threshold[0].aggregations[0].per_series_aligner == "ALIGN_SUM" &&
      p.conditions[0].condition_threshold[0].aggregations[0].cross_series_reducer == "REDUCE_SUM"
    ])
    error_message = "Each policy must fire when its counter summed over 15 minutes is above 0."
  }
  assert {
    condition     = alltrue([for p in google_monitoring_alert_policy.this : p.user_labels == tomap({ env = "test" })])
    error_message = "Alert policies must carry the module's labels."
  }
}

run "alert_email_creates_a_channel" {
  command = apply
  variables {
    alert_email                = "alerts@example.com"
    create_alert_email_channel = true
    notification_channels      = ["projects/example-project/notificationChannels/123"]
  }
  assert {
    condition = (
      length(google_monitoring_notification_channel.email) == 1 &&
      google_monitoring_notification_channel.email[0].type == "email" &&
      google_monitoring_notification_channel.email[0].labels["email_address"] == "alerts@example.com"
    )
    error_message = "alert_email must create one email notification channel for that address."
  }
  assert {
    # T26: the address must be redacted in plans (the one CI posts on a pull request).
    condition     = issensitive(google_monitoring_notification_channel.email[0].labels["email_address"]) && !strcontains(google_monitoring_notification_channel.email[0].display_name, "alerts@example.com")
    error_message = "The email channel's labels must be sensitive (alert_email is), and its display name must not carry the address."
  }
  assert {
    condition = alltrue([
      for p in google_monitoring_alert_policy.this :
      p.notification_channels == tolist(["projects/example-project/notificationChannels/123", "projects/example-project/notificationChannels/999"])
    ]) && length(google_monitoring_alert_policy.this) == 3
    error_message = "Every policy must notify both the passed-in channels and the email channel."
  }
}

run "alert_email_alone_creates_policies" {
  command = apply
  variables {
    alert_email                = "alerts@example.com"
    create_alert_email_channel = true
  }
  assert {
    condition = length(google_monitoring_alert_policy.this) == 3 && alltrue([
      for p in google_monitoring_alert_policy.this : p.notification_channels == tolist(["projects/example-project/notificationChannels/999"])
    ])
    error_message = "alert_email alone must be enough for the alert policies."
  }
}

run "health_sync_startup_probe_proves_the_app_started" {
  command = apply
  assert {
    condition = (
      google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].http_get[0].path == "/status" &&
      google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].http_get[0].port == 8080 &&
      length(google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].tcp_socket) == 0
    )
    error_message = "health-sync must have an HTTP startup probe on GET /status, port 8080."
  }
  assert {
    condition = (
      google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].timeout_seconds <= google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].period_seconds &&
      google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].timeout_seconds >= 20 &&
      google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].failure_threshold * google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].period_seconds <= 600 &&
      google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].failure_threshold * google_cloud_run_v2_service.health_sync[0].template[0].containers[0].startup_probe[0].period_seconds >= 60
    )
    error_message = "The probe timeout must cover /status's 20 s budget and not exceed the period; the window must be 60-600 s."
  }
}

run "v2_to_fhir_timeout_concurrency_and_destroyable" {
  command = apply
  assert {
    # Module healthcare's ack_deadline_seconds defaults to 60: Pub/Sub retries a push that is
    # unanswered by then, so a longer request timeout only lets two copies run at once.
    condition     = tonumber(trimsuffix(google_cloud_run_v2_service.v2_to_fhir.template[0].timeout, "s")) <= 60
    error_message = "v2-to-fhir's default request timeout must not exceed the push ack deadline (60 s)."
  }
  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.template[0].max_instance_request_concurrency == 4 && google_cloud_run_v2_service.health_sync[0].template[0].max_instance_request_concurrency == 4
    error_message = "Concurrency must match gunicorn's four threads."
  }
  assert {
    condition     = google_cloud_run_v2_service.v2_to_fhir.deletion_protection == false && google_cloud_run_v2_service.health_sync[0].deletion_protection == false
    error_message = "By default the services must be destroyable (deletion_protection = false)."
  }
}

run "ingest_enabled_creates_the_sync_trigger" {
  command = apply
  variables {
    ingest_enabled = true
  }

  assert {
    condition     = length(google_cloud_scheduler_job.health_sync) == 1 && length(google_cloud_scheduler_job.seed) == 0
    error_message = "ingest_enabled with real data must create the health-sync Scheduler job only."
  }
  assert {
    condition     = google_cloud_scheduler_job.health_sync[0].schedule == "*/15 * * * *"
    error_message = "health-sync must be triggered every 15 minutes."
  }
  assert {
    condition     = google_cloud_scheduler_job.health_sync[0].retry_config[0].retry_count == 0 && google_cloud_scheduler_job.health_sync[0].attempt_deadline == "180s"
    error_message = "The sync trigger must have retry_count = 0 and attempt_deadline = 180s."
  }
  assert {
    condition     = google_cloud_scheduler_job.health_sync[0].http_target[0].uri == "https://pulsebridge-health-sync-abc123-uc.a.run.app/sync" && google_cloud_scheduler_job.health_sync[0].http_target[0].http_method == "POST"
    error_message = "The trigger must POST to health-sync's /sync route."
  }
  assert {
    condition = (
      google_cloud_scheduler_job.health_sync[0].http_target[0].oidc_token[0].service_account_email == "pb-scheduler-invoker@example-project.iam.gserviceaccount.com" &&
      google_cloud_scheduler_job.health_sync[0].http_target[0].oidc_token[0].audience == "https://pulsebridge-health-sync-abc123-uc.a.run.app" &&
      length(google_cloud_scheduler_job.health_sync[0].http_target[0].oauth_token) == 0
    )
    error_message = "The trigger must send an OIDC token for scheduler-invoker with the service URL as audience."
  }
  assert {
    condition     = output.ingest_enabled == true
    error_message = "The ingest_enabled output must echo true."
  }
}

run "push_endpoint_output" {
  command = apply
  assert {
    condition     = output.v2_to_fhir_push_endpoint == "https://pulsebridge-v2-to-fhir-abc123-uc.a.run.app/pubsub/hl7v2"
    error_message = "The push endpoint must be the service URL plus /pubsub/hl7v2."
  }
  assert {
    condition     = output.health_sync_url == "https://pulsebridge-health-sync-abc123-uc.a.run.app" && output.seed_job_name == null
    error_message = "With real data, health_sync_url is set and seed_job_name is null."
  }
}
