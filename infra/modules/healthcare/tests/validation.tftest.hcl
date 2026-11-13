# Variable validations reject bad input at plan time.

mock_provider "google" {
  override_data {
    target = data.google_project.this
    values = { number = "123456789012" }
  }
}

variables {
  project_id = "example-project"
}

run "rejects_bad_project_id" {
  command = plan
  variables {
    project_id = "Example_Project"
  }
  expect_failures = [var.project_id]
}

run "rejects_bad_name_prefix" {
  command = plan
  variables {
    name_prefix = "-pb"
  }
  expect_failures = [var.name_prefix]
}

run "rejects_bad_location" {
  command = plan
  variables {
    location = "US Central"
  }
  expect_failures = [var.location]
}

run "rejects_abandon_deletion_policy" {
  command = plan
  variables {
    deletion_policy = "ABANDON"
  }
  expect_failures = [var.deletion_policy]
}

run "rejects_unknown_parser_version" {
  command = plan
  variables {
    hl7v2_parser_version = "V4"
  }
  expect_failures = [var.hl7v2_parser_version]
}

run "rejects_push_without_endpoint" {
  command = plan
  variables {
    push_enabled               = true
    push_service_account_email = "pulsebridge-push@example-project.iam.gserviceaccount.com"
  }
  expect_failures = [var.push_endpoint]
}

run "rejects_push_without_service_account" {
  command = plan
  variables {
    push_enabled  = true
    push_endpoint = "https://v2-to-fhir-abc123-uc.a.run.app/pubsub/hl7v2"
  }
  expect_failures = [var.push_service_account_email]
}

run "rejects_plain_http_endpoint" {
  command = plan
  variables {
    push_enabled               = true
    push_endpoint              = "http://v2-to-fhir-abc123-uc.a.run.app/pubsub/hl7v2"
    push_service_account_email = "pulsebridge-push@example-project.iam.gserviceaccount.com"
  }
  expect_failures = [var.push_endpoint]
}

run "rejects_bad_service_account_email" {
  command = plan
  variables {
    push_enabled               = true
    push_endpoint              = "https://v2-to-fhir-abc123-uc.a.run.app/pubsub/hl7v2"
    push_service_account_email = "someone@example.com"
  }
  expect_failures = [var.push_service_account_email]
}

run "rejects_too_few_delivery_attempts" {
  command = plan
  variables {
    max_delivery_attempts = 4
  }
  expect_failures = [var.max_delivery_attempts]
}

run "rejects_ack_deadline_out_of_range" {
  command = plan
  variables {
    ack_deadline_seconds = 5
  }
  expect_failures = [var.ack_deadline_seconds]
}

run "rejects_backoff_max_below_min" {
  command = plan
  variables {
    retry_minimum_backoff = "60s"
    retry_maximum_backoff = "30s"
  }
  expect_failures = [var.retry_maximum_backoff]
}

run "rejects_backoff_over_600s" {
  command = plan
  variables {
    retry_minimum_backoff = "700s"
  }
  expect_failures = [var.retry_minimum_backoff]
}

run "rejects_retention_over_7_days" {
  command = plan
  variables {
    message_retention_duration = "700000s"
  }
  expect_failures = [var.message_retention_duration]
}

run "rejects_unknown_iam_account_key" {
  command = plan
  variables {
    store_iam_service_accounts = {
      openemr = "openemr@example-project.iam.gserviceaccount.com"
    }
  }
  expect_failures = [var.store_iam_service_accounts]
}

run "rejects_non_service_account_iam_email" {
  command = plan
  variables {
    store_iam_service_accounts = {
      fhir_proxy = "person@example.com"
    }
  }
  expect_failures = [var.store_iam_service_accounts]
}

run "accepts_valid_full_input" {
  command = plan
  variables {
    location                   = "europe-west4"
    name_prefix                = "pb-dev"
    time_zone                  = "Europe/Amsterdam"
    deletion_policy            = "PREVENT"
    hl7v2_parser_version       = "V3"
    push_enabled               = true
    push_endpoint              = "https://v2-to-fhir-abc123-ez.a.run.app/pubsub/hl7v2"
    push_service_account_email = "pulsebridge-push@example-project.iam.gserviceaccount.com"
    max_delivery_attempts      = 5
    retry_minimum_backoff      = "600s"
    retry_maximum_backoff      = "600s"
    message_retention_duration = "600s"
    store_iam_service_accounts = {
      health_sync = "health-sync@example-project.iam.gserviceaccount.com"
    }
  }
}

run "rejects_bad_time_zone" {
  command = plan
  variables {
    time_zone = "UTC+2 summer"
  }
  expect_failures = [var.time_zone]
}
