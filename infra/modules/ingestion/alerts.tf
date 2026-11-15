# Log-based alerting (services/health-sync/README.md, "Alerts (for T20)", and the seeder's logs).
#
# A LOG-BASED METRIC is a counter that Cloud Logging increments for every log entry matching a
# filter. The services write one JSON object per line; Cloud Run puts each such line's fields
# under jsonPayload (and its "message" field in jsonPayload.message). An ALERT POLICY watches a
# metric and notifies NOTIFICATION CHANNELS (an email address, ...) when a condition holds.
#
# Metrics always exist (they cost nothing at this volume and can be charted). Alert policies
# exist when there is somewhere to send them: alert_email (a channel created here) and/or
# notification_channels (channels made elsewhere). With neither, there are metrics only and
# nobody is told.

locals {
  # Scoped to this module's own workloads, so another service's lines can never trigger them.
  health_sync_scope = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.health_sync_name}\""
  seed_scope        = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${local.seed_name}\""

  # The Scheduler job that exists in this mode (when ingest_enabled). Its name is fixed here so
  # the filter can name it before the job exists.
  scheduler_job_name = local.deploy_health_sync ? "${local.health_sync_name}-every-15-min" : "${local.seed_name}-hourly"

  alerts = merge(
    local.deploy_health_sync ? {
      "health-sync-failed-run" = {
        # The README's filter: a run that answered 5xx needs a person (permissions, a missing
        # store, a refused Bundle); 503s (Google or FHIR down for a whole run) are included and
        # the policy auto-closes when runs succeed again. Plus the sync handler's own exception
        # (event "bug" with the message "sync failed: <type>", health_sync/app.py), which
        # writes no sync-run line. The /status handler also logs event "bug", with the message
        # "status check failed: ...", and is deliberately not matched: it is not a run.
        filter      = "${local.health_sync_scope} AND ((jsonPayload.event=\"sync-run\" AND jsonPayload.http_status>=500) OR (jsonPayload.event=\"bug\" AND jsonPayload.message=~\"^sync failed\"))"
        description = "health-sync sync runs that answered HTTP 5xx, or crashed in the sync handler."
        title       = "health-sync: sync run failed"
        severity    = "ERROR"
        doc         = "A health-sync run answered HTTP 5xx or crashed. Read its `sync-run` (or `bug`) log line: `outcome` and `reason` say which (services/health-sync/README.md, \"What Scheduler sees\")."
      }
      "health-sync-not-connected" = {
        # Both outcomes answer 200 at WARNING and need the deployer: reconnect-needed (refresh
        # token expired or revoked, client refused, scope missing) and not-configured (a secret
        # file missing, empty or unusable, for example after a secret version was disabled).
        filter      = "${local.health_sync_scope} AND jsonPayload.event=\"sync-run\" AND (jsonPayload.outcome=\"reconnect-needed\" OR jsonPayload.outcome=\"not-configured\")"
        description = "health-sync runs that could not use the Google Health connection: reconnect needed, or secrets missing or unusable."
        title       = "health-sync: Google Health not connected"
        severity    = "WARNING"
        doc         = "health-sync cannot use the Google Health connection. `outcome` = reconnect-needed: run the connect script (T11) again (in Testing status refresh tokens expire after 7 days). `outcome` = not-configured: a secret is missing or unusable; `reason` names it."
      }
    } : {},
    local.deploy_seeder ? {
      "seed-run-failed" = {
        # The seeder logs its run report as event "seed-run" at WARNING (exit 1, 2, 3 or 4) or
        # ERROR (outcome bug) when it did not succeed (pulsebridge_seed/cli.py), to stderr.
        filter      = "${local.seed_scope} AND jsonPayload.event=\"seed-run\" AND severity>=WARNING"
        description = "Seeder job executions that did not end as seeded."
        title       = "seeder: run failed"
        severity    = "ERROR"
        doc         = "A seeder execution did not end as `seeded`. Read its `seed-run` log line: `outcome` and `reason` say why (services/seed/README.md, exit codes)."
      }
    } : {},
    {
      "scheduler-attempt-failed" = {
        # Failures that write no line of the service's own: ingress refusal, a 403 from IAM, a
        # 504 at the request timeout, a crash-looping container, a refused jobs.run call.
        # Cloud Scheduler logs every attempt as resource cloud_scheduler_job (labels
        # project_id, location, job_id) with an AttemptStarted and an AttemptFinished entry
        # (Scheduler troubleshooting page); a failed attempt's AttemptFinished carries
        # debugInfo and is logged at ERROR (community evidence only; see README).
        filter      = "resource.type=\"cloud_scheduler_job\" AND resource.labels.job_id=\"${local.scheduler_job_name}\" AND jsonPayload.@type=\"type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished\" AND severity>=ERROR"
        description = "Cloud Scheduler attempts of ${local.scheduler_job_name} that failed (the target answered an error or could not be reached)."
        title       = "Scheduler: ${local.scheduler_job_name} attempt failed"
        severity    = "ERROR"
        doc         = "Cloud Scheduler could not complete an attempt of ${local.scheduler_job_name}. Its AttemptFinished log entry's `debugInfo` gives the HTTP status or URL_ERROR reason (for example a 403: a missing invoker grant; a 404 before the container logs anything: ingress)."
      }
    },
  )

  # Where alerts go: channels passed in, plus the email channel made here.
  alert_channels_requested = length(var.notification_channels) > 0 || var.alert_email != null
  alert_channels           = concat(var.notification_channels, google_monitoring_notification_channel.email[*].name)
}

resource "google_monitoring_notification_channel" "email" {
  count = var.alert_email == null ? 0 : 1

  project      = var.project_id
  display_name = "PulseBridge ${var.name_prefix} alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  user_labels = var.labels
}

resource "google_logging_metric" "this" {
  for_each = local.alerts

  project     = var.project_id
  name        = "${var.name_prefix}-${each.key}"
  description = each.value.description
  filter      = each.value.filter

  # A counter: DELTA kind, INT64 values, one per matching entry.
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "this" {
  for_each = local.alert_channels_requested ? local.alerts : {}

  project               = var.project_id
  display_name          = each.value.title
  combiner              = "OR"
  notification_channels = local.alert_channels
  severity              = each.value.severity
  user_labels           = var.labels

  conditions {
    display_name = each.value.title

    condition_threshold {
      # Log-based metrics appear in Monitoring as logging.googleapis.com/user/<metric name>.
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.this[each.key].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      # Sum the matching entries over 15 minutes (one sync interval) across all series.
      aggregations {
        alignment_period     = "900s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    # Close the incident once an hour passes without a matching entry.
    auto_close = "3600s"
  }

  documentation {
    content   = each.value.doc
    mime_type = "text/markdown"
  }
}
