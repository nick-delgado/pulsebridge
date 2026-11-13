# Default inputs: no push endpoint, no application service accounts.
# mock_provider needs no credentials and creates nothing; `command = apply` makes the mock
# fill in computed values (IDs) so references between resources can be checked.

mock_provider "google" {
  # Fixed, synthetic coordinates so assertions can compare against literal strings.
  override_data {
    target = data.google_project.this
    values = { number = "123456789012" }
  }
  override_resource {
    target = google_healthcare_dataset.this
    values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge" }
  }
  override_resource {
    target = google_healthcare_fhir_store.this
    values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir" }
  }
  override_resource {
    target = google_healthcare_hl7_v2_store.this
    values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2" }
  }
  override_resource {
    target = google_pubsub_topic.ingest
    values = { id = "projects/example-project/topics/pulsebridge-hl7v2-ingest" }
  }
  override_resource {
    target = google_pubsub_topic.dead_letter
    values = { id = "projects/example-project/topics/pulsebridge-hl7v2-ingest-dead-letter" }
  }
}

variables {
  project_id = "example-project"
}

run "fhir_store_is_r4_with_chosen_settings" {
  command = apply

  assert {
    condition     = google_healthcare_fhir_store.this.version == "R4"
    error_message = "FHIR store must be R4."
  }
  assert {
    condition     = google_healthcare_fhir_store.this.enable_update_create == true
    error_message = "update-create must be on: writers PUT to client-derived IDs."
  }
  assert {
    condition     = google_healthcare_fhir_store.this.disable_referential_integrity == false
    error_message = "Referential integrity must stay on."
  }
  assert {
    condition     = google_healthcare_fhir_store.this.disable_resource_versioning == false
    error_message = "Resource versioning must stay on."
  }
  assert {
    condition     = google_healthcare_fhir_store.this.enable_history_import == false
    error_message = "History import must be off."
  }
  assert {
    condition     = google_healthcare_fhir_store.this.complex_data_type_reference_parsing == "ENABLED"
    error_message = "Complex data type reference parsing must be ENABLED explicitly."
  }
  assert {
    condition     = google_healthcare_fhir_store.this.default_search_handling_strict == true
    error_message = "Search handling must default to strict."
  }
  assert {
    condition     = length(google_healthcare_fhir_store.this.notification_configs) == 0 && length(google_healthcare_fhir_store.this.notification_config) == 0
    error_message = "The FHIR store must have no notification config."
  }
  assert {
    condition     = google_healthcare_fhir_store.this.dataset == "projects/example-project/locations/us-central1/datasets/pulsebridge"
    error_message = "The FHIR store must live in the module's dataset."
  }
}

run "hl7v2_store_notifies_the_ingest_topic" {
  command = apply

  assert {
    condition     = length(google_healthcare_hl7_v2_store.this.notification_configs) == 1
    error_message = "The HL7v2 store must have exactly one notification config."
  }
  assert {
    condition     = google_healthcare_hl7_v2_store.this.notification_configs[0].pubsub_topic == "projects/example-project/topics/pulsebridge-hl7v2-ingest"
    error_message = "The notification config must target the module's ingest topic."
  }
  assert {
    # S5: a filter would stop empty-MSH-9 messages reaching the mapper (D7).
    condition     = try(google_healthcare_hl7_v2_store.this.notification_configs[0].filter, null) == null || google_healthcare_hl7_v2_store.this.notification_configs[0].filter == ""
    error_message = "The notification config must have no filter."
  }
  assert {
    condition     = google_healthcare_hl7_v2_store.this.dataset == "projects/example-project/locations/us-central1/datasets/pulsebridge"
    error_message = "The HL7v2 store must live in the module's dataset."
  }
}

