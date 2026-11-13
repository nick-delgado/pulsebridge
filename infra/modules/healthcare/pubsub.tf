# Notification path: HL7v2 store -> ingest topic -> ingest subscription -> v2-to-fhir.
# Failed deliveries end up on the dead-letter topic, which has its own pull subscription.

locals {
  ingest_topic_name             = "${var.name_prefix}-hl7v2-ingest"
  ingest_subscription_name      = "${var.name_prefix}-hl7v2-ingest-push"
  dead_letter_topic_name        = "${var.name_prefix}-hl7v2-ingest-dead-letter"
  dead_letter_subscription_name = "${var.name_prefix}-hl7v2-ingest-dead-letter-pull"

  # Cloud Run checks the OIDC token's audience against the service URL, so by default use
  # the endpoint's origin (scheme + host), not the full URL with the handler path.
  push_audience = var.push_audience != null ? var.push_audience : (
    var.push_endpoint != null ? regex("^https://[^/]+", var.push_endpoint) : null
  )
}

resource "google_pubsub_topic" "ingest" {
  project = var.project_id
  name    = local.ingest_topic_name
  labels  = var.labels
  # No topic-level message retention: it is billed, and the subscription already keeps
  # unacknowledged notifications for message_retention_duration.
}

resource "google_pubsub_topic" "dead_letter" {
  project = var.project_id
  name    = local.dead_letter_topic_name
  labels  = var.labels
}

# The subscription v2-to-fhir receives notifications from. It always exists; push_config is
# added only when push_enabled is true. Until then it is a pull subscription nobody pulls, so
# notifications wait in it (up to message_retention_duration) and are pushed once the
# v2-to-fhir endpoint is configured. Switching pull to push is an in-place update.
resource "google_pubsub_subscription" "ingest" {
  project = var.project_id
  name    = local.ingest_subscription_name
  topic   = google_pubsub_topic.ingest.id
  labels  = var.labels

  ack_deadline_seconds       = var.ack_deadline_seconds
  message_retention_duration = var.message_retention_duration
  retain_acked_messages      = false

  # Never expire. By default Pub/Sub deletes a subscription after 31 days without activity,
  # which would silently break a demo that sits idle for a month.
  expiration_policy {
    ttl = ""
  }

  # Exponential backoff between redeliveries, so a failing endpoint is not hammered.
  retry_policy {
    minimum_backoff = var.retry_minimum_backoff
    maximum_backoff = var.retry_maximum_backoff
  }

  # After max_delivery_attempts failed deliveries the notification is moved to the
  # dead-letter topic instead of being retried for the full retention period. v2-to-fhir
  # acknowledges mapping errors itself (D7), so what lands here is a notification the
  # service could not handle at all (crash, timeout, FHIR store down for ~40 minutes).
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }

  dynamic "push_config" {
    for_each = var.push_enabled ? [1] : []
    content {
      push_endpoint = var.push_endpoint

      # Pub/Sub attaches "Authorization: Bearer <OIDC token>" signed as this service account;
      # Cloud Run rejects the request unless that account has roles/run.invoker on the service.
      oidc_token {
        service_account_email = var.push_service_account_email
        audience              = local.push_audience
      }
      # Default (wrapped) body: {"message": {"data", "attributes", "messageId", ...},
      # "subscription": ...}. v2-to-fhir reads message.data (the stored message's name) and
      # message.attributes.msgType (S5).
    }
  }

  # The Pub/Sub service agent must be able to publish to the dead-letter topic before
  # dead-lettering works, so the grant comes first (tests/check_ordering.sh checks this edge;
  # terraform test cannot). The token-creator grant that push needs lives in module identity;
  # passing its push account email in creates that ordering in the root.
  depends_on = [google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher]
}

# Dead-lettered notifications are kept here for inspection (`gcloud pubsub subscriptions pull`).
# A topic with no subscription drops every message published to it.
resource "google_pubsub_subscription" "dead_letter" {
  project = var.project_id
  name    = local.dead_letter_subscription_name
  topic   = google_pubsub_topic.dead_letter.id
  labels  = var.labels

  message_retention_duration = var.message_retention_duration
  retain_acked_messages      = false

  expiration_policy {
    ttl = ""
  }
}
