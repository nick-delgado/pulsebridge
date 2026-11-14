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

# Not here: the Pub/Sub service agent's roles/iam.serviceAccountTokenCreator on the push
# service account (needed to mint the push OIDC token). Module identity owns that one grant;
# if two modules managed the same IAM member, destroying either would remove the other's grant.
# tests/check_ordering.sh fails if a token-creator grant reappears in this module.

# ---------------------------------------------------------------------------------------------
# Store-level bindings for the application service accounts (module identity creates them)
# ---------------------------------------------------------------------------------------------

locals {
  # Which narrowest predefined role each account gets on each store. Keys are module
  # identity's account names. Accounts absent from var.store_iam_service_accounts get nothing,
  # and keys of that map not listed here (scheduler-invoker, pubsub-push, ...) are ignored.
  # hl7V2Consumer is described by Google as "list and read HL7v2 messages, update message
  # labels, and publish new messages": it carries healthcare.hl7V2Messages.update, which
  # messages.patch (labels only) requires. hl7V2Editor would add delete and ingest.
  store_role_matrix = {
    "health-sync" = {
      fhir = "roles/healthcare.fhirResourceEditor" # conditional update / executeBundle of Observations
    }
    "seeder" = {
      fhir = "roles/healthcare.fhirResourceEditor" # synthetic-data seeder (T09), same write pattern
    }
    "v2-to-fhir" = {
      fhir  = "roles/healthcare.fhirResourceEditor" # write Patient, Device, Observation, Provenance
      hl7v2 = "roles/healthcare.hl7V2Consumer"      # messages.get + messages.patch (outcome labels, S5)
    }
    "fhir-proxy" = {
      fhir  = "roles/healthcare.fhirResourceReader" # search and read only
      hl7v2 = "roles/healthcare.hl7V2Consumer"      # messages.list (label filter) + messages.get
    }
    "edge-vm" = {
      hl7v2 = "roles/healthcare.hl7V2Ingest" # MLLP adapter -> messages:ingest
    }
    "cuff-sim" = {
      hl7v2 = "roles/healthcare.hl7V2Ingest" # HTTPS transport -> messages:ingest
    }
  }

  # Iterate over this module's own account names and keep those the caller supplied. The
  # for_each keys therefore depend only on key names (known at plan time), never on the
  # emails, which may still be unknown on the first apply.
  fhir_store_bindings = {
    for account, roles in local.store_role_matrix :
    account => {
      role   = roles.fhir
      member = "serviceAccount:${var.store_iam_service_accounts[account]}"
    }
    if contains(keys(var.store_iam_service_accounts), account) && contains(keys(roles), "fhir")
  }

  hl7v2_store_bindings = {
    for account, roles in local.store_role_matrix :
    account => {
      role   = roles.hl7v2
      member = "serviceAccount:${var.store_iam_service_accounts[account]}"
    }
    if contains(keys(var.store_iam_service_accounts), account) && contains(keys(roles), "hl7v2")
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
