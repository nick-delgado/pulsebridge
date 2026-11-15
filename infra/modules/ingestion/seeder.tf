# The synthetic-data seeder (T09) as a Cloud Run JOB. Created only when use_synthetic_data is
# true; it then replaces health-sync as the wearable source and is also the setup step that
# writes the synthetic Patient (D11). services/seed/README.md, "For T20 ... deploying it".
#
# A job, not a service: it has no port and no URL. One "execution" runs the container to
# completion and stops; nothing runs, or is billed, between executions.

locals {
  # The hourly top-up's range. With runs every hour and FINAL_AFTER_MINUTES = 60, a window must
  # stay in range for at least 60 + 2 x 60 + 5 = 185 minutes to be finalised even if one run is
  # missed (seed README, "How long --hours must be"), hence 4 hours.
  seed_scheduled_args = ["--hours", "4"]
}

resource "google_cloud_run_v2_job" "seed" {
  count = local.deploy_seeder ? 1 : 0

  project             = var.project_id
  location            = var.region
  name                = local.seed_name
  labels              = var.labels
  deletion_protection = var.deletion_protection

  template {
    # One task per execution: the seeder is a single process that batches its own Bundles.
    task_count  = 1
    parallelism = 1

    template {
      service_account = var.service_account_emails["seeder"]

      # No retries: the next hourly execution is the retry, and every write is keyed, so a
      # re-run lands on the same resources (seed README, "Re-running").
      max_retries = 0
      timeout     = var.seed_task_timeout

      containers {
        image = var.seed_image

        # No `command`: the image's ENTRYPOINT is pulsebridge-seed (T35), the single source of
        # the entrypoint; Terraform sets only the arguments. The first seeding overrides them
        # with --days 30 (README, "First run").
        args = local.seed_scheduled_args

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        # services/seed/README.md, "Configuration". No secrets: the seeder authenticates with
        # its own service account.
        dynamic "env" {
          for_each = merge(local.healthcare_env, { TIME_ZONE = var.time_zone })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
}

# Scheduler starts a job by calling the Cloud Run Admin API, `POST .../jobs/<job>:run`, which
# needs the permission run.jobs.run on that job. roles/run.invoker contains run.jobs.run (and
# run.routes.invoke, which means nothing on a job), granted here on this one job only, so the
# account can run this job and nothing else. It is NOT roles/run.jobsExecutorWithOverrides:
# the scheduled run changes no arguments, so it needs no right to override them.
resource "google_cloud_run_v2_job_iam_member" "seed_runner" {
  count = local.deploy_seeder ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.seed[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_emails["scheduler-invoker"]}"
}

# Hourly trigger, absent until ingest_enabled is true: the first seeding (--days 30), run by
# hand, writes the synthetic Patient before anything else may (D11).
resource "google_cloud_scheduler_job" "seed" {
  count = local.deploy_seeder && var.ingest_enabled ? 1 : 0

  project     = var.project_id
  region      = var.region
  name        = "${local.seed_name}-hourly"
  description = "PulseBridge: run the synthetic-data seeder job hourly (--hours 4)."

  # Minute 7, not 0: many schedules fire on the hour, and this one does not need to.
  schedule  = "7 * * * *"
  time_zone = "Etc/UTC"

  # Scheduler only waits for the Admin API to accept the execution, not for the job to finish.
  attempt_deadline = "180s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    # Cloud Run Admin API v2: jobs.run on projects/<p>/locations/<r>/jobs/<name>. Built from its
    # parts rather than from the resource's `id`, whose format is the provider's business.
    uri = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.seed[0].name}:run"

    # A Google API takes an OAuth access token, not an OIDC ID token (that is for Cloud Run
    # services and other HTTPS endpoints). The scope defaults to cloud-platform.
    oauth_token {
      service_account_email = var.service_account_emails["scheduler-invoker"]
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.seed_runner]
}
