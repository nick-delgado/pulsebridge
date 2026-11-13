# Cloud Healthcare API: one dataset holding a FHIR R4 store and an HL7v2 store.
#
# A dataset is a regional container; the stores inside it hold the data. Deleting the dataset
# deletes every store and every resource or message in it.

locals {
  dataset_name     = var.name_prefix
  fhir_store_name  = "${var.name_prefix}-fhir"
  hl7v2_store_name = "${var.name_prefix}-hl7v2"
}

resource "google_healthcare_dataset" "this" {
  project   = var.project_id
  name      = local.dataset_name
  location  = var.location
  time_zone = var.time_zone

  # DELETE in dev (destroyed nightly, nothing billable may remain); PREVENT in prod.
  deletion_policy = var.deletion_policy
}

# ---------------------------------------------------------------------------------------------
# FHIR R4 store: the system of record for Patient, Device, Observation and Provenance.
# Every setting below is set on purpose; several are immutable after creation, and changing
# them in this file would replace the store and delete its data.
# ---------------------------------------------------------------------------------------------
resource "google_healthcare_fhir_store" "this" {
  name    = local.fhir_store_name
  dataset = google_healthcare_dataset.this.id

  # FHIR release 4.0.1 (R4), the version every writer and the HL7 validator (T08) use.
  version = "R4"

  # Update-create: lets a client create a resource with an ID it chose, using an update
  # (PUT Observation/<id>) or a conditional update whose body carries an id. The writers
  # (T06, T09, T10, T12) are idempotent on client-derived keys; with this off, any PUT to an
  # ID the store has not seen yet fails, e.g. the first sync after a nightly dev rebuild.
  # A plain conditional update without an id works either way. Client-chosen IDs appear in
  # audit logs and Pub/Sub, so they must be opaque (hashes), never patient identifiers.
  enable_update_create = true

  # Referential integrity ON (the API default): a write is refused if it references a
  # resource that does not exist, so an Observation can never point at a missing Patient or
  # Device. Consequences for writers:
  # - Inside a transaction Bundle the store resolves all entries together, so order does not
  #   matter. A reference to a resource created in the same Bundle may use the entry's
  #   fullUrl (urn:uuid:...) or a plain Patient/<id> when that entry PUTs to a client-chosen
  #   id (from knowledge of the API; confirm on first apply).
  # - Outside a transaction, write Patient and Device before the Observation, and the
  #   Observation before its Provenance.
  # - Deletes must go referrers first: deleting an Observation fails while a Provenance
  #   targets it, and deleting a Patient fails while Observations reference it. Cleanup or
  #   regeneration (T09, manual fixes) deletes Provenance, then Observation, then Patient.
  #   ($purge only removes a resource's old versions; it does not bypass this check.)
  #   Deleting the whole store or dataset (terraform destroy) is unaffected.
  # Immutable after creation.
  disable_referential_integrity = false

  # Versioning ON (the API default): every update keeps the previous version, readable with
  # _history. health-sync rewrites overlapping roll-up windows on each run, and history shows
  # how a partly synced window changed. Costs a little storage (about 1 KB per version).
  # Immutable after creation.
  disable_resource_versioning = false

  # History import OFF: only for bulk-importing past resource versions, which nothing here
  # does; leaving it off means no import can fabricate history that "happened" in the past.
  enable_history_import = false

  # Parse references inside complex data types (for example inside extensions) too, so
  # referential integrity applies to every reference. Set explicitly because Google plans to
  # flip the default for new stores; turning it on later can fail on existing data.
  complex_data_type_reference_parsing = "ENABLED"

  # Strict search: an unknown search parameter is an error, not silently ignored. With the
  # FHIR default (lenient), a typo such as `dat=2026-11-01` would return every Observation.
  default_search_handling_strict = true

  # No notification_configs: nothing consumes FHIR change events (the dashboard reads through
  # fhir-proxy), and publishing ~300 events a day to an unread topic would be pure noise.

  # validation_config is left at its defaults: required-field, reference-type, FHIRPath and
  # base-profile validation all stay ON.

  labels          = var.labels
  deletion_policy = var.deletion_policy
}

# ---------------------------------------------------------------------------------------------
# HL7v2 store: receives ORU^R01 messages from the MLLP adapter (edge VM) and from the cuff
# simulator's HTTPS transport, replies with the commit-level ACK, and notifies Pub/Sub.
# ---------------------------------------------------------------------------------------------
resource "google_healthcare_hl7_v2_store" "this" {
  name    = local.hl7v2_store_name
  dataset = google_healthcare_dataset.this.id

  # Default (false), stated so a reader sees it: resending identical bytes returns AA, stores
  # nothing new and publishes no second notification (spikes S4, S5). true would turn a
  # harmless resend into an AE "message already exists".
  reject_duplicate_message = false

  parser_config {
    # The parser that fills a stored message's parsedData. Immutable after creation (S4).
    # v2-to-fhir parses the raw bytes with hl7apy (S5), so parsedData is not used; V1 is set
    # explicitly because it is the version whose ingest and NACK behaviour spikes S4 and S5
    # recorded, and which the D7 malformed-message samples rely on.
    version = var.hl7v2_parser_version

    # Default (false): a message without an MSH header is rejected with AE "Invalid MSH
    # header". That NACK is one of the D7 demo cases.
    allow_null_header = false

    # segment_terminator is left unset, so segments must end in '\r' as HL7v2 requires; a
    # message with '\n' terminators is rejected with AE (D7 "unparseable" class).
    # schema is left unset: no schematized parsing, nothing consumes it.
  }

  # One notification per newly stored message (ingest or create): the Pub/Sub message's data
  # is the stored message's resource name and its only attribute is msgType (S5).
  # No filter, on purpose: D7 wants messages with an empty or unknown MSH-9 to reach
  # v2-to-fhir and end as an application-level error; a `message_type = "ORU"` filter would
  # leave them with no outcome at all (S5).
  notification_configs {
    pubsub_topic = google_pubsub_topic.ingest.id
  }

  labels          = var.labels
  deletion_policy = var.deletion_policy

  # Creating a store that points at a topic it cannot publish to fails with 403 (S5), so the
  # service agent's publisher binding must exist first (checked by tests/check_ordering.sh).
  depends_on = [google_pubsub_topic_iam_member.healthcare_agent_publisher]
}
