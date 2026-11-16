# IAM in this module is always resource-level (on one topic, subscription, store or service
# account), never project-level, and always additive (_iam_member), so it cannot remove
# bindings other modules make on the same resources.

# No data source here (T38). The module used to read data.google_project itself for the project
# number. The stack calls this module with nothing that defers a read now, but any data source
# inside a module whose call has `depends_on` is read only at apply whenever anything it depends
# on has a pending change; the number then becomes "(known after apply)", and because `member`
# forces replacement, every agent grant was planned for replacement when an unrelated API was
# enabled (dev, 16 November). Taking the number as an input value removes the hazard: an input
# that is known at plan stays known, whatever the module call depends on.

locals {
  # Google-managed service agents (a service agent is a Google-managed service account that a
  # Google service acts as inside the project). Addresses follow Google's documented pattern
  # service-<project number>@gcp-sa-<service>.iam.gserviceaccount.com.
  healthcare_service_agent = "service-${var.project_number}@gcp-sa-healthcare.iam.gserviceaccount.com"
  # From module identity, which creates this agent; the variable's validation checks it is the
  # pattern above for this project number.
  pubsub_service_agent = var.pubsub_service_agent_email
}

# ---------------------------------------------------------------------------------------------
# Service agents
# ---------------------------------------------------------------------------------------------

# Make sure the Cloud Healthcare service agent exists before it is granted anything. Google
# creates some service agents lazily, on first use, rather than when the API is enabled; the
# Pub/Sub one did not exist on a new project (module identity README, T36). The Healthcare one
# did exist on the dev deploy right after the API was enabled (checklist A6), but nothing Google
# publishes that this repo could check promises it, so the module asks for it explicitly, the same
# way module identity does for Pub/Sub. Service Usage's services.generateServiceIdentity creates
# the agent if missing and returns it if it exists, so on a project where it exists this only adds
# a state entry. Beta provider only. It cannot be updated, imported or really destroyed (destroy
# drops it from state; the agent stays, as every service agent does).
resource "google_project_service_identity" "healthcare" {
  provider = google-beta

  project = var.project_id
  service = "healthcare.googleapis.com"

  lifecycle {
    # The reference to var.enabled_apis is also what orders this after the API's enablement:
    # Terraform adds a dependency for every reference, including those in conditions.
    precondition {
      condition     = contains(var.enabled_apis, "healthcare.googleapis.com")
      error_message = "enabled_apis must include healthcare.googleapis.com (module project-base's enabled_apis output)."
    }
    postcondition {
      # An empty or null email proves nothing either way (the API does not promise one for every
      # service), so only a different address fails the apply.
      condition     = self.email == null ? true : contains(["", local.healthcare_service_agent], self.email)
      error_message = "The Healthcare service agent Google returned is not service-<project number>@gcp-sa-healthcare.iam.gserviceaccount.com, the address the publisher grant assumes."
    }
  }
}

# The Cloud Healthcare service agent publishes the HL7v2 store's notifications. Publisher on
# this one topic only. Spike S5 found the project-level roles/healthcare.serviceAgent (added
# by Google when the API is enabled) already allows it; the explicit topic binding keeps the
# module working where that default grant has been removed.
resource "google_pubsub_topic_iam_member" "healthcare_agent_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.ingest.name
  role    = "roles/pubsub.publisher"
  # Built from the project number (known at plan), NOT from the service identity's `member`
  # attribute, which is "(known after apply)" while that resource is new: `member` forces
  # replacement, so the deployed grant would be destroyed and re-created for nothing.
  member = "serviceAccount:${local.healthcare_service_agent}"

  # Because the member does not reference the service identity, the order is stated here. A
  # resource-level depends_on only orders the two; it makes no value unknown.
  depends_on = [google_project_service_identity.healthcare]
}

# Dead-lettering is done by the Pub/Sub service agent: it must publish to the dead-letter
# topic and acknowledge (subscriber) the original on the source subscription. Both grants take
# the address from var.pubsub_service_agent_email; in the stack that is module identity's output,
# which is declared to depend on identity's google_project_service_identity.pubsub, so these
# grants are created after the agent in a single apply. The value itself is built from the
# project number, so it is known at plan.
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
  # Which narrowest predefined role each account gets on each store: `fhir` is the main FHIR
  # store (health data), `fhir_audit` the audit FHIR store (D15), `hl7v2` the HL7v2 store.
  # Keys are module identity's account names. Accounts absent from
  # var.store_iam_service_accounts get nothing, and keys of that map not listed here
  # (scheduler-invoker, pubsub-push, ...) are ignored.
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
      fhir       = "roles/healthcare.fhirResourceReader" # search and read only
      fhir_audit = "roles/healthcare.fhirResourceReader" # messages log: read AuditEvents (D12, D15)
      hl7v2      = "roles/healthcare.hl7V2Consumer"      # messages.list (label filter) + messages.get
    }
    "edge-vm" = {
      hl7v2 = "roles/healthcare.hl7V2Ingest" # MLLP adapter -> messages:ingest
    }
    "cuff-sim" = {
      # D15: the simulator records each send attempt as an AuditEvent (D12) in the audit store
      # only, with PUT AuditEvent/<id>. It has no binding on the main FHIR store, which holds
      # health data. fhirResourceEditor also lets it read, search and delete audit records;
      # Google's role list shows no predefined FHIR role that writes without reading. See the
      # README's IAM section.
      fhir_audit = "roles/healthcare.fhirResourceEditor" # write AuditEvent per send attempt
      hl7v2      = "roles/healthcare.hl7V2Ingest"        # HTTPS transport -> messages:ingest
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

  fhir_audit_store_bindings = {
    for account, roles in local.store_role_matrix :
    account => {
      role   = roles.fhir_audit
      member = "serviceAccount:${var.store_iam_service_accounts[account]}"
    }
    if contains(keys(var.store_iam_service_accounts), account) && contains(keys(roles), "fhir_audit")
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

# These three role-table-driven resources (app, audit, and the HL7v2 one below) are the only
# store IAM this module may declare; tests/check_store_iam.sh fails on any other.
resource "google_healthcare_fhir_store_iam_member" "app" {
  for_each = local.fhir_store_bindings

  fhir_store_id = google_healthcare_fhir_store.this.id
  role          = each.value.role
  member        = each.value.member
}

# Audit store (D15): only cuff-sim (editor) and fhir-proxy (reader).
resource "google_healthcare_fhir_store_iam_member" "audit" {
  for_each = local.fhir_audit_store_bindings

  fhir_store_id = google_healthcare_fhir_store.audit.id
  role          = each.value.role
  member        = each.value.member
}

resource "google_healthcare_hl7_v2_store_iam_member" "app" {
  for_each = local.hl7v2_store_bindings

  hl7_v2_store_id = google_healthcare_hl7_v2_store.this.id
  role            = each.value.role
  member          = each.value.member
}