run "hl7v2_parser_config_is_explicit_and_defaults_kept" {
  command = apply

  assert {
    condition     = length(google_healthcare_hl7_v2_store.this.parser_config) == 1 && google_healthcare_hl7_v2_store.this.parser_config[0].version == "V1"
    error_message = "parser_config.version must be set explicitly to V1."
  }
  assert {
    condition     = google_healthcare_hl7_v2_store.this.parser_config[0].allow_null_header == false
    error_message = "allow_null_header must stay at its default (false)."
  }
  assert {
    condition     = google_healthcare_hl7_v2_store.this.reject_duplicate_message == false
    error_message = "reject_duplicate_message must stay at its default (false)."
  }
  assert {
    condition     = google_healthcare_hl7_v2_store.this.parser_config[0].segment_terminator == null && google_healthcare_hl7_v2_store.this.parser_config[0].schema == null
    error_message = "segment_terminator and schema must be unset (\\r terminator, unschematized)."
  }
}

run "subscription_is_pull_with_dead_letter_when_no_endpoint" {
  command = apply

  assert {
    condition     = length(google_pubsub_subscription.ingest.push_config) == 0
    error_message = "Without push_enabled the ingest subscription must have no push config."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.topic == "projects/example-project/topics/pulsebridge-hl7v2-ingest"
    error_message = "The ingest subscription must be attached to the ingest topic."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.dead_letter_policy[0].dead_letter_topic == "projects/example-project/topics/pulsebridge-hl7v2-ingest-dead-letter"
    error_message = "The ingest subscription must dead-letter to the module's dead-letter topic."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.dead_letter_policy[0].max_delivery_attempts == 10
    error_message = "Default max_delivery_attempts must be 10."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.retry_policy[0].minimum_backoff == "10s" && google_pubsub_subscription.ingest.retry_policy[0].maximum_backoff == "600s"
    error_message = "Retry policy must back off from 10s to 600s."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.expiration_policy[0].ttl == "" && google_pubsub_subscription.dead_letter.expiration_policy[0].ttl == ""
    error_message = "Subscriptions must never expire."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.ack_deadline_seconds == 60
    error_message = "Ack deadline must default to 60 s."
  }
  assert {
    condition     = google_pubsub_subscription.dead_letter.topic == "projects/example-project/topics/pulsebridge-hl7v2-ingest-dead-letter"
    error_message = "The dead-letter topic must have its own subscription, or dead-lettered messages are dropped."
  }
  assert {
    condition     = length(google_service_account_iam_member.pubsub_agent_token_creator) == 0
    error_message = "No token-creator grant without push."
  }
}

run "healthcare_agent_is_publisher_on_ingest_topic_and_nothing_wider" {
  command = apply

  assert {
    condition = (
      google_pubsub_topic_iam_member.healthcare_agent_publisher.topic == "pulsebridge-hl7v2-ingest" &&
      google_pubsub_topic_iam_member.healthcare_agent_publisher.role == "roles/pubsub.publisher" &&
      google_pubsub_topic_iam_member.healthcare_agent_publisher.member == "serviceAccount:service-123456789012@gcp-sa-healthcare.iam.gserviceaccount.com"
    )
    error_message = "The Healthcare service agent must be publisher on the ingest topic."
  }
  assert {
    # No other binding in the module names the Healthcare service agent.
    condition = length(concat(
      [for m in [
        google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher.member,
        google_pubsub_subscription_iam_member.pubsub_agent_ingest_subscriber.member,
      ] : m if strcontains(m, "gcp-sa-healthcare")],
      [for b in google_healthcare_fhir_store_iam_member.app : b if strcontains(b.member, "gcp-sa-healthcare")],
      [for b in google_healthcare_hl7_v2_store_iam_member.app : b if strcontains(b.member, "gcp-sa-healthcare")],
      [for b in google_service_account_iam_member.pubsub_agent_token_creator : b if strcontains(b.member, "gcp-sa-healthcare")],
    )) == 0
    error_message = "The Healthcare service agent must hold no other binding in this module."
  }
}

