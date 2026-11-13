# IAM in this module is always resource-level (on one topic, subscription, store or service
# account), never project-level, and always additive (_iam_member), so it cannot remove
# bindings other modules make on the same resources.

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Google-managed service agents. They exist once the API is enabled (module project-base).
  healthcare_service_agent = "service-${data.google_project.this.number}@gcp-sa-healthcare.iam.gserviceaccount.com"
  pubsub_service_agent     = "service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# ---------------------------------------------------------------------------------------------
# Service agents
# ---------------------------------------------------------------------------------------------

# The Cloud Healthcare service agent publishes the HL7v2 store's notifications. Publisher on
# this one topic only. Spike S5 found the project-level roles/healthcare.serviceAgent (added
# by Google when the API is enabled) already allows it; the explicit topic binding keeps the
# module working where that default grant has been removed.
resource "google_pubsub_topic_iam_member" "healthcare_agent_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.ingest.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${local.healthcare_service_agent}"
}

# Dead-lettering is done by the Pub/Sub service agent: it must publish to the dead-letter
# topic and acknowledge (subscriber) the original on the source subscription.
resource "google_pubsub_topic_iam_member" "pubsub_agent_dead_letter_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${local.pubsub_service_agent}"
}

resource "google_pubsub_subscription_iam_member" "pubsub_agent_ingest_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.ingest.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${local.pubsub_service_agent}"
}

# To sign the push request's OIDC token, the Pub/Sub service agent creates tokens as the push
# service account. Granted on that one account, not project-wide.
resource "google_service_account_iam_member" "pubsub_agent_token_creator" {
  count = var.push_enabled && var.grant_pubsub_token_creator ? 1 : 0

  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.push_service_account_email}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${local.pubsub_service_agent}"
}

# ---------------------------------------------------------------------------------------------
# Store-level bindings for the application service accounts (module identity creates them)
# ---------------------------------------------------------------------------------------------

locals {
  # Which narrowest predefined role each account gets on each store. Accounts absent from
  # var.store_iam_service_accounts get nothing.
  # hl7V2Consumer is described by Google as "list and read HL7v2 messages, update message
  # labels, and publish new messages": it carries healthcare.hl7V2Messages.update, which
  # messages.patch (labels only) requires. hl7V2Editor would add delete and ingest.
  store_role_matrix = {
    health_sync = {
      fhir = "roles/healthcare.fhirResourceEditor" # conditional update / executeBundle of Observations
    }
    v2_to_fhir = {
      fhir  = "roles/healthcare.fhirResourceEditor" # write Patient, Device, Observation, Provenance
      hl7v2 = "roles/healthcare.hl7V2Consumer"      # messages.get + messages.patch (outcome labels, S5)
    }
    fhir_proxy = {
      fhir  = "roles/healthcare.fhirResourceReader" # search and read only
      hl7v2 = "roles/healthcare.hl7V2Consumer"      # messages.list (label filter) + messages.get
    }
    edge_vm = {
      hl7v2 = "roles/healthcare.hl7V2Ingest" # MLLP adapter -> messages:ingest
    }
    cuff_sim = {
      hl7v2 = "roles/healthcare.hl7V2Ingest" # HTTPS transport -> messages:ingest
    }
  }

  # Keys depend only on the map's keys, which the caller writes literally, so for_each is
  # known at plan time even when the emails are not yet.
  fhir_store_bindings = {
    for account, email in var.store_iam_service_accounts :
    account => {
      role   = local.store_role_matrix[account].fhir
      member = "serviceAccount:${email}"
    }
    if contains(keys(local.store_role_matrix[account]), "fhir")
  }

  hl7v2_store_bindings = {
    for account, email in var.store_iam_service_accounts :
    account => {
      role   = local.store_role_matrix[account].hl7v2
      member = "serviceAccount:${email}"
    }
    if contains(keys(local.store_role_matrix[account]), "hl7v2")
  }
}

resource "google_healthcare_fhir_store_iam_member" "app" {
  for_each = local.fhir_store_bindings

  fhir_store_id = google_healthcare_fhir_store.this.id
  role          = each.value.role
  member        = each.value.member
}

resource "google_healthcare_hl7_v2_store_iam_member" "app" {
  for_each = local.hl7v2_store_bindings

  hl7_v2_store_id = google_healthcare_hl7_v2_store.this.id
  role            = each.value.role
  member          = each.value.member
}
