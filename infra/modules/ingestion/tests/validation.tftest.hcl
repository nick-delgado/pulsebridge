# Variable validations reject bad input at plan time. Mocked provider only.

mock_provider "google" {}

variables {
  project_id            = "example-project"
  healthcare_location   = "us-central1"
  healthcare_dataset_id = "pulsebridge"
  fhir_store_id         = "pulsebridge-fhir"
  hl7v2_store_id        = "pulsebridge-hl7v2"
  patient_id            = "PB-EXAMPLE-0001"
  time_zone             = "America/New_York"
  v2_to_fhir_image      = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir@sha256:1111111111111111111111111111111111111111111111111111111111111111"
  health_sync_image     = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync@sha256:2222222222222222222222222222222222222222222222222222222222222222"
  seed_image            = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/seed@sha256:3333333333333333333333333333333333333333333333333333333333333333"

  service_account_emails = {
    "v2-to-fhir"        = "pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
    "pubsub-push"       = "pb-pubsub-push@example-project.iam.gserviceaccount.com"
    "health-sync"       = "pb-health-sync@example-project.iam.gserviceaccount.com"
    "scheduler-invoker" = "pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
    "fhir-proxy"        = "pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    "seeder"            = "pb-seeder@example-project.iam.gserviceaccount.com"
  }
  secret_ids = {
    "google-health-client-id"     = "pb-google-health-client-id"
    "google-health-client-secret" = "pb-google-health-client-secret"
    "google-health-refresh-token" = "pb-google-health-refresh-token"
  }
}

# The baseline above must pass, or every expect_failures below could pass for the wrong reason.
run "baseline_is_valid" {
  command = plan
}

run "baseline_synthetic_is_valid" {
  command = plan
  variables {
    use_synthetic_data = true
    patient_id         = "SYN-0001"
  }
}

# ---- images by digest ------------------------------------------------------------------------

run "rejects_tag_only_image" {
  command = plan
  variables {
    v2_to_fhir_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir:v1.2.0"
  }
  expect_failures = [var.v2_to_fhir_image]
}

run "rejects_tag_plus_digest_image" {
  command = plan
  variables {
    health_sync_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/health-sync:v1@sha256:2222222222222222222222222222222222222222222222222222222222222222"
  }
  expect_failures = [var.health_sync_image]
}

run "rejects_short_digest" {
  command = plan
  variables {
    seed_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/seed@sha256:abc123"
  }
  expect_failures = [var.seed_image]
}

run "rejects_latest_tag" {
  command = plan
  variables {
    v2_to_fhir_image = "us-central1-docker.pkg.dev/example-project/pulsebridge-services/v2-to-fhir:latest"
  }
  expect_failures = [var.v2_to_fhir_image]
}

run "requires_health_sync_image_with_real_data" {
  command = plan
  variables {
    health_sync_image = null
  }
  expect_failures = [var.health_sync_image]
}

run "requires_seed_image_with_synthetic_data" {
  command = plan
  variables {
    use_synthetic_data = true
    patient_id         = "SYN-0001"
    seed_image         = null
  }
  expect_failures = [var.seed_image]
}

# ---- patient ID and the synthetic switch ------------------------------------------------------

run "synthetic_requires_syn_patient" {
  command = plan
  variables {
    use_synthetic_data = true
    patient_id         = "PB-EXAMPLE-0001"
  }
  expect_failures = [var.patient_id]
}

run "synthetic_refuses_bare_syn_prefix" {
  command = plan
  variables {
    use_synthetic_data = true
    patient_id         = "SYN-"
  }
  expect_failures = [var.patient_id]
}

run "real_data_refuses_syn_patient" {
  command = plan
  variables {
    patient_id = "SYN-0001"
  }
  expect_failures = [var.patient_id]
}

run "rejects_patient_id_with_bad_characters" {
  command = plan
  variables {
    patient_id = "PB EXAMPLE/1"
  }
  expect_failures = [var.patient_id]
}

# ---- time zone shape -------------------------------------------------------------------------

run "rejects_fixed_offset_time_zone" {
  command = plan
  variables {
    time_zone = "UTC-5"
  }
  expect_failures = [var.time_zone]
}

