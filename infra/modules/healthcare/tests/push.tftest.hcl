# push_enabled = true: the ingest subscription pushes to v2-to-fhir with an OIDC token.

mock_provider "google" {
  override_data {
    target = data.google_project.this
    values = { number = "123456789012" }
  }
  override_resource {
    target = google_pubsub_topic.dead_letter
    values = { id = "projects/example-project/topics/pulsebridge-hl7v2-ingest-dead-letter" }
  }
}

variables {
  project_id                 = "example-project"
  push_enabled               = true
  push_endpoint              = "https://v2-to-fhir-abc123-uc.a.run.app/pubsub/hl7v2"
  push_service_account_email = "pulsebridge-push@example-project.iam.gserviceaccount.com"
}

run "push_config_with_oidc_when_endpoint_given" {
  command = apply

  assert {
    condition     = length(google_pubsub_subscription.ingest.push_config) == 1
    error_message = "push_enabled must add a push config."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.push_config[0].push_endpoint == "https://v2-to-fhir-abc123-uc.a.run.app/pubsub/hl7v2"
    error_message = "The push endpoint must be the given URL."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.push_config[0].oidc_token[0].service_account_email == "pulsebridge-push@example-project.iam.gserviceaccount.com"
    error_message = "The OIDC token must be signed as the given service account."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.push_config[0].oidc_token[0].audience == "https://v2-to-fhir-abc123-uc.a.run.app"
    error_message = "The default audience must be the endpoint's origin (the Cloud Run service URL)."
  }
  assert {
    condition     = google_pubsub_subscription.ingest.dead_letter_policy[0].dead_letter_topic == "projects/example-project/topics/pulsebridge-hl7v2-ingest-dead-letter"
    error_message = "The push subscription must keep its dead-letter policy."
  }
}

run "pubsub_agent_can_mint_tokens_for_the_push_account_only" {
  command = apply

  assert {
    condition     = length(google_service_account_iam_member.pubsub_agent_token_creator) == 1
    error_message = "push_enabled must add one token-creator grant."
  }
  assert {
    condition = (
      google_service_account_iam_member.pubsub_agent_token_creator[0].service_account_id == "projects/example-project/serviceAccounts/pulsebridge-push@example-project.iam.gserviceaccount.com" &&
      google_service_account_iam_member.pubsub_agent_token_creator[0].role == "roles/iam.serviceAccountTokenCreator" &&
      google_service_account_iam_member.pubsub_agent_token_creator[0].member == "serviceAccount:service-123456789012@gcp-sa-pubsub.iam.gserviceaccount.com"
    )
    error_message = "Token creator must be granted to the Pub/Sub agent on the push account only."
  }
}

run "explicit_audience_overrides_default" {
  command = apply

  variables {
    push_audience = "https://v2-to-fhir.example.test"
  }

  assert {
    condition     = google_pubsub_subscription.ingest.push_config[0].oidc_token[0].audience == "https://v2-to-fhir.example.test"
    error_message = "push_audience must be used when given."
  }
}

run "token_creator_grant_can_be_turned_off" {
  command = apply

  variables {
    grant_pubsub_token_creator = false
  }

  assert {
    condition     = length(google_service_account_iam_member.pubsub_agent_token_creator) == 0
    error_message = "grant_pubsub_token_creator = false must skip the grant."
  }
}

run "endpoint_alone_does_not_switch_on_push" {
  # The decision is the plan-time bool, never "is the endpoint set", so an endpoint that is
  # wired but not yet switched on leaves the subscription pulling (and no token grant).
  command = apply

  variables {
    push_enabled = false
  }

  assert {
    condition     = length(google_pubsub_subscription.ingest.push_config) == 0
    error_message = "push_enabled = false must leave the subscription without a push config even when an endpoint is given."
  }
  assert {
    condition     = length(google_service_account_iam_member.pubsub_agent_token_creator) == 0
    error_message = "push_enabled = false must not grant token creator."
  }
}
