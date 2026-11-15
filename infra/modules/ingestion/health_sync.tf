# health-sync (T10): pulls five-minute heart-rate roll-ups from the Google Health API and writes
# them to the FHIR store. Created only for a real-data deployment (use_synthetic_data = false).
# Its settings follow services/health-sync/README.md, "For T20".

resource "google_cloud_run_v2_service" "health_sync" {
  count = local.deploy_health_sync ? 1 : 0

  project     = var.project_id
  location    = var.region
  name        = local.health_sync_name
  description = "PulseBridge health-sync: Google Health heart-rate roll-ups to FHIR R4 Observations, every 15 minutes."
  labels      = var.labels

  # Internal-only by default: Cloud Scheduler in the same project counts as internal. fhir-proxy
  # must route its /status call through the VPC for it to count (README, "Ingress").
  ingress              = var.health_sync_ingress
  invoker_iam_disabled = false
  deletion_protection  = var.deletion_protection

  template {
    service_account = var.service_account_emails["health-sync"]

    # min 0: scale to zero between runs. max 1: the in-process "one run at a time" lock then
    # covers the whole service, so a second POST /sync can never start a parallel run on
    # another instance (README "Deadline: one run at a time").
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    max_instance_request_concurrency = 4

    # At least the Scheduler attempt deadline (180 s, below), so Cloud Run never cuts a run that
    # Scheduler is still waiting for. The run itself stops at RUN_DEADLINE_SECONDS (150 s).
    timeout = var.health_sync_request_timeout

    # One secret volume per Google Health secret. A secret VOLUME is read from Secret Manager on
    # every file read, always at the version named here, "latest"; a secret ENVIRONMENT
    # VARIABLE is resolved once at instance start-up. Files let the next run pick up a
    # reconnect (a new refresh token version) without a redeploy, and keep the values out of
    # the service's environment and configuration.
    dynamic "volumes" {
      for_each = local.google_health_secrets
      content {
        name = volumes.key
        secret {
          secret = var.secret_ids[volumes.key]
          items {
            version = "latest"
            path    = "value"
          }
        }
      }
    }

    containers {
      image = var.health_sync_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = false
      }

      # /secrets/<secret key>/value, one directory per secret.
      dynamic "volume_mounts" {
        for_each = local.google_health_secrets
        content {
          name       = volume_mounts.key
          mount_path = "/secrets/${volume_mounts.key}"
        }
      }

      # services/health-sync/README.md, "Configuration". Only file PATHS are in the environment;
      # no secret value is ever an environment variable (tests/static_checks.sh refuses
      # value_source / secret_key_ref anywhere in this module).
      dynamic "env" {
        for_each = merge(
          local.healthcare_env,
          {
            TIME_ZONE             = var.time_zone
            SYNC_INTERVAL_MINUTES = tostring(local.sync_interval_minutes)
          },
          { for key, env_name in local.google_health_secrets : env_name => "/secrets/${key}/value" },
        )
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}

# The sync trigger. Absent (count 0) until ingest_enabled is true: the setup step (the connect
# script, T11) must write the full Patient first (D11), or the first sync would create an
# identifier-only one by conditional create and race the setup.
resource "google_cloud_scheduler_job" "health_sync" {
  count = local.deploy_health_sync && var.ingest_enabled ? 1 : 0

  project     = var.project_id
  region      = var.region
  name        = "${local.health_sync_name}-every-15-min"
  description = "PulseBridge: POST /sync on health-sync every ${local.sync_interval_minutes} minutes."

  schedule  = "*/${local.sync_interval_minutes} * * * *"
  time_zone = "Etc/UTC" # a fixed interval is the same in every zone; UTC has no DST gaps

  # Scheduler waits this long for the answer (180 s is the HTTP-target default, stated
  # explicitly because health-sync's run budget is sized to it).
  attempt_deadline = "180s"

  # No retries: the next scheduled run is the retry, and its range overlaps this one. A retry
  # could also start a second run while the first is still writing.
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.health_sync[0].uri}/sync"

    # Scheduler mints an OIDC ID token for scheduler-invoker. Cloud Run checks its audience
    # against the service URL, so set it to the URL; Scheduler's default would be the full
    # target URI including "/sync".
    oidc_token {
      service_account_email = var.service_account_emails["scheduler-invoker"]
      audience              = google_cloud_run_v2_service.health_sync[0].uri
    }
  }

  # The invoker binding must exist before the first call, or the first runs answer 403.
  depends_on = [google_cloud_run_v2_service_iam_member.invoker]
}
