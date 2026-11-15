# Log-based alerting for health-sync (services/health-sync/README.md, "Alerts (for T20)").
#
# A LOG-BASED METRIC is a counter that Cloud Logging increments for every log entry matching a
# filter. health-sync writes one JSON line per run ("event": "sync-run"); Cloud Run turns each
# JSON line on stdout into a log entry whose fields are under jsonPayload. An ALERT POLICY
# watches a metric and notifies a channel (email, SMS, ...) when a condition holds.
#
# Metrics always exist with health-sync (they cost nothing at this volume and can be charted);
# alert policies only when notification_channels is non-empty, so "no channel" means "metrics
# only" and no alerting charge.

locals {
  # Both filters are scoped to this service's revisions, so another service's lines could never
  # trigger them.
  health_sync_log_scope = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.health_sync_name}\""

  health_sync_alerts = local.deploy_health_sync ? {
    "failed-run" = {
      # The README's filter: a run that answered 5xx needs a person (permissions, a missing
      # store, a refused Bundle). 503s (Google or FHIR briefly unavailable) are included: the
      # next run usually heals them, which the policy's auto-close reflects. Plus the one failure
      # that writes no sync-run line: an exception in the handler itself, logged as
      # event "bug" before the 500 (health_sync/app.py).
      filter      = "${local.health_sync_log_scope} AND ((jsonPayload.event=\"sync-run\" AND jsonPayload.http_status>=500) OR jsonPayload.event=\"bug\")"
      description = "health-sync runs that ended with HTTP 5xx (see the run's outcome and reason)."
      title       = "health-sync: sync run failed"
      doc         = "A health-sync run answered HTTP 5xx. Read the run's `sync-run` log line: `outcome` and `reason` say which (services/health-sync/README.md, \"What Scheduler sees\")."
    }
    "reconnect-needed" = {
      filter      = "${local.health_sync_log_scope} AND jsonPayload.event=\"sync-run\" AND jsonPayload.outcome=\"reconnect-needed\""
      description = "health-sync runs that found the Google Health connection unusable (refresh token expired or revoked, client refused, scope missing)."
      title       = "health-sync: Google Health reconnect needed"
      doc         = "The Google Health refresh token no longer works. Run the connect script (T11) again. In Testing status Google's refresh tokens expire after 7 days."
    }
  } : {}
}

resource "google_logging_metric" "health_sync" {
  for_each = local.health_sync_alerts

  project     = var.project_id
  name        = "${local.health_sync_name}-${each.key}"
  description = each.value.description
  filter      = each.value.filter

  # A counter: DELTA kind, INT64 values, one per matching entry.
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "health_sync" {
  for_each = length(var.notification_channels) > 0 ? local.health_sync_alerts : {}

  project               = var.project_id
  display_name          = each.value.title
  combiner              = "OR"
  notification_channels = var.notification_channels
  severity              = each.key == "reconnect-needed" ? "WARNING" : "ERROR"

  conditions {
    display_name = each.value.title

    condition_threshold {
      # Log-based metrics appear in Monitoring as logging.googleapis.com/user/<metric name>.
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.health_sync[each.key].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      # Sum the matching lines over 15 minutes, one run's worth.
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
    # Close the incident once an hour passes without a matching line (four clean runs).
    auto_close = "3600s"
  }

  documentation {
    content   = each.value.doc
    mime_type = "text/markdown"
  }
}
