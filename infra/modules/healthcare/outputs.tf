# Outputs are this module's contract with the modules that consume it (ingestion T20, phr
# T22, edge-vm T21) via the env roots. Full resource paths are what the REST API and the
# client libraries take; short names are what the MLLP adapter's flags take.

output "project_id" {
  description = "Project that holds the dataset and topics (echoed so consumers take every coordinate from one place)."
  value       = var.project_id
}

output "location" {
  description = "Healthcare API location of the dataset and stores (MLLP adapter flag --hl7_v2_location_id)."
  value       = google_healthcare_dataset.this.location
}

output "dataset_id" {
  description = "Full resource path of the dataset: projects/{project}/locations/{location}/datasets/{dataset}."
  value       = google_healthcare_dataset.this.id
}

output "dataset_name" {
  description = "Short dataset name (MLLP adapter flag --hl7_v2_dataset_id)."
  value       = google_healthcare_dataset.this.name
}

output "fhir_store_id" {
  description = "Full resource path of the FHIR R4 store: projects/{p}/locations/{l}/datasets/{d}/fhirStores/{s}."
  value       = google_healthcare_fhir_store.this.id
}

output "fhir_store_name" {
  description = "Short FHIR store name."
  value       = google_healthcare_fhir_store.this.name
}

output "fhir_base_url" {
  description = "FHIR REST base URL of the store (append e.g. /Observation?patient=...). Used by health-sync, v2-to-fhir and fhir-proxy."
  value       = "https://healthcare.googleapis.com/v1/${google_healthcare_fhir_store.this.id}/fhir"
}

output "hl7v2_store_id" {
  description = "Full resource path of the HL7v2 store: projects/{p}/locations/{l}/datasets/{d}/hl7V2Stores/{s}. Prefix of every stored message name."
  value       = google_healthcare_hl7_v2_store.this.id
}

output "hl7v2_store_name" {
  description = "Short HL7v2 store name (MLLP adapter flag --hl7_v2_store_id)."
  value       = google_healthcare_hl7_v2_store.this.name
}

output "hl7v2_ingest_url" {
  description = "REST URL for direct HTTPS ingest (POST {\"message\": {\"data\": <base64 ER7>}}), used by the cuff simulator's HTTPS transport."
  value       = "https://healthcare.googleapis.com/v1/${google_healthcare_hl7_v2_store.this.id}/messages:ingest"
}

output "mllp_adapter_store_flags" {
  description = "The four store coordinates the MLLP adapter takes as flags (spike S4), keyed by flag name without the leading dashes."
  value = {
    hl7_v2_project_id  = var.project_id
    hl7_v2_location_id = google_healthcare_dataset.this.location
    hl7_v2_dataset_id  = google_healthcare_dataset.this.name
    hl7_v2_store_id    = google_healthcare_hl7_v2_store.this.name
  }
}

output "ingest_topic_id" {
  description = "Full path of the topic the HL7v2 store publishes ingest notifications to: projects/{p}/topics/{t}."
  value       = google_pubsub_topic.ingest.id
}

output "ingest_topic_name" {
  description = "Short name of the ingest notification topic."
  value       = google_pubsub_topic.ingest.name
}

output "ingest_subscription_id" {
  description = "Full path of the ingest subscription (push to v2-to-fhir when push_enabled): projects/{p}/subscriptions/{s}. Appears as \"subscription\" in every push body."
  value       = google_pubsub_subscription.ingest.id
}

output "ingest_subscription_name" {
  description = "Short name of the ingest subscription."
  value       = google_pubsub_subscription.ingest.name
}

output "dead_letter_topic_id" {
  description = "Full path of the dead-letter topic for notifications v2-to-fhir could not handle."
  value       = google_pubsub_topic.dead_letter.id
}

output "dead_letter_topic_name" {
  description = "Short name of the dead-letter topic."
  value       = google_pubsub_topic.dead_letter.name
}

output "dead_letter_subscription_name" {
  description = "Pull subscription that keeps dead-lettered notifications for inspection."
  value       = google_pubsub_subscription.dead_letter.name
}

output "push_enabled" {
  description = "Whether the ingest subscription currently pushes to v2-to-fhir."
  value       = var.push_enabled
}

output "healthcare_service_agent_email" {
  description = "Cloud Healthcare service agent that publishes the store's notifications."
  value       = local.healthcare_service_agent
}

output "pubsub_service_agent_email" {
  description = "Pub/Sub service agent that dead-letters and signs push OIDC tokens."
  value       = local.pubsub_service_agent
}
