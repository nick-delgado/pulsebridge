# Store-level bindings for the application service accounts. The input is module identity's
# map as it is: hyphenated account names, including keys this module does not use.

mock_provider "google" {
  override_data {
    target = data.google_project.this
    values = { number = "123456789012" }
  }
  override_resource {
    target = google_healthcare_fhir_store.this
    values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir" }
  }
  override_resource {
    target = google_healthcare_hl7_v2_store.this
    values = { id = "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2" }
  }
}

variables {
  project_id = "example-project"
}

run "identity_map_gets_exactly_the_expected_roles" {
  command = apply

  variables {
    # Shaped like module identity's service_account_emails output, extra keys included.
    store_iam_service_accounts = {
      "health-sync"       = "health-sync@example-project.iam.gserviceaccount.com"
      "seeder"            = "seeder@example-project.iam.gserviceaccount.com"
      "v2-to-fhir"        = "v2-to-fhir@example-project.iam.gserviceaccount.com"
      "fhir-proxy"        = "fhir-proxy@example-project.iam.gserviceaccount.com"
      "edge-vm"           = "edge-vm@example-project.iam.gserviceaccount.com"
      "cuff-sim"          = "cuff-sim@example-project.iam.gserviceaccount.com"
      "scheduler-invoker" = "scheduler-invoker@example-project.iam.gserviceaccount.com"
      "pubsub-push"       = "pubsub-push@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition = {
      for k, b in google_healthcare_fhir_store_iam_member.app : k => "${b.member} ${b.role}"
      } == {
      "health-sync" = "serviceAccount:health-sync@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceEditor"
      "seeder"      = "serviceAccount:seeder@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceEditor"
      "v2-to-fhir"  = "serviceAccount:v2-to-fhir@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceEditor"
      "fhir-proxy"  = "serviceAccount:fhir-proxy@example-project.iam.gserviceaccount.com roles/healthcare.fhirResourceReader"
    }
    error_message = "FHIR store bindings must be exactly: health-sync, seeder and v2-to-fhir editor, fhir-proxy reader."
  }
  assert {
    condition = {
      for k, b in google_healthcare_hl7_v2_store_iam_member.app : k => "${b.member} ${b.role}"
      } == {
      "v2-to-fhir" = "serviceAccount:v2-to-fhir@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Consumer"
      "fhir-proxy" = "serviceAccount:fhir-proxy@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Consumer"
      "edge-vm"    = "serviceAccount:edge-vm@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Ingest"
      "cuff-sim"   = "serviceAccount:cuff-sim@example-project.iam.gserviceaccount.com roles/healthcare.hl7V2Ingest"
    }
    error_message = "HL7v2 store bindings must be exactly: v2-to-fhir and fhir-proxy consumer, edge-vm and cuff-sim ingest."
  }
  assert {
    condition     = alltrue([for b in google_healthcare_fhir_store_iam_member.app : b.fhir_store_id == "projects/example-project/locations/us-central1/datasets/pulsebridge/fhirStores/pulsebridge-fhir"])
    error_message = "FHIR bindings must be on the module's FHIR store."
  }
  assert {
    condition     = alltrue([for b in google_healthcare_hl7_v2_store_iam_member.app : b.hl7_v2_store_id == "projects/example-project/locations/us-central1/datasets/pulsebridge/hl7V2Stores/pulsebridge-hl7v2"])
    error_message = "HL7v2 bindings must be on the module's HL7v2 store."
  }
}

run "unknown_keys_are_ignored" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "scheduler-invoker" = "scheduler-invoker@example-project.iam.gserviceaccount.com"
      "pubsub-push"       = "pubsub-push@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.app) == 0 && length(google_healthcare_hl7_v2_store_iam_member.app) == 0
    error_message = "Keys this module does not use must create no binding."
  }
}

run "old_underscore_keys_create_nothing" {
  command = apply

  variables {
    store_iam_service_accounts = {
      health_sync = "health-sync@example-project.iam.gserviceaccount.com"
      v2_to_fhir  = "v2-to-fhir@example-project.iam.gserviceaccount.com"
      fhir_proxy  = "fhir-proxy@example-project.iam.gserviceaccount.com"
      edge_vm     = "edge-vm@example-project.iam.gserviceaccount.com"
      cuff_sim    = "cuff-sim@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.app) == 0 && length(google_healthcare_hl7_v2_store_iam_member.app) == 0
    error_message = "The old underscore keys must create no binding."
  }
}

run "seeder_gets_fhir_editor_only" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "seeder" = "seeder@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = keys(google_healthcare_fhir_store_iam_member.app) == ["seeder"] && google_healthcare_fhir_store_iam_member.app["seeder"].role == "roles/healthcare.fhirResourceEditor"
    error_message = "seeder must get exactly FHIR resource editor on the FHIR store."
  }
  assert {
    condition     = length(google_healthcare_hl7_v2_store_iam_member.app) == 0
    error_message = "seeder must get nothing on the HL7v2 store."
  }
}

run "one_account_gets_only_its_own_binding" {
  command = apply

  variables {
    store_iam_service_accounts = {
      "edge-vm" = "edge-vm@example-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(google_healthcare_fhir_store_iam_member.app) == 0
    error_message = "edge-vm alone must create no FHIR store binding."
  }
  assert {
    condition     = keys(google_healthcare_hl7_v2_store_iam_member.app) == ["edge-vm"] && google_healthcare_hl7_v2_store_iam_member.app["edge-vm"].role == "roles/healthcare.hl7V2Ingest"
    error_message = "edge-vm alone must create exactly one HL7v2 ingest binding."
  }
}