run "pubsub_agent_can_dead_letter" {
  command = apply

  assert {
    condition = (
      google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher.topic == "pulsebridge-hl7v2-ingest-dead-letter" &&
      google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher.role == "roles/pubsub.publisher" &&
      google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher.member == "serviceAccount:service-123456789012@gcp-sa-pubsub.iam.gserviceaccount.com"
    )
    error_message = "The Pub/Sub service agent must be publisher on the dead-letter topic."
  }
  assert {
    condition = (
      google_pubsub_subscription_iam_member.pubsub_agent_ingest_subscriber.subscription == "pulsebridge-hl7v2-ingest-push" &&
      google_pubsub_subscription_iam_member.pubsub_agent_ingest_subscriber.role == "roles/pubsub.subscriber" &&
      google_pubsub_subscription_iam_member.pubsub_agent_ingest_subscriber.member == "serviceAccount:service-123456789012@gcp-sa-pubsub.iam.gserviceaccount.com"
    )
    error_message = "The Pub/Sub service agent must be subscriber on the ingest subscription."
  }
}

run "no_app_bindings_without_service_accounts" {
  command = apply

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.app) == 0 && length(google_healthcare_hl7_v2_store_iam_member.app) == 0
    error_message = "No store bindings may be created when no service accounts are given."
  }
}

run "destroy_deletes_by_default" {
  command = apply

  assert {
    condition = (
      google_healthcare_dataset.this.deletion_policy == "DELETE" &&
      google_healthcare_fhir_store.this.deletion_policy == "DELETE" &&
      google_healthcare_hl7_v2_store.this.deletion_policy == "DELETE"
    )
    error_message = "Default deletion_policy must let destroy remove the dataset and stores."
  }
}

run "prevent_protects_dataset_and_stores" {
  command = apply

  variables {
    deletion_policy = "PREVENT"
  }

  assert {
    condition = (
      google_healthcare_dataset.this.deletion_policy == "PREVENT" &&
      google_healthcare_fhir_store.this.deletion_policy == "PREVENT" &&
      google_healthcare_hl7_v2_store.this.deletion_policy == "PREVENT"
    )
    error_message = "deletion_policy = PREVENT must reach the dataset and both stores."
  }
}

run "outputs_give_paths_consumers_need" {
  command = apply

  assert {
    condition     = output.fhir_base_url == "https://healthcare.googleapis.com/v1/projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir/fhir"
    error_message = "fhir_base_url must be the store's FHIR REST base."
  }
  assert {
    condition     = output.hl7v2_ingest_url == "https://healthcare.googleapis.com/v1/projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2/messages:ingest"
    error_message = "hl7v2_ingest_url must be the store's messages:ingest URL."
  }
  assert {
    condition = output.mllp_adapter_store_flags == {
      hl7_v2_project_id  = "example-project"
      hl7_v2_location_id = "us-central1"
      hl7_v2_dataset_id  = "pulsebridge"
      hl7_v2_store_id    = "pulsebridge-hl7v2"
    }
    error_message = "mllp_adapter_store_flags must hold the short names the adapter flags take."
  }
  assert {
    condition     = output.ingest_topic_id == "projects/example-project/topics/pulsebridge-hl7v2-ingest" && output.ingest_subscription_name == "pulsebridge-hl7v2-ingest-push"
    error_message = "Topic and subscription outputs must carry the module's names."
  }
}

run "time_zone_and_labels_reach_resources" {
  command = apply

  variables {
    time_zone = "Europe/Amsterdam"
    labels    = { app = "pulsebridge" }
  }

  assert {
    condition     = google_healthcare_dataset.this.time_zone == "Europe/Amsterdam"
    error_message = "time_zone must be set on the dataset."
  }
  assert {
    condition = alltrue([for l in [
      google_healthcare_fhir_store.this.labels,
      google_healthcare_hl7_v2_store.this.labels,
      google_pubsub_topic.ingest.labels,
      google_pubsub_topic.dead_letter.labels,
      google_pubsub_subscription.ingest.labels,
      google_pubsub_subscription.dead_letter.labels,
    ] : l == tomap({ app = "pulsebridge" })])
    error_message = "labels must reach both stores, both topics and both subscriptions."
  }
}