run "rejects_etc_gmt_offset_time_zone" {
  command = plan
  variables {
    time_zone = "Etc/GMT+5"
  }
  expect_failures = [var.time_zone]
}

run "rejects_bad_time_zone_shape" {
  command = plan
  variables {
    time_zone = "New York"
  }
  expect_failures = [var.time_zone]
}

run "accepts_utc_and_named_zone_with_three_parts" {
  command = plan
  variables {
    time_zone = "America/Argentina/Buenos_Aires"
  }
}

# ---- accounts, secrets, coordinates ----------------------------------------------------------

run "requires_fhir_proxy_with_real_data" {
  command = plan
  variables {
    service_account_emails = {
      "v2-to-fhir"        = "pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
      "pubsub-push"       = "pb-pubsub-push@example-project.iam.gserviceaccount.com"
      "health-sync"       = "pb-health-sync@example-project.iam.gserviceaccount.com"
      "scheduler-invoker" = "pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
    }
  }
  expect_failures = [var.service_account_emails]
}

run "requires_seeder_account_with_synthetic_data" {
  command = plan
  variables {
    use_synthetic_data = true
    patient_id         = "SYN-0001"
    service_account_emails = {
      "v2-to-fhir"        = "pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
      "pubsub-push"       = "pb-pubsub-push@example-project.iam.gserviceaccount.com"
      "scheduler-invoker" = "pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
    }
  }
  expect_failures = [var.service_account_emails]
}

run "rejects_public_member_as_account" {
  command = plan
  variables {
    service_account_emails = {
      "v2-to-fhir"        = "pb-v2-to-fhir@example-project.iam.gserviceaccount.com"
      "pubsub-push"       = "allUsers"
      "health-sync"       = "pb-health-sync@example-project.iam.gserviceaccount.com"
      "scheduler-invoker" = "pb-scheduler-invoker@example-project.iam.gserviceaccount.com"
      "fhir-proxy"        = "pb-fhir-proxy@example-project.iam.gserviceaccount.com"
    }
  }
  expect_failures = [var.service_account_emails]
}

run "requires_google_health_secrets_with_real_data" {
  command = plan
  variables {
    secret_ids = { "google-health-client-id" = "pb-google-health-client-id" }
  }
  expect_failures = [var.secret_ids]
}

run "rejects_full_path_store_id" {
  command = plan
  variables {
    fhir_store_id = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir"
  }
  expect_failures = [var.fhir_store_id]
}

run "rejects_short_health_sync_timeout" {
  command = plan
  variables {
    health_sync_request_timeout = "120s"
  }
  expect_failures = [var.health_sync_request_timeout]
}

run "rejects_bad_notification_channel" {
  command = plan
  variables {
    notification_channels = ["someone@example.com"]
  }
  expect_failures = [var.notification_channels]
}

run "rejects_unsupported_ingress_value" {
  command = plan
  variables {
    v2_to_fhir_ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  }
  expect_failures = [var.v2_to_fhir_ingress]
}

run "rejects_healthcare_in_another_project_with_internal_ingress" {
  command = plan
  variables {
    healthcare_project_id = "other-project"
  }
  expect_failures = [var.healthcare_project_id]
}

run "allows_healthcare_in_another_project_with_all_ingress" {
  command = plan
  variables {
    healthcare_project_id = "other-project"
    v2_to_fhir_ingress    = "INGRESS_TRAFFIC_ALL"
  }
}

run "allows_healthcare_in_the_same_project" {
  command = plan
  variables {
    healthcare_project_id = "example-project"
  }
}

run "rejects_bad_alert_email" {
  command = plan
  variables {
    alert_email                = "not an email"
    create_alert_email_channel = true
  }
  expect_failures = [var.alert_email]
}

# T26: the address is sensitive, so a separate bool decides whether the channel exists. An address
# without the bool would be silently ignored, and the bool without an address has nothing to send to.
run "rejects_alert_email_without_the_channel_switch" {
  command = plan
  variables {
    alert_email = "alerts@example.com"
  }
  expect_failures = [var.alert_email]
}

run "rejects_the_channel_switch_without_an_address" {
  command = plan
  variables {
    create_alert_email_channel = true
  }
  expect_failures = [google_monitoring_notification_channel.email]
}
